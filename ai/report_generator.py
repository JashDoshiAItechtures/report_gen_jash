"""Report generation pipeline — intent classification + LLM-based report builder.

This module adds report generation capability to the SQL chatbot without
modifying any existing chat behaviour.
"""

import hashlib
import json
import logging
import re
from datetime import date
from typing import Any

import dspy

from ai.groq_setup import get_lm
from ai.report_signatures import ReportGeneration, ReportModification
from ai.validator import validate_sql, check_sql_against_schema
from ai.sql_pattern_checker import check_sql_patterns, format_issues_for_repair
from ai.signatures import SQLRepair, AnalyzeAndPlan, SQLGeneration
from db.schema import format_schema, get_schema
from db.relationships import format_relationships
from db.profiler import get_data_profile
from db.executor import execute_sql

logger = logging.getLogger(__name__)

MAX_REPAIR_RETRIES = 2

# ── Blueprint Cache ─────────────────────────────────────────────────────────
# Caches the LLM-generated report blueprint (JSON structure with SQL queries,
# chart types, KPI labels, etc.) keyed by a normalized hash of the user's
# question. Same question → identical report structure every time.
# Data values are still executed fresh from the DB.
# Cache is persisted to disk so it survives server restarts.
import pathlib as _pathlib

_CACHE_DIR = _pathlib.Path(__file__).resolve().parent.parent / ".report_cache"
_CACHE_FILE = _CACHE_DIR / "blueprints.json"


def _load_blueprint_cache() -> dict[str, dict]:
    """Load cached blueprints from disk."""
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            logger.info("Loaded %d cached report blueprints from disk", len(data))
            return data
    except Exception as exc:
        logger.warning("Failed to load blueprint cache: %s", exc)
    return {}


def _save_blueprint_cache(cache: dict[str, dict]) -> None:
    """Save cached blueprints to disk."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Failed to save blueprint cache: %s", exc)


_blueprint_cache: dict[str, dict] = _load_blueprint_cache()

# Per-key locks to prevent duplicate LLM calls when two users request
# the same report simultaneously.  The second request waits for the
# first LLM call to finish and then gets the cached result.
import threading as _threading
_cache_locks: dict[str, _threading.Lock] = {}
_cache_locks_guard = _threading.Lock()  # protects _cache_locks dict itself


def _get_cache_lock(key: str) -> _threading.Lock:
    """Get or create a per-key lock for blueprint generation."""
    with _cache_locks_guard:
        if key not in _cache_locks:
            _cache_locks[key] = _threading.Lock()
        return _cache_locks[key]

# ── Intent classification (deterministic — no LLM) ─────────────────────────

_REPORT_KEYWORDS = [
    # Generic report terms
    "report", "dashboard", "analyze", "analysis", "trend", "trends",
    "summary", "comparison", "compare", "insight", "insights",
    "performance", "overview", "breakdown", "kpi", "kpis",
    "analytics", "metrics", "statistics", "visualize", "visualization",
    "chart", "graph", "show me", "give me a report",
    "top 10", "top 5", "top 20",
    # Sales / revenue
    "sales report", "revenue report", "revenue analysis",
    # Operations / order status
    "open orders", "open order", "inorder", "in-order", "in order",
    "order status", "order fulfillment", "active orders", "pending orders",
    # Backorder
    "backorder", "back order", "back-order", "unfulfilled", "outstanding orders",
    # Procurement / purchasing
    "purchase order", "procurement", "vendor report", "vendor analysis",
    "po report", "supplier report",
    # Customer / product
    "customer report", "customer analysis", "product report", "product analysis",
    "inventory report", "stock report",
    # Financial
    "cost report", "margin report", "profitability", "financial report",
]

_REPORT_PATTERNS = [
    r"\b(?:show|give|create|generate|build|make)\b.*\b(?:report|dashboard|analysis|overview)\b",
    r"\b(?:sales|revenue|order|product|customer|vendor)\s+(?:performance|analysis|breakdown|trend|summary)\b",
    r"\b(?:analyze|analyse)\b",
    r"\btop\s+\d+\b.*\b(?:product|customer|vendor|item|sku)\b",
    # Operational / backorder / procurement report patterns
    r"\b(?:backorder|back-order|inorder|in-order)\b",
    r"\b(?:open|pending|active|processing)\s+orders?\b",
    r"\b(?:order|purchase)\s+(?:status|fulfillment|pipeline)\b",
    r"\b(?:procurement|purchasing)\s+(?:report|analysis|overview|summary|dashboard)\b",
    r"\b(?:vendor|supplier)\s+(?:report|analysis|performance|summary)\b",
    r"\bunfulfilled\s+(?:orders?|lines?)\b",
]


def classify_intent(question: str) -> str:
    """Classify user intent as 'chat' or 'report'.

    Uses keyword matching and regex patterns — fully deterministic.
    """
    q = question.lower().strip()

    # Check for explicit report patterns first
    for pattern in _REPORT_PATTERNS:
        if re.search(pattern, q):
            return "report"

    # Keyword check — at least one keyword must appear
    for kw in _REPORT_KEYWORDS:
        if kw in q:
            return "report"

    return "chat"


# ── SQL Auto-Correction ────────────────────────────────────────────────────
# The LLM persistently treats sales_order_line_pricing as if it were a
# combined sales_order + sales_order_line table. It references columns like
# status, so_id, product_id on the pricing table, but those columns live on  
# sales_order and sales_order_line respectively.
#
# This auto-corrector detects and rewrites these broken queries.

# Columns that belong to sales_order (NOT on line/pricing/gold/diamond)
_SO_ONLY_COLS = {'status', 'so_id', 'customer_id', 'order_date', 'total_amount',
                 'order_number', 'created_at', 'updated_at'}
# Columns that belong to sales_order_line (NOT pricing/gold/diamond)
_SOL_ONLY_COLS = {'product_id', 'variant_sku', 'quantity', 'so_id'}
# Sub-tables that are frequently misused as main FROM tables
_SUB_TABLES = {
    'sales_order_line_pricing': 'solp',
    'sales_order_line_gold': 'solg',
    'sales_order_line_diamond': 'sold',
}


def _fix_report_sql(sql: str) -> str:
    """Auto-correct common SQL mistakes generated by the LLM.

    Handles multiple error patterns:
    1. Sub-tables (pricing/gold/diamond) used as FROM with wrong column refs
    2. sales_order joined directly to product_master (missing sales_order_line)
    3. so.product_id references (product_id lives on sales_order_line, not sales_order)
    """
    if not sql:
        return sql

    original = sql

    # Normalize whitespace for easier matching
    sql_oneline = ' '.join(sql.split())

    # ══════════════════════════════════════════════════════════════════════
    # PASS 1: Fix sub-table (pricing/gold/diamond) alias issues
    # ══════════════════════════════════════════════════════════════════════
    for sub_tbl, preferred_alias in _SUB_TABLES.items():
        # Find the alias used for this sub-table
        alias_match = re.search(
            rf'\b{sub_tbl}\s+(\w+)\b', sql_oneline, re.IGNORECASE
        )
        if not alias_match:
            continue

        alias = alias_match.group(1)

        # Check if this alias references columns it doesn't own (aliased refs)
        has_status_ref = bool(re.search(rf'\b{re.escape(alias)}\.status\b', sql_oneline, re.IGNORECASE))
        has_so_id_ref = bool(re.search(rf'\b{re.escape(alias)}\.so_id\b', sql_oneline, re.IGNORECASE))
        has_product_id_ref = bool(re.search(rf'\b{re.escape(alias)}\.product_id\b', sql_oneline, re.IGNORECASE))
        has_customer_id_ref = bool(re.search(rf'\b{re.escape(alias)}\.customer_id\b', sql_oneline, re.IGNORECASE))
        has_order_date_ref = bool(re.search(rf'\b{re.escape(alias)}\.order_date\b', sql_oneline, re.IGNORECASE))
        has_quantity_ref = bool(re.search(rf'\b{re.escape(alias)}\.quantity\b', sql_oneline, re.IGNORECASE))
        has_variant_sku_ref = bool(re.search(rf'\b{re.escape(alias)}\.variant_sku\b', sql_oneline, re.IGNORECASE))
        has_total_amount_ref = bool(re.search(rf'\b{re.escape(alias)}\.total_amount\b', sql_oneline, re.IGNORECASE))

        # Also detect UNPREFIXED column references — the LLM sometimes writes
        # "SELECT product_id FROM sales_order_line_pricing WHERE status = 'closed'"
        # without any alias prefix. These bare columns are wrong if the sub-table
        # doesn't own them.
        is_from_table = bool(re.search(rf'\bFROM\s+{sub_tbl}\b', sql_oneline, re.IGNORECASE))
        if is_from_table:
            # Check for bare (unprefixed) column references
            for col in ('status', 'order_date', 'customer_id', 'total_amount'):
                if not has_status_ref and col == 'status' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_status_ref = True
                if not has_order_date_ref and col == 'order_date' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_order_date_ref = True
                if not has_customer_id_ref and col == 'customer_id' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_customer_id_ref = True
                if not has_total_amount_ref and col == 'total_amount' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_total_amount_ref = True
            for col in ('product_id', 'variant_sku', 'quantity'):
                if not has_product_id_ref and col == 'product_id' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_product_id_ref = True
                if not has_variant_sku_ref and col == 'variant_sku' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_variant_sku_ref = True
                if not has_quantity_ref and col == 'quantity' and re.search(rf'(?<!\w\.)\b{col}\b', sql_oneline, re.IGNORECASE):
                    has_quantity_ref = True

        needs_sol = has_so_id_ref or has_product_id_ref or has_quantity_ref or has_variant_sku_ref
        needs_so = has_status_ref or has_customer_id_ref or has_order_date_ref or has_total_amount_ref

        if not needs_sol and not needs_so:
            continue

        logger.info(
            "SQL auto-correct: %s alias '%s' references wrong columns "
            "(status=%s, so_id=%s, product_id=%s). Injecting proper joins.",
            sub_tbl, alias, has_status_ref, has_so_id_ref, has_product_id_ref
        )

        # Check what tables are already in the query
        stripped = sql_oneline
        for t in _SUB_TABLES:
            stripped = stripped.replace(t, '')
        has_sol_table = 'sales_order_line' in stripped.replace('sales_order_line_', '')
        has_so_table = bool(re.search(r'\bsales_order\b(?!_)', stripped))

        # Choose alias names that won't conflict
        sol_alias = 'sol' if alias != 'sol' else 'sol2'
        so_alias = 'so' if alias != 'so' else 'so2'

        # Restructure the FROM clause
        from_pattern = re.compile(
            rf'FROM\s+{sub_tbl}\s+{re.escape(alias)}\b',
            re.IGNORECASE
        )
        if from_pattern.search(sql_oneline):
            new_from = f'FROM sales_order {so_alias}'
            new_from += f' JOIN sales_order_line {sol_alias} ON {so_alias}.so_id = {sol_alias}.so_id'
            new_from += f' JOIN {sub_tbl} {alias} ON {sol_alias}.sol_id = {alias}.sol_id'
            sql_oneline = from_pattern.sub(new_from, sql_oneline, count=1)

            # Remove any now-redundant JOIN to sales_order
            sql_oneline = re.sub(
                rf'\bJOIN\s+sales_order\s+{re.escape(so_alias)}\s+ON\s+[^J]*?(?=JOIN|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)',
                '', sql_oneline, flags=re.IGNORECASE
            )
            has_sol_table = True
            has_so_table = True
        else:
            # Sub-table is joined, not FROM — inject missing intermediate tables
            if needs_sol and not has_sol_table:
                sql_oneline = re.sub(
                    rf'JOIN\s+{sub_tbl}\s+{re.escape(alias)}\b',
                    f'JOIN sales_order_line {sol_alias} ON {sol_alias}.so_id = {so_alias}.so_id '
                    f'JOIN {sub_tbl} {alias}',
                    sql_oneline, count=1, flags=re.IGNORECASE
                )
                sql_oneline = re.sub(
                    rf'JOIN\s+{sub_tbl}\s+{re.escape(alias)}\s+ON\s+\w+\.so_id\s*=\s*{re.escape(alias)}\.so_id',
                    f'JOIN {sub_tbl} {alias} ON {sol_alias}.sol_id = {alias}.sol_id',
                    sql_oneline, flags=re.IGNORECASE
                )
                has_sol_table = True

            if needs_so and not has_so_table:
                sql_oneline = re.sub(
                    r'\bFROM\b',
                    f'FROM sales_order {so_alias} JOIN',
                    sql_oneline, count=1, flags=re.IGNORECASE
                )
                has_so_table = True

        # Remap column references to correct aliases
        if has_status_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.status\b',
                f'{so_alias}.status',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_so_id_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.so_id\b',
                f'{sol_alias}.so_id',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_product_id_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.product_id\b',
                f'{sol_alias}.product_id',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_customer_id_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.customer_id\b',
                f'{so_alias}.customer_id',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_order_date_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.order_date\b',
                f'{so_alias}.order_date',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_total_amount_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.total_amount\b',
                f'{so_alias}.total_amount',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_quantity_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.quantity\b',
                f'{sol_alias}.quantity',
                sql_oneline, flags=re.IGNORECASE
            )
        if has_variant_sku_ref:
            sql_oneline = re.sub(
                rf'\b{re.escape(alias)}\.variant_sku\b',
                f'{sol_alias}.variant_sku',
                sql_oneline, flags=re.IGNORECASE
            )

        # Remap UNPREFIXED (bare) column references to correct aliases
        # e.g., "WHERE status = 'closed'" → "WHERE so.status = 'closed'"
        if is_from_table:
            bare_so_cols = {'status': so_alias, 'order_date': so_alias,
                           'customer_id': so_alias, 'total_amount': so_alias}
            bare_sol_cols = {'product_id': sol_alias, 'variant_sku': sol_alias,
                            'quantity': sol_alias}
            for col, target_alias in {**bare_so_cols, **bare_sol_cols}.items():
                # Match bare column NOT preceded by a dot (i.e., not already aliased)
                sql_oneline = re.sub(
                    rf'(?<!\w\.)(?<!\w)\b{col}\b(?!\s*\()',  # avoid function names
                    f'{target_alias}.{col}',
                    sql_oneline, flags=re.IGNORECASE
                )
    # ══════════════════════════════════════════════════════════════════════
    # PASS 2: Fix so.product_id → sol.product_id  (product_id lives on
    #         sales_order_line, NOT sales_order)
    # ══════════════════════════════════════════════════════════════════════

    # Find all aliases used for sales_order (but NOT sales_order_line*)
    so_aliases = set()
    for m in re.finditer(r'\bsales_order\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE):
        so_aliases.add(m.group(1))

    for so_alias in so_aliases:
        if not re.search(rf'\b{re.escape(so_alias)}\.product_id\b', sql_oneline, re.IGNORECASE):
            continue

        logger.info("SQL auto-correct: %s.product_id detected — product_id is on sales_order_line, not sales_order", so_alias)

        # Find the alias for sales_order_line if it exists
        sol_match = re.search(r'\bsales_order_line\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE)

        if sol_match:
            sol_alias = sol_match.group(1)
        else:
            # Need to inject sales_order_line into the query
            sol_alias = 'sol'
            # Insert JOIN sales_order_line after FROM sales_order so
            sql_oneline = re.sub(
                rf'(FROM\s+sales_order\s+{re.escape(so_alias)})\b',
                rf'\1 JOIN sales_order_line {sol_alias} ON {so_alias}.so_id = {sol_alias}.so_id',
                sql_oneline, count=1, flags=re.IGNORECASE
            )

        # Remap so.product_id → sol.product_id
        sql_oneline = re.sub(
            rf'\b{re.escape(so_alias)}\.product_id\b',
            f'{sol_alias}.product_id',
            sql_oneline, flags=re.IGNORECASE
        )

    # ══════════════════════════════════════════════════════════════════════
    # PASS 3: Fix direct sales_order → product_master JOIN (missing
    #         sales_order_line in between).
    #         Pattern: JOIN product_master pm ON so.product_id = pm.product_id
    #         Fix: inject sales_order_line, rewrite JOIN condition
    # ══════════════════════════════════════════════════════════════════════

    # Detect: JOIN product_master <alias> ON <so_alias>.product_id = <pm_alias>.product_id
    # where <so_alias> is a sales_order alias (already caught above, but joining might
    # still reference the wrong table).  After Pass 2, so.product_id is already fixed,
    # but we still need to ensure the JOIN to product_master goes through sol.
    # This is already handled by Pass 2 remapping, so no additional action needed
    # if Pass 2 ran.  But handle the case where the LLM omits sales_order_line entirely
    # and writes: FROM sales_order so JOIN product_master pm ON so.product_id = pm.product_id
    # (After Pass 2, this becomes sol.product_id, and the JOIN is already injected.)

    # ══════════════════════════════════════════════════════════════════════
    # PASS 4: Fix raw product_id / variant_sku used as a chart label column.
    #
    # The LLM sometimes writes:
    #   SELECT sol.product_id, SUM(...) AS value FROM ... GROUP BY sol.product_id
    # This produces SKU codes (PROD-0484) as labels instead of human-readable names.
    # Fix: inject JOIN to product_master and replace with pm.product_name.
    # ══════════════════════════════════════════════════════════════════════

    # Detect sol.<alias>.product_id or sol.variant_sku as first SELECT token
    _sol_alias_m = re.search(r'\bsales_order_line\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE)
    _sol_alias_p4 = _sol_alias_m.group(1) if _sol_alias_m else 'sol'

    # Check: does the SELECT clause start with <sol_alias>.product_id or .variant_sku?
    _select_label_pid = re.search(
        rf'\bSELECT\s+{re.escape(_sol_alias_p4)}\.(product_id|variant_sku)\b',
        sql_oneline, re.IGNORECASE
    )
    if _select_label_pid:
        logger.info(
            "SQL auto-correct PASS 4: %s.%s used as chart label — rewriting to pm.product_name",
            _sol_alias_p4, _select_label_pid.group(1)
        )
        # Check if product_master is already joined
        _has_pm = bool(re.search(r'\bproduct_master\b', sql_oneline, re.IGNORECASE))
        _pm_alias = 'pm'
        if not _has_pm:
            # Find a good place to inject: after the last JOIN or after FROM clause
            # Inject before WHERE / GROUP / ORDER / LIMIT
            _inject_point = re.search(
                r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT)\b', sql_oneline, re.IGNORECASE
            )
            if _inject_point:
                pos = _inject_point.start()
                sql_oneline = (
                    sql_oneline[:pos]
                    + f'JOIN product_master {_pm_alias} ON {_sol_alias_p4}.product_id = {_pm_alias}.product_id '
                    + sql_oneline[pos:]
                )
            else:
                sql_oneline += f' JOIN product_master {_pm_alias} ON {_sol_alias_p4}.product_id = {_pm_alias}.product_id'
        else:
            # Find the pm alias already in use
            _pm_alias_m = re.search(r'\bproduct_master\s+(\w+)', sql_oneline, re.IGNORECASE)
            if _pm_alias_m:
                _pm_alias = _pm_alias_m.group(1)

        # Replace the label column in SELECT
        sql_oneline = re.sub(
            rf'\bSELECT\s+{re.escape(_sol_alias_p4)}\.(product_id|variant_sku)\b',
            f'SELECT {_pm_alias}.product_name',
            sql_oneline, count=1, flags=re.IGNORECASE
        )
        # Replace in GROUP BY
        sql_oneline = re.sub(
            rf'\bGROUP\s+BY\s+{re.escape(_sol_alias_p4)}\.(product_id|variant_sku)\b',
            f'GROUP BY {_pm_alias}.product_name',
            sql_oneline, flags=re.IGNORECASE
        )

    # ══════════════════════════════════════════════════════════════════════
    # PASS 5: Fix raw customer_id used as chart label → customer_name
    #
    # The LLM sometimes writes:
    #   SELECT so.customer_id, SUM(...) AS value FROM ... GROUP BY so.customer_id
    # This produces raw IDs (C001) as labels instead of human-readable names.
    # Fix: inject JOIN to customer_master and replace with cm.customer_name.
    # ══════════════════════════════════════════════════════════════════════

    _SQL_KEYWORDS = {'WHERE', 'GROUP', 'ORDER', 'LIMIT', 'JOIN', 'ON', 'LEFT',
                     'RIGHT', 'INNER', 'HAVING', 'UNION', 'AS', 'SET', 'INTO',
                     'VALUES', 'SELECT', 'FROM', 'AND', 'OR', 'NOT', 'IN',
                     'CROSS', 'FULL', 'OUTER', 'NATURAL', 'USING', 'BETWEEN'}

    _so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE)
    _so_alias_p5 = _so_alias_m.group(1) if _so_alias_m else None
    # Guard: if the captured "alias" is actually a SQL keyword, there's no real alias
    if _so_alias_p5 and _so_alias_p5.upper() in _SQL_KEYWORDS:
        _so_alias_p5 = None
    # Use the table name directly when no alias is present
    _so_ref_p5 = _so_alias_p5 if _so_alias_p5 else 'sales_order'

    # Check: does the SELECT clause contain <so_alias>.customer_id as a label?
    _select_label_cid = re.search(
        rf'\bSELECT\s+{re.escape(_so_ref_p5)}\.customer_id\b',
        sql_oneline, re.IGNORECASE
    )
    # Also check for bare customer_id (no alias prefix)
    if not _select_label_cid:
        _select_label_cid = re.search(
            r'\bSELECT\s+customer_id\b',
            sql_oneline, re.IGNORECASE
        )

    if _select_label_cid:
        logger.info("SQL auto-correct PASS 5: customer_id used as chart label — rewriting to cm.customer_name")

        _has_cm = bool(re.search(r'\bcustomer_master\b', sql_oneline, re.IGNORECASE))
        _cm_alias = 'cm'
        if not _has_cm:
            _inject_point = re.search(
                r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT)\b', sql_oneline, re.IGNORECASE
            )
            if _inject_point:
                pos = _inject_point.start()
                sql_oneline = (
                    sql_oneline[:pos]
                    + f'JOIN customer_master {_cm_alias} ON {_so_ref_p5}.customer_id = {_cm_alias}.customer_id '
                    + sql_oneline[pos:]
                )
            else:
                sql_oneline += f' JOIN customer_master {_cm_alias} ON {_so_ref_p5}.customer_id = {_cm_alias}.customer_id'
        else:
            _cm_alias_m = re.search(r'\bcustomer_master\s+(\w+)', sql_oneline, re.IGNORECASE)
            if _cm_alias_m:
                _cm_alias = _cm_alias_m.group(1)

        # Replace customer_id with customer_name in SELECT
        sql_oneline = re.sub(
            rf'\bSELECT\s+(?:{re.escape(_so_ref_p5)}\.)?customer_id\b',
            f'SELECT {_cm_alias}.customer_name',
            sql_oneline, count=1, flags=re.IGNORECASE
        )
        # Replace in GROUP BY
        sql_oneline = re.sub(
            rf'\bGROUP\s+BY\s+(?:{re.escape(_so_ref_p5)}\.)?customer_id\b',
            f'GROUP BY {_cm_alias}.customer_name',
            sql_oneline, flags=re.IGNORECASE
        )

    # ══════════════════════════════════════════════════════════════════════
    # PASS 6: Fix raw vendor_id used as chart label → vendor_name
    # ══════════════════════════════════════════════════════════════════════

    _po_alias_m = re.search(r'\bpurchase_order\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE)
    _po_alias_p6 = _po_alias_m.group(1) if _po_alias_m else None
    if _po_alias_p6 and _po_alias_p6.upper() in _SQL_KEYWORDS:
        _po_alias_p6 = None
    _po_ref_p6 = _po_alias_p6 if _po_alias_p6 else 'purchase_order'

    _select_label_vid = re.search(
        rf'\bSELECT\s+(?:{re.escape(_po_ref_p6)}\.)?vendor_id\b',
        sql_oneline, re.IGNORECASE
    )
    if _select_label_vid:
        logger.info("SQL auto-correct PASS 6: vendor_id used as chart label — rewriting to vm.vendor_name")
        _has_vm = bool(re.search(r'\bvendor_master\b', sql_oneline, re.IGNORECASE))
        _vm_alias = 'vm'
        if not _has_vm:
            _inject_point = re.search(
                r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT)\b', sql_oneline, re.IGNORECASE
            )
            if _inject_point:
                pos = _inject_point.start()
                sql_oneline = (
                    sql_oneline[:pos]
                    + f'JOIN vendor_master {_vm_alias} ON {_po_ref_p6}.vendor_id = {_vm_alias}.vendor_id '
                    + sql_oneline[pos:]
                )
            else:
                sql_oneline += f' JOIN vendor_master {_vm_alias} ON {_po_ref_p6}.vendor_id = {_vm_alias}.vendor_id'
        else:
            _vm_alias_m = re.search(r'\bvendor_master\s+(\w+)', sql_oneline, re.IGNORECASE)
            if _vm_alias_m:
                _vm_alias = _vm_alias_m.group(1)

        sql_oneline = re.sub(
            rf'\bSELECT\s+(?:{re.escape(_po_ref_p6)}\.)?vendor_id\b',
            f'SELECT {_vm_alias}.vendor_name',
            sql_oneline, count=1, flags=re.IGNORECASE
        )
        sql_oneline = re.sub(
            rf'\bGROUP\s+BY\s+(?:{re.escape(_po_ref_p6)}\.)?vendor_id\b',
            f'GROUP BY {_vm_alias}.vendor_name',
            sql_oneline, flags=re.IGNORECASE
        )

    # ══════════════════════════════════════════════════════════════════════
    # PASS 7: Fix gold/diamond-specific columns referenced on wrong alias.
    #
    # gold_kt, gold_purity, gold_weight_grams  → sales_order_line_gold  (NOT pricing)
    # diamond_type, diamond_carat, diamond_quality, diamond_color → sales_order_line_diamond
    #
    # The LLM writes lp.gold_kt or solp.gold_kt — both fail at DB level.
    # Postgres HINT: "Perhaps you meant to reference the column 'g.gold_kt'."
    # ══════════════════════════════════════════════════════════════════════

    _GOLD_ONLY_COLS7 = ('gold_kt', 'gold_purity', 'gold_weight_grams')
    _DIAMOND_ONLY_COLS7 = ('diamond_type', 'diamond_carat', 'diamond_quality', 'diamond_color')

    # Detect current gold / diamond / sol table aliases
    _solg_m7 = re.search(r'\bsales_order_line_gold\s+(\w+)', sql_oneline, re.IGNORECASE)
    _solg_alias7 = _solg_m7.group(1) if _solg_m7 else None
    _sold_m7 = re.search(r'\bsales_order_line_diamond\s+(\w+)', sql_oneline, re.IGNORECASE)
    _sold_alias7 = _sold_m7.group(1) if _sold_m7 else None
    _sol_m7 = re.search(r'\bsales_order_line\b(?!_)\s+(\w+)', sql_oneline, re.IGNORECASE)
    _sol_alias7 = _sol_m7.group(1) if _sol_m7 else 'sol'

    for _gcol in _GOLD_ONLY_COLS7:
        _bad_gold_refs = re.findall(rf'\b(\w+)\.{_gcol}\b', sql_oneline, re.IGNORECASE)
        for _bad_a in set(_bad_gold_refs):
            if _solg_alias7 and _bad_a.lower() == _solg_alias7.lower():
                continue  # already on the correct gold table alias
            if _solg_alias7:
                sql_oneline = re.sub(
                    rf'\b{re.escape(_bad_a)}\.{_gcol}\b',
                    f'{_solg_alias7}.{_gcol}',
                    sql_oneline, flags=re.IGNORECASE
                )
                logger.info("SQL auto-correct PASS 7: %s.%s → %s.%s (gold column on wrong alias)",
                            _bad_a, _gcol, _solg_alias7, _gcol)
            else:
                _solg_alias7 = 'solg'
                _inj7 = re.search(r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT)\b', sql_oneline, re.IGNORECASE)
                _join7 = f'JOIN sales_order_line_gold {_solg_alias7} ON {_sol_alias7}.sol_id = {_solg_alias7}.sol_id '
                if _inj7:
                    pos = _inj7.start()
                    sql_oneline = sql_oneline[:pos] + _join7 + sql_oneline[pos:]
                else:
                    sql_oneline += ' ' + _join7.strip()
                sql_oneline = re.sub(
                    rf'\b{re.escape(_bad_a)}\.{_gcol}\b',
                    f'{_solg_alias7}.{_gcol}',
                    sql_oneline, flags=re.IGNORECASE
                )
                logger.info("SQL auto-correct PASS 7: added gold join + fixed %s.%s → %s.%s",
                            _bad_a, _gcol, _solg_alias7, _gcol)

    for _dcol in _DIAMOND_ONLY_COLS7:
        _bad_dia_refs = re.findall(rf'\b(\w+)\.{_dcol}\b', sql_oneline, re.IGNORECASE)
        for _bad_a in set(_bad_dia_refs):
            if _sold_alias7 and _bad_a.lower() == _sold_alias7.lower():
                continue
            if _sold_alias7:
                sql_oneline = re.sub(
                    rf'\b{re.escape(_bad_a)}\.{_dcol}\b',
                    f'{_sold_alias7}.{_dcol}',
                    sql_oneline, flags=re.IGNORECASE
                )
                logger.info("SQL auto-correct PASS 7: %s.%s → %s.%s (diamond column on wrong alias)",
                            _bad_a, _dcol, _sold_alias7, _dcol)
            else:
                _sold_alias7 = 'sold'
                _inj7 = re.search(r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT)\b', sql_oneline, re.IGNORECASE)
                _join7 = f'JOIN sales_order_line_diamond {_sold_alias7} ON {_sol_alias7}.sol_id = {_sold_alias7}.sol_id '
                if _inj7:
                    pos = _inj7.start()
                    sql_oneline = sql_oneline[:pos] + _join7 + sql_oneline[pos:]
                else:
                    sql_oneline += ' ' + _join7.strip()
                sql_oneline = re.sub(
                    rf'\b{re.escape(_bad_a)}\.{_dcol}\b',
                    f'{_sold_alias7}.{_dcol}',
                    sql_oneline, flags=re.IGNORECASE
                )
                logger.info("SQL auto-correct PASS 7: added diamond join + fixed %s.%s → %s.%s",
                            _bad_a, _dcol, _sold_alias7, _dcol)

    sql = sql_oneline

    if sql != original:
        logger.info("SQL auto-corrected:\n  BEFORE: %s\n  AFTER:  %s",
                     original.replace('\n', ' ')[:300],
                     sql[:300])

    return sql


# ── Server-side filter injection ───────────────────────────────────────────
# Instead of asking the LLM to regenerate SQL with filters, we inject
# WHERE clauses programmatically into the existing working SQL.

def _inject_filters(sql: str, filters: dict) -> str:
    """Inject WHERE conditions into an existing SQL query for applied filters.

    This is the reliable alternative to asking the LLM to rewrite queries.
    It modifies the existing (working) SQL by adding/extending WHERE clauses
    and injecting required JOINs if needed.

    Args:
        sql: Original SQL query string
        filters: Dict with keys: date_from, date_to, category, status, customer, product
    """
    if not sql or not filters:
        return sql

    sql = ' '.join(sql.split())  # normalize whitespace
    conditions = []

    # ── Date filters ──────────────────────────────────────────────────
    # Only apply if the query references sales_order
    if filters.get('date_from') and re.search(r'\bsales_order\b(?!_)', sql, re.IGNORECASE):
        # Find the alias for sales_order
        so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
        so_alias = so_alias_m.group(1) if so_alias_m else 'so'
        conditions.append(f"{so_alias}.order_date >= '{filters['date_from']}'")

    if filters.get('date_to') and re.search(r'\bsales_order\b(?!_)', sql, re.IGNORECASE):
        so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
        so_alias = so_alias_m.group(1) if so_alias_m else 'so'
        conditions.append(f"{so_alias}.order_date <= '{filters['date_to']}'")

    # ── Status filter ─────────────────────────────────────────────────
    if filters.get('status') and re.search(r'\bsales_order\b(?!_)', sql, re.IGNORECASE):
        so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
        so_alias = so_alias_m.group(1) if so_alias_m else 'so'
        status_val = filters['status'].replace("'", "''")
        # Remove any existing status condition and replace
        sql = re.sub(
            rf"\b{re.escape(so_alias)}\.status\s*=\s*'[^']*'",
            f"{so_alias}.status = '{status_val}'",
            sql, flags=re.IGNORECASE
        )
        # If no existing status condition was replaced, add one
        if not re.search(rf"\b{re.escape(so_alias)}\.status\s*=", sql, re.IGNORECASE):
            conditions.append(f"{so_alias}.status = '{status_val}'")

    # ── Category filter ───────────────────────────────────────────────
    if filters.get('category'):
        cat_val = filters['category'].replace("'", "''")
        # Check if product_master is already in the query
        pm_match = re.search(r'\bproduct_master\s+(\w+)', sql, re.IGNORECASE)
        if pm_match:
            pm_alias = pm_match.group(1)
        else:
            # Need to inject the join chain: sales_order_line + product_master
            pm_alias = 'pm'
            sol_match = re.search(r'\bsales_order_line\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
            so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)

            if sol_match:
                sol_alias = sol_match.group(1)
                # sales_order_line exists, just add product_master join
                sql = re.sub(
                    r'(\bWHERE\b)',
                    f'JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id WHERE',
                    sql, count=1, flags=re.IGNORECASE
                )
                if 'WHERE' not in sql.upper():
                    sql += f' JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id'
            elif so_alias_m:
                so_alias = so_alias_m.group(1)
                sol_alias = 'sol'
                # Need both sales_order_line and product_master
                join_clause = (f'JOIN sales_order_line {sol_alias} ON {so_alias}.so_id = {sol_alias}.so_id '
                               f'JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id')
                if 'WHERE' in sql.upper():
                    sql = re.sub(r'(\bWHERE\b)', f'{join_clause} WHERE', sql, count=1, flags=re.IGNORECASE)
                else:
                    sql += f' {join_clause}'

        conditions.append(f"{pm_alias}.category = '{cat_val}'")

    # ── Product filter ────────────────────────────────────────────────
    if filters.get('product'):
        prod_val = filters['product'].replace("'", "''")
        pm_match = re.search(r'\bproduct_master\s+(\w+)', sql, re.IGNORECASE)
        if pm_match:
            pm_alias = pm_match.group(1)
        else:
            # Inject join chain (same logic as category)
            pm_alias = 'pm'
            sol_match = re.search(r'\bsales_order_line\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
            so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)

            if sol_match:
                sol_alias = sol_match.group(1)
                if 'WHERE' in sql.upper():
                    sql = re.sub(
                        r'(\bWHERE\b)',
                        f'JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id WHERE',
                        sql, count=1, flags=re.IGNORECASE
                    )
                else:
                    sql += f' JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id'
            elif so_alias_m:
                so_alias = so_alias_m.group(1)
                sol_alias = 'sol'
                join_clause = (f'JOIN sales_order_line {sol_alias} ON {so_alias}.so_id = {sol_alias}.so_id '
                               f'JOIN product_master {pm_alias} ON {sol_alias}.product_id = {pm_alias}.product_id')
                if 'WHERE' in sql.upper():
                    sql = re.sub(r'(\bWHERE\b)', f'{join_clause} WHERE', sql, count=1, flags=re.IGNORECASE)
                else:
                    sql += f' {join_clause}'

        conditions.append(f"{pm_alias}.product_name = '{prod_val}'")

    # ── Customer filter ───────────────────────────────────────────────
    if filters.get('customer'):
        cust_val = filters['customer'].replace("'", "''")
        cm_match = re.search(r'\bcustomer_master\s+(\w+)', sql, re.IGNORECASE)
        if cm_match:
            cm_alias = cm_match.group(1)
        else:
            cm_alias = 'cm'
            so_alias_m = re.search(r'\bsales_order\b(?!_)\s+(\w+)', sql, re.IGNORECASE)
            if so_alias_m:
                so_alias = so_alias_m.group(1)
                if 'WHERE' in sql.upper():
                    sql = re.sub(
                        r'(\bWHERE\b)',
                        f'JOIN customer_master {cm_alias} ON {so_alias}.customer_id = {cm_alias}.customer_id WHERE',
                        sql, count=1, flags=re.IGNORECASE
                    )
                else:
                    sql += f' JOIN customer_master {cm_alias} ON {so_alias}.customer_id = {cm_alias}.customer_id'

        conditions.append(f"{cm_alias}.customer_name = '{cust_val}'")

    # ── Apply collected conditions ────────────────────────────────────
    if conditions:
        cond_str = ' AND '.join(conditions)
        if re.search(r'\bWHERE\b', sql, re.IGNORECASE):
            # Find the position right after WHERE and its existing conditions
            # Insert before GROUP BY / ORDER BY / LIMIT if present
            for keyword in ['GROUP BY', 'ORDER BY', 'LIMIT', 'HAVING']:
                pattern = re.compile(rf'\b{keyword}\b', re.IGNORECASE)
                match = pattern.search(sql)
                if match:
                    insert_pos = match.start()
                    sql = sql[:insert_pos] + f'AND {cond_str} ' + sql[insert_pos:]
                    break
            else:
                # No GROUP BY/ORDER BY/LIMIT — just append
                sql += f' AND {cond_str}'
        else:
            # No WHERE clause at all — insert before GROUP BY etc. or append
            for keyword in ['GROUP BY', 'ORDER BY', 'LIMIT', 'HAVING']:
                pattern = re.compile(rf'\b{keyword}\b', re.IGNORECASE)
                match = pattern.search(sql)
                if match:
                    insert_pos = match.start()
                    sql = sql[:insert_pos] + f'WHERE {cond_str} ' + sql[insert_pos:]
                    break
            else:
                sql += f' WHERE {cond_str}'

    return sql


# ── Report generation ──────────────────────────────────────────────────────

class ReportPipeline:
    """Generates a complete analytics report from a natural-language request."""

    def __init__(self, provider: str = "groq"):
        self.provider = provider
        self._lm = get_lm(provider)
        self.report_gen = dspy.Predict(ReportGeneration)
        self.report_mod = dspy.Predict(ReportModification)
        self.repair = dspy.Predict(SQLRepair)
        # Chat pipeline modules for SQL quality improvement
        self.analyze = dspy.Predict(AnalyzeAndPlan)
        self.sql_gen = dspy.Predict(SQLGeneration)
        # Per-report regeneration counter to prevent getting stuck
        self._regen_count = 0
        self._MAX_REGEN_PER_REPORT = 3

    # ── Shared SQL validation + execution (mirrors SQLAnalystPipeline) ──

    @staticmethod
    def _clean_sql(raw: str) -> str:
        """Strip markdown fences, trailing prose, and whitespace from LLM SQL."""
        sql = raw.strip()
        if sql.startswith("```"):
            lines = sql.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            sql = "\n".join(lines).strip()
        match = re.search(
            r"((?:SELECT|WITH)\b[\s\S]*?)(;|\n\n(?=[A-Z][a-z])|$)",
            sql, re.IGNORECASE,
        )
        if match:
            sql = match.group(1).strip()
        sql = sql.rstrip(";")
        return sql

    def _validate_and_execute_sql(self, sql: str, context: str = "report", sql_description: str = "") -> tuple:
        """Validate and execute SQL using the same pipeline as SQL Chat.

        Applies the full validation chain:
        1. Auto-correct known LLM mistakes (_fix_report_sql)
        2. Schema validation (check_sql_against_schema)
        3. Structural pattern checker (check_sql_patterns)
        4. Safety validation (validate_sql)
        5. Execute with repair loop (up to 2 retries on DB errors)

        Returns (corrected_sql, result_dict) where result_dict has
        'success', 'data', 'error' keys.
        """
        # Step 1: Auto-correct known regex patterns
        sql = _fix_report_sql(sql)

        # Step 2: Schema validation
        try:
            schema = get_schema()
            schema_valid, schema_issues = check_sql_against_schema(sql, schema)
            if not schema_valid:
                logger.warning("[%s] Schema issues: %s", context, schema_issues)
        except Exception as exc:
            logger.warning("Schema check failed: %s", exc)

        # Step 3: Structural pattern checker
        try:
            pattern_issues = check_sql_patterns(sql)
            if pattern_issues:
                logger.warning(
                    "[%s] Pattern issues: %s",
                    context,
                    [i["pattern_name"] for i in pattern_issues],
                )
        except Exception as exc:
            logger.warning("Pattern check failed: %s", exc)

        # Step 4: Safety validation
        is_safe, reason = validate_sql(sql)
        if not is_safe:
            return sql, {"success": False, "data": [], "error": f"Query rejected: {reason}"}

        # Step 5: Execute with repair loop
        result = execute_sql(sql)

        for attempt in range(MAX_REPAIR_RETRIES):
            if result["success"]:
                break
            logger.warning(
                "[%s] SQL error (attempt %d): %s", context, attempt + 1, result["error"]
            )
            try:
                schema_str = format_schema()
                # Build a context-aware repair question
                repair_question = f"Fix this SQL for a {context} query"
                if "GroupingError" in result["error"] or "ungrouped column" in result["error"]:
                    repair_question += (
                        ". The error is a correlated subquery GroupingError. "
                        "Do NOT use correlated subqueries that reference outer query aliases. "
                        "Rewrite as a simple flat aggregate: "
                        "SELECT SUM(solp.line_total) AS value FROM ... JOIN ... WHERE ..."
                    )
                repair_result = self.repair(
                    sql_query=sql,
                    error_message=result["error"],
                    schema_info=schema_str,
                    question=repair_question,
                )
                sql = self._clean_sql(repair_result.corrected_sql)
                sql = _fix_report_sql(sql)  # re-apply regex fixes after repair

                is_safe, reason = validate_sql(sql)
                if not is_safe:
                    return sql, {"success": False, "data": [], "error": f"Repaired query rejected: {reason}"}

                result = execute_sql(sql)
            except Exception as exc:
                logger.error("[%s] Repair attempt %d failed: %s", context, attempt + 1, exc)
                break

        # ── Final fallback: full SQL regeneration via chat pipeline ─────
        if not result["success"] and sql_description and self._regen_count < self._MAX_REGEN_PER_REPORT:
            self._regen_count += 1
            logger.info("[%s] Repair failed — attempting full SQL regeneration (%d/%d)",
                        context, self._regen_count, self._MAX_REGEN_PER_REPORT)
            new_sql = self._regenerate_sql(sql_description)
            if new_sql:
                is_safe, reason = validate_sql(new_sql)
                if is_safe:
                    regen_result = execute_sql(new_sql)
                    if regen_result["success"]:
                        logger.info("[%s] SQL regeneration succeeded", context)
                        return new_sql, regen_result
                    else:
                        logger.warning("[%s] Regenerated SQL also failed: %s", context, regen_result["error"])
        elif not result["success"] and sql_description and self._regen_count >= self._MAX_REGEN_PER_REPORT:
            logger.warning("[%s] Skipping regeneration — limit reached (%d/%d)",
                           context, self._regen_count, self._MAX_REGEN_PER_REPORT)

        return sql, result

    @staticmethod
    def _build_question_with_context(question: str) -> str:
        today = date.today()
        current_year = today.year
        last_year = current_year - 1
        return (
            f"[CONTEXT: Today is {today.isoformat()}. "
            f"Current year = {current_year}. "
            f"'Last year' = {last_year} ({last_year}-01-01 to {last_year}-12-31). "
            f"'This year' = {current_year} ({current_year}-01-01 to {current_year}-12-31).]\n\n"
            f"{question}"
        )

    def _pre_analyze_query(self, question: str, schema_str: str,
                           rels_str: str, profile_str: str) -> str:
        """Use the chat pipeline's AnalyzeAndPlan to produce a SQL generation guide.

        This gives the report LLM concrete, schema-verified guidance for
        writing correct SQL queries without affecting the report structure.
        Returns a text block to append to the LLM prompt.
        """
        try:
            question_with_date = self._build_question_with_context(question)
            plan = self.analyze(
                question=question_with_date,
                schema_info=schema_str,
                relationships=rels_str,
                data_profile=profile_str,
            )

            guide = [
                "",
                "══════════════════════════════════════════════════",
                "📋 SQL GENERATION GUIDE (use for ALL SQL queries in the report)",
                "══════════════════════════════════════════════════",
                f"Analysis intent: {plan.intent}",
                f"Relevant tables: {plan.relevant_tables}",
                f"Relevant columns: {plan.relevant_columns}",
                f"Join conditions: {plan.join_conditions}",
                f"Where conditions: {plan.where_conditions}",
                f"Aggregations: {plan.aggregations}",
                f"Group by: {plan.group_by}",
                "",
                "⚠ Use ONLY the tables and columns listed above in your SQL.",
                "⚠ Follow the join conditions EXACTLY as specified.",
                "⚠ Apply the where conditions in ALL SQL queries.",
                "⚠ Adapt these for each KPI/chart but keep tables and joins correct.",
                "══════════════════════════════════════════════════",
            ]

            result = "\n".join(guide)
            logger.info("Pre-analysis SQL guide generated (%d chars)", len(result))
            return result
        except Exception as exc:
            logger.warning("Pre-analysis failed (non-fatal, continuing without guide): %s", exc)
            return ""

    @staticmethod
    def _get_analytical_framework(question: str) -> str:
        """Provide high-level analytical guidance based on the report theme.

        Tells the report LLM WHAT to analyze (conceptual metrics/dimensions)
        without prescribing HOW (no SQL examples — the schema and pre-analysis
        handle that). This prevents hallucination while ensuring relevant metrics.
        """
        q = question.lower().strip()
        q = re.sub(r'\[active filters:.*?\]', '', q, flags=re.IGNORECASE)
        q = re.sub(r'\[context:.*?\]', '', q, flags=re.IGNORECASE).strip()

        # Theme → (keywords, focus area, metric hints, anti-patterns)
        themes = {
            "customer": {
                "kw": ["customer", "buyer", "buying pattern", "purchase pattern",
                       "customer behavior", "customer analysis", "retention",
                       "churn", "loyalty", "repeat", "rfm", "customer value",
                       "top 10 customer", "top 5 customer", "top 20 customer",
                       "top customer", "best customer", "biggest customer",
                       "customer domain", "customer segment",
                       "customer spending", "spending", "spend", "spender",
                       "top spender", "biggest spender", "customer spend"],
                "focus": "CUSTOMER behavior, segments, spending patterns, and customer-level metrics",
                "metrics": [
                    "Total number of unique customers",
                    "Total customer revenue / spending (SUM of closed order totals)",
                    "Average order value (AOV) — total revenue ÷ total orders",
                    "Average number of orders per customer",
                    "Average spend per customer (total revenue ÷ unique customers)",
                    "Customer repeat purchase rate (% of customers with 2+ orders)",
                    "Customer revenue ranking (horizontalBar — top 10 customer names vs total spend)",
                    "Customer spending trend over time (line chart — monthly revenue)",
                    "Customer product category preferences (bar — category vs total spend)",
                    "New vs returning customer split (doughnut — NEW=1 order, RETURNING=2+ orders)",
                    "Customer concentration — top 10 vs rest (pie — top 10 share vs others)",
                    "Customer order frequency distribution (bar — orders-per-customer buckets)",
                ],
                "sql_notes": (
                    "CRITICAL SQL NOTES for this report:\n"
                    "Base table: sales_order (alias: so), JOIN sales_order_line (alias: sol) ON so.so_id = sol.so_id\n"
                    "Always filter: WHERE so.status = 'closed'\n\n"
                    "• Total Unique Customers: SELECT COUNT(DISTINCT so.customer_id) AS value FROM sales_order so WHERE so.status = 'closed'\n"
                    "• Total Customer Revenue: SELECT SUM(so.total_amount) AS value FROM sales_order so WHERE so.status = 'closed'\n"
                    "• Average Order Value (AOV): SELECT ROUND(AVG(so.total_amount), 2) AS value FROM sales_order so WHERE so.status = 'closed'\n"
                    "• Average Orders per Customer: SELECT ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT so.customer_id), 0), 2) AS value FROM sales_order so WHERE so.status = 'closed'\n"
                    "• Average Spend per Customer: SELECT ROUND(SUM(so.total_amount) / NULLIF(COUNT(DISTINCT so.customer_id), 0), 2) AS value FROM sales_order so WHERE so.status = 'closed'\n"
                    "• Customer Repeat Purchase Rate (%): "
                    "SELECT ROUND(100.0 * COUNT(DISTINCT CASE WHEN order_count > 1 THEN customer_id END) "
                    "/ NULLIF(COUNT(DISTINCT customer_id), 0), 2) AS value "
                    "FROM (SELECT customer_id, COUNT(*) AS order_count FROM sales_order WHERE status = 'closed' GROUP BY customer_id) t\n"
                    "• New vs Returning split (for CHART only — DO NOT use as KPI): "
                    "SELECT CASE WHEN order_count = 1 THEN 'New' ELSE 'Returning' END AS customer_type, "
                    "COUNT(*) AS customer_count "
                    "FROM (SELECT customer_id, COUNT(*) AS order_count FROM sales_order WHERE status = 'closed' GROUP BY customer_id) t "
                    "GROUP BY customer_type ORDER BY customer_type\n"
                    "• Top customers by spend (for CHART): "
                    "SELECT cm.customer_name, SUM(so.total_amount) AS total_spend "
                    "FROM sales_order so JOIN customer_master cm ON so.customer_id = cm.customer_id "
                    "WHERE so.status = 'closed' GROUP BY cm.customer_name ORDER BY total_spend DESC LIMIT 10\n"
                    "• NEVER use customer_id alone — always JOIN customer_master for the name column in charts."
                ),
                "avoid": "generic Total Revenue, generic Top Products, generic Category Distribution, generic Revenue by Region — EVERY KPI label and chart title MUST include the word 'Customer'. Charts MUST show customer-level data on the X-axis (customer names, customer segments, etc.), NOT product names or regions",
            },
            "sales": {
                "kw": ["sales report", "revenue report", "sales analysis", "revenue analysis",
                       "sales performance", "revenue performance", "total sales", "total revenue",
                       "sales overview", "revenue overview", "sales dashboard", "revenue dashboard",
                       "sales summary", "revenue summary", "monthly sales", "monthly revenue",
                       "yearly sales", "yearly revenue", "quarterly sales"],
                "focus": "SALES and REVENUE performance, trends, and key business metrics",
                "metrics": [
                    "Total revenue (sum of all completed orders)",
                    "Total number of orders placed",
                    "Average order value (AOV)",
                    "Total units sold",
                    "Revenue trend over time (monthly line chart)",
                    "Revenue by product category (bar/pie chart)",
                    "Top products by revenue (horizontal bar chart)",
                    "Top customers by revenue (horizontal bar chart)",
                    "Order volume trend (line chart — orders per month)",
                    "Revenue vs order count correlation (dual-axis or stacked bar)",
                    "Sales status distribution (pie/doughnut — completed vs cancelled etc.)",
                    "Revenue by sales channel or region (bar chart)",
                ],
                "avoid": "inventory, procurement, or vendor metrics that are unrelated to sales — every KPI and chart must be SALES/REVENUE-centric",
            },
            "product": {
                "kw": ["product analysis", "product performance", "product report",
                       "best selling", "product mix", "sku analysis", "variant",
                       "top 10 product", "top 5 product", "top product"],
                "focus": "PRODUCT-level performance and comparison",
                "metrics": [
                    "Total products sold", "Best/worst selling products",
                    "Average revenue per product", "Product category breakdown",
                    "Price point distribution", "Product sales trend",
                    "Top products by volume vs revenue", "Product margin analysis",
                ],
                "avoid": "generic order/customer metrics — every KPI and chart must be PRODUCT-centric",
            },
            "inventory": {
                "kw": ["inventory", "stock", "overstock", "understock",
                       "stock level", "warehouse", "finished goods"],
                "focus": "INVENTORY levels, stock health, and turnover",
                "metrics": [
                    "Total SKUs in stock", "Overstocked items count",
                    "Low/out-of-stock items", "Stock value by category",
                    "Stock health distribution", "Top overstocked SKUs",
                    "Stock turnover indicators", "Category-wise stock levels",
                ],
                "avoid": "revenue or customer metrics — every KPI and chart must be INVENTORY-centric",
            },
            "vendor": {
                "kw": ["vendor", "purchase order", "supplier", "procurement",
                       "vendor analysis", "po analysis",
                       "top 10 vendor", "top vendor", "best vendor"],
                "focus": "VENDOR performance and procurement analysis",
                "metrics": [
                    "Total active vendors", "Total PO value", "Average PO value",
                    "Top vendors by value", "PO volume trend",
                    "Vendor concentration", "PO status distribution",
                ],
                "avoid": "sales order or customer metrics — every KPI and chart must be VENDOR/PROCUREMENT-centric",
            },
            "material": {
                "kw": ["gold", "diamond", "karat", "carat", "material",
                       "making charges", "material cost", "component cost"],
                "focus": "MATERIAL and component cost breakdown",
                "metrics": [
                    "Total gold cost (SUM of gold_amount_per_unit × quantity)",
                    "Total diamond cost (SUM of diamond_amount_per_unit × quantity)",
                    "Total making charges (SUM of making_charges_per_unit × quantity)",
                    "Gold cost by karat type (gold_kt from sales_order_line_gold)",
                    "Material cost trend over time (monthly line chart)",
                    "Top products by gold cost (horizontal bar)",
                    "Cost component ratio — gold vs diamond vs making charges (pie/doughnut)",
                    "Gold weight distribution by karat (bar chart)",
                ],
                "sql_notes": (
                    "CRITICAL SQL NOTES for this report:\n"
                    "• gold_amount_per_unit, diamond_amount_per_unit, making_charges_per_unit\n"
                    "  are columns on sales_order_line_pricing (alias: lp) — use them directly.\n"
                    "• Total Gold Cost KPI: SELECT SUM(lp.gold_amount_per_unit * sol.quantity) AS value\n"
                    "  FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id\n"
                    "  JOIN sales_order_line_pricing lp ON sol.sol_id = lp.sol_id WHERE so.status = 'closed'\n"
                    "• Total Diamond Cost KPI: same structure but SUM(lp.diamond_amount_per_unit * sol.quantity)\n"
                    "• Total Making Charges KPI: same structure but SUM(lp.making_charges_per_unit * sol.quantity)\n"
                    "• NEVER use SUM(lp.line_total) for a specific component — line_total = ALL costs combined.\n"
                    "• gold_kt (karat type) lives on sales_order_line_gold (alias: solg) — join it for karat charts.\n"
                    "• diamond_type lives on sales_order_line_diamond (alias: sold) — join for diamond type charts."
                ),
                "avoid": "SUM(line_total) for any single component cost; generic revenue metrics unrelated to materials",
            },
            "order": {
                "kw": ["order analysis", "order pattern", "order report",
                       "order trend", "order frequency", "order status"],
                "focus": "ORDER-level patterns and fulfillment",
                "metrics": [
                    "Total orders", "Average order value", "Order frequency trend",
                    "Cancellation rate", "Average items per order",
                    "Order status distribution", "Order value distribution",
                    "Peak ordering periods",
                ],
                "avoid": "product-level or customer-level detail — focus on ORDER metrics",
            },
            "aov": {
                "kw": ["aov", "average order value", "order value", "basket size",
                       "basket value", "avg order", "average order", "per order"],
                "focus": "AVERAGE ORDER VALUE (AOV) analysis, trends, and segmentation",
                "metrics": [
                    "Overall AOV (total revenue ÷ total orders)",
                    "Median Order Value (use PERCENTILE_CONT(0.5) WITHIN GROUP)",
                    "Highest Single Order Value (MAX of total_amount)",
                    "Orders Above Average (count of orders > overall AOV)",
                    "AOV Year-over-Year Change % (calculate actual percentage)",
                    "Top Product by AOV Contribution (product NAME, not count)",
                    "AOV by product category (bar chart — category vs avg order value)",
                    "AOV by customer tier (horizontalBar — top/mid/low spending groups)",
                    "AOV trend over time (line — monthly AOV, ONLY if 6+ months)",
                    "Order value distribution (doughnut — order size buckets: <1L, 1-5L, 5-10L, >10L)",
                    "Top 10 products by AOV contribution (horizontalBar)",
                    "AOV comparison by order status (bar — closed vs open vs processing)",
                ],
                "sql_notes": (
                    "CRITICAL SQL NOTES for AOV reports:\n"
                    "• AOV = ROUND(SUM(so.total_amount) / NULLIF(COUNT(*), 0), 2) AS value\n"
                    "  FROM sales_order so WHERE so.status = 'closed'\n"
                    "• AOV by category: SELECT pm.category, ROUND(AVG(so.total_amount), 2) AS avg_order_value\n"
                    "  FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id\n"
                    "  JOIN product_master pm ON sol.product_id = pm.product_id\n"
                    "  WHERE so.status = 'closed' GROUP BY pm.category ORDER BY avg_order_value DESC\n"
                    "• Order value buckets: SELECT CASE\n"
                    "    WHEN so.total_amount < 100000 THEN 'Below ₹1L'\n"
                    "    WHEN so.total_amount < 500000 THEN '₹1L-5L'\n"
                    "    WHEN so.total_amount < 1000000 THEN '₹5L-10L'\n"
                    "    ELSE 'Above ₹10L' END AS order_bucket, COUNT(*) AS order_count\n"
                    "  FROM sales_order so WHERE so.status = 'closed' GROUP BY order_bucket ORDER BY order_count DESC\n"
                    "• YoY AOV change: Use subqueries for current vs previous year, calculate % difference\n"
                    "• For February-specific queries: add WHERE EXTRACT(MONTH FROM so.order_date) = 2\n"
                    "• NEVER return raw counts as KPI values for 'Top Product' — return the NAME"
                ),
                "avoid": "generic revenue/order count metrics that ignore AOV — every KPI and chart must be about ORDER VALUE, not just counts or totals. Do NOT make all charts 'by year' — show different dimensions",
            },
            "comparison": {
                "kw": ["compare", "comparison", "versus", "vs", "across",
                       "between", "difference", "contrast", "benchmark"],
                "focus": "COMPARATIVE analysis across multiple dimensions, periods, or segments",
                "metrics": [
                    "Metric value for period/segment A vs B",
                    "Percentage difference between compared items",
                    "Absolute change (delta) between segments",
                    "Best performing segment/period (return NAME, not count)",
                    "Worst performing segment/period (return NAME, not count)",
                    "Growth rate % between compared periods",
                    "Side-by-side comparison (bar chart — grouped by dimension)",
                    "Trend comparison (line chart — multiple series, only if 6+ points)",
                    "Share breakdown (pie/doughnut — proportion of each segment)",
                    "Ranking of compared items (horizontalBar)",
                    "Category-level comparison (stackedBar — multi-series)",
                    "Distribution across segments (doughnut)",
                ],
                "sql_notes": (
                    "CRITICAL SQL NOTES for comparison reports:\n"
                    "• Use GROUP BY for the compared dimension (year, month, category, etc.)\n"
                    "• For year-over-year: EXTRACT(YEAR FROM so.order_date) AS year\n"
                    "• For month filtering: EXTRACT(MONTH FROM so.order_date) = N\n"
                    "• For growth %: ROUND(100.0 * (new_val - old_val) / NULLIF(old_val, 0), 2)\n"
                    "• For best/worst KPIs: use ORDER BY + LIMIT 1 and return the NAME/label\n"
                    "• IMPORTANT: Do NOT make all 6 charts show the same dimension (e.g. all 'by year').\n"
                    "  Instead, compare across DIFFERENT angles: by year, by category, by customer,\n"
                    "  by product, by order size, etc.\n"
                    "• Use bar charts for ≤5 comparison items, NOT line charts"
                ),
                "avoid": "making all charts identical (all 'by year') — each chart must compare a DIFFERENT dimension. Do NOT use line/area for ≤5 data points.",
            },
        }

        # Score all themes and pick the top 2 matching ones
        scored = []
        for tid, t in themes.items():
            score = sum(1 for kw in t["kw"] if kw in q)
            if score > 0:
                scored.append((score, tid))
        scored.sort(reverse=True)

        if not scored:
            return ""

        # Combine top 2 frameworks (e.g., "compare AOV" → aov + comparison)
        matched_ids = [s[1] for s in scored[:2]]

        result_parts = []
        for mid in matched_ids:
            t = themes[mid]
            metrics_list = "\n".join(f"  • {m}" for m in t["metrics"])
            sql_notes_block = ""
            if t.get("sql_notes"):
                sql_notes_block = f"\n📌 SQL FORMULAS — FOLLOW EXACTLY:\n{t['sql_notes']}\n"
            part = (
                f"\n══════════════════════════════════════════════════\n"
                f"🚨 MANDATORY REPORT FOCUS: {t['focus']}\n"
                f"══════════════════════════════════════════════════\n"
                f"YOU MUST generate KPIs and charts from this list:\n"
                f"{metrics_list}\n"
                f"{sql_notes_block}\n"
                f"🚫 STRICTLY FORBIDDEN: {t['avoid']}\n"
                f"🚨 Every KPI label and chart title MUST relate to: {t['focus']}\n"
                f"🚨 If a KPI or chart does NOT directly measure {mid.upper()}-level data, DELETE it and replace with one from the list above.\n"
                f"══════════════════════════════════════════════════"
            )
            result_parts.append(part)

        logger.info("Analytical framework(s): %s", ", ".join(f"{mid}(score={s})" for s, mid in scored[:2]))

        # ── Universal KPI quality enforcement (appended to ALL frameworks) ──
        universal_kpi_rules = (
            "\n══════════════════════════════════════════════════"
            "\n⛔ MANDATORY KPI QUALITY RULES (APPLY TO ALL REPORTS)"
            "\n══════════════════════════════════════════════════"
            "\nThe following KPI labels are PERMANENTLY BANNED:"
            "\n  ✗ 'Revenue Growth' — growth requires a baseline comparison; use 'Total Revenue' instead"
            "\n  ✗ 'Sales Growth' — same reason; use 'Total Sales Value' instead"
            "\n  ✗ 'Top-Selling Product Category' — this is a NAME, not a scalar; use 'Product Categories Count' instead"
            "\n  ✗ 'Top-Selling Product' — this is a list/ranking metric, not a KPI"
            "\n  ✗ Any KPI with 'Growth' in the label UNLESS the SQL calculates an actual % change"
            "\n  ✗ Any KPI with 'Trend' in the label — trends are charts, not single values"
            "\n  ✗ Any KPI with 'Distribution' or 'Breakdown' — these are chart metrics"
            "\n"
            "\nEACH of the 6 KPIs MUST:"
            "\n  ✓ Return a UNIQUE numeric value (no two KPIs may share the same number)"
            "\n  ✓ Use a DIFFERENT SQL query (different aggregate function or different WHERE clause)"
            "\n  ✓ Be a meaningful scalar: COUNT, SUM, AVG, MAX, MIN, or a calculated RATIO"
            "\n  ✓ Have a label that clearly describes what the NUMBER represents"
            "\n"
            "\nGOOD KPI examples: Total Revenue, Total Orders, Average Order Value, "
            "Unique Customers, Total Quantity Sold, Order Fulfillment Rate (%)"
            "\nBAD KPI examples: Revenue Growth, Sales Growth, Top-Selling Product, "
            "Revenue Trend, Category Distribution"
            "\n══════════════════════════════════════════════════"
        )
        result_parts.append(universal_kpi_rules)

        return "\n".join(result_parts)

    def _regenerate_sql(self, sql_description: str) -> str | None:
        """Regenerate a SQL query using the chat pipeline's full chain.

        Used as a last resort when auto-fix and repair both fail.
        Uses AnalyzeAndPlan → SQLGeneration (same chain as SQL chat).
        """
        try:
            schema_str = getattr(self, '_report_schema_str', None) or format_schema()
            rels_str = getattr(self, '_report_rels_str', None) or format_relationships()
            profile_str = getattr(self, '_report_profile_str', None) or get_data_profile()

            question_with_date = self._build_question_with_context(sql_description)

            plan = self.analyze(
                question=question_with_date,
                schema_info=schema_str,
                relationships=rels_str,
                data_profile=profile_str,
            )

            plan_text = (
                f"Intent: {plan.intent}\n"
                f"Tables: {plan.relevant_tables}\n"
                f"Columns: {plan.relevant_columns}\n"
                f"Joins: {plan.join_conditions}\n"
                f"Where: {plan.where_conditions}\n"
                f"Aggregations: {plan.aggregations}\n"
                f"Group By: {plan.group_by}\n"
                f"Order By: {plan.order_by}\n"
                f"Limit: {plan.limit_val}"
            )

            sql_result = self.sql_gen(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text,
            )

            sql = self._clean_sql(sql_result.sql_query)
            sql = _fix_report_sql(sql)
            logger.info("SQL regenerated via chat pipeline: %s", sql[:200])
            return sql
        except Exception as exc:
            logger.warning("SQL regeneration via chat pipeline failed: %s", exc)
            return None

    @staticmethod
    def _repair_json(text: str) -> str:
        """Best-effort repair of common LLM JSON generation errors.

        Handles:
        1. Single-quoted keys/values -> double-quoted
        2. Literal newlines / tabs / carriage-returns inside string values
        3. Trailing commas before } or ]
        4. Truncated JSON (missing closing braces/brackets)
        """
        # ── Pass 0: if the text looks like Python dict (single quotes), convert ──
        # Only attempt if there is no " in the text but there are '
        if "'" in text and '"' not in text:
            import re as _re0
            # Replace 'key': pattern
            text = _re0.sub(r"(?<=[{,\[]\s*)'", '"', text)
            text = _re0.sub(r"'(?=\s*[:\}\],])", '"', text)

        # ── Pass 1: escape unescaped control chars inside string literals ──
        result: list[str] = []
        in_string = False
        escape_next = False

        for ch in text:
            if escape_next:
                result.append(ch)
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                result.append(ch)
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                if ch == "\n":
                    result.append("\\n")
                elif ch == "\r":
                    result.append("\\r")
                elif ch == "\t":
                    result.append("\\t")
                else:
                    result.append(ch)
            else:
                result.append(ch)

        text = "".join(result)

        # ── Pass 2: remove trailing commas before } or ] ───────────────────
        import re as _re
        text = _re.sub(r",(\s*[}\]])", r"\1", text)

        # ── Pass 3: close any truncated JSON ──────────────────────────────
        # Count unmatched { and [
        depth_brace = 0
        depth_bracket = 0
        in_str = False
        esc = False
        for ch in text:
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "{":
                    depth_brace += 1
                elif ch == "}":
                    depth_brace -= 1
                elif ch == "[":
                    depth_bracket += 1
                elif ch == "]":
                    depth_bracket -= 1

        # If we ended mid-string, close it first
        if in_str:
            text += '"'
        # Close any open arrays before open objects
        if depth_bracket > 0:
            text += "]" * depth_bracket
        if depth_brace > 0:
            text += "}" * depth_brace

        return text

    @staticmethod
    def _extract_json(raw: str) -> dict:
        """Extract and parse JSON from LLM output with multi-stage repair."""
        text = raw.strip()

        # ── Strip markdown code fences ────────────────────────────────────
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # ── Find JSON object boundaries ───────────────────────────────────
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        # ── Stage 1: direct parse ─────────────────────────────────────────
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # ── Stage 2: repair then parse ────────────────────────────────────
        repaired = ReportPipeline._repair_json(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        # ── Stage 3: re-extract boundaries after repair and retry ─────────
        start = repaired.find("{")
        end = repaired.rfind("}")
        if start != -1 and end != -1 and end > start:
            repaired = repaired[start:end + 1]

        return json.loads(repaired)  # let the caller handle any final exception

    @staticmethod
    def _fix_kpi_sql(sql: str) -> str:
        """Detect and fix KPI SQL that returns multiple rows instead of a single aggregate.

        Common LLM mistakes:
        - SELECT name, SUM(x) ... GROUP BY name ORDER BY ... LIMIT 10  (ranking, not KPI)
        - SELECT col FROM ... LIMIT 5  (list, not aggregate)

        Fix strategy: wrap multi-row queries into a COUNT or SUM aggregate.
        """
        if not sql:
            return sql

        sql_upper = ' '.join(sql.upper().split())

        # Detect multi-row patterns: GROUP BY with multiple result rows
        has_group_by = 'GROUP BY' in sql_upper

        # Check for LIMIT > 1 (LIMIT 10, LIMIT 5, etc.)
        limit_match = re.search(r'LIMIT\s+(\d+)', sql_upper)
        has_multi_limit = limit_match and int(limit_match.group(1)) > 1

        # Check if the SELECT already looks like a single aggregate (no GROUP BY)
        # e.g., SELECT COUNT(*), SELECT SUM(x) — these are fine
        select_match = re.search(r'SELECT\s+(.*?)\s+FROM', sql_upper, re.DOTALL)
        if select_match:
            select_clause = select_match.group(1).strip()
            # If it's a pure aggregate (no comma-separated non-agg columns), it's fine
            agg_funcs = ['COUNT(', 'SUM(', 'AVG(', 'MIN(', 'MAX(', 'ROUND(']
            is_pure_aggregate = (
                any(select_clause.startswith(f) for f in agg_funcs)
                and ',' not in select_clause
                and not has_group_by
            )
            if is_pure_aggregate:
                return sql  # Already a proper KPI query

        # ── Detect correlated subquery (GroupingError source) ────────────────
        # Pattern: the SQL itself contains a nested SELECT that references outer
        # table aliases — PostgreSQL rejects this in a scalar context.
        # Fix: strip the inner subquery and rewrite as a flat aggregate.
        correlated_subq = re.search(
            r'\(\s*SELECT\b.*?\bWHERE\b.*?\b(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)',
            sql, re.IGNORECASE | re.DOTALL
        )
        if correlated_subq:
            # Best-effort: remove the problematic subquery clause entirely,
            # keeping the outer aggregate structure. The repair loop will
            # regenerate a clean version via LLM if this produces bad SQL.
            sql = re.sub(
                r'/\s*\(\s*SELECT\b[^)]+\)\s*/',
                '',
                sql, flags=re.IGNORECASE | re.DOTALL
            )
            sql_upper = ' '.join(sql.upper().split())
            has_group_by = 'GROUP BY' in sql_upper
            logger.info("KPI SQL auto-fix: stripped correlated subquery")

        if has_group_by or has_multi_limit:
            # This is a ranking/list query, not a KPI
            # Wrap it: SELECT COUNT(*) AS value FROM (original) sub
            # But first, try to detect the value column to SUM it instead
            if has_group_by and select_match:
                # Find the aggregate column (last column in SELECT)
                cols = select_match.group(1).strip()
                # Try to find the aggregate expression
                agg_match = re.search(
                    r'(SUM|COUNT|AVG|MIN|MAX)\s*\([^)]+\)',
                    cols, re.IGNORECASE
                )
                if agg_match:
                    agg_expr = agg_match.group(0)
                    # Remove the GROUP BY and everything after, replace SELECT
                    # to get a total aggregate
                    fixed = re.sub(
                        r'SELECT\s+.*?\s+FROM',
                        f'SELECT {agg_expr} AS value FROM',
                        sql, count=1, flags=re.IGNORECASE | re.DOTALL
                    )
                    fixed = re.sub(
                        r'\s+GROUP\s+BY\s+.*$',
                        '', fixed, flags=re.IGNORECASE
                    )
                    fixed = re.sub(
                        r'\s+ORDER\s+BY\s+.*$',
                        '', fixed, flags=re.IGNORECASE
                    )
                    fixed = re.sub(
                        r'\s+LIMIT\s+\d+',
                        '', fixed, flags=re.IGNORECASE
                    )
                    logger.info("KPI SQL auto-fix: converted ranking query to aggregate")
                    return fixed.strip()

            # Fallback: wrap the entire query in a COUNT
            clean_sql = sql.rstrip(';').strip()
            wrapped = f"SELECT COUNT(*) AS value FROM ({clean_sql}) _kpi_sub"
            logger.info("KPI SQL auto-fix: wrapped multi-row query in COUNT(*)")
            return wrapped

        return sql

    @staticmethod
    def _format_indian(num: float) -> str:
        """Format a numeric value into Indian notation (crores/lakhs)."""
        abs_num = abs(num)
        if abs_num >= 1e7:
            return f"₹{num / 1e7:.2f} crores"
        if abs_num >= 1e5:
            return f"₹{num / 1e5:.2f} lakhs"
        return f"₹{num:,.2f}"

    def _regenerate_summary(self, original_summary: str, kpis: list) -> str:
        """Prepend actual KPI values to the report summary so the displayed
        text matches the SQL query results instead of LLM-estimated values."""
        valid_kpis = [
            k for k in kpis
            if isinstance(k.get("value"), (int, float))
        ]
        if not valid_kpis:
            return original_summary

        lines = ["Actual values from the database:"]
        for k in valid_kpis:
            label = k.get("label") or k.get("id") or "Metric"
            val = k["value"]
            fmt = k.get("format", "")
            if fmt == "percent":
                display = f"{val:.1f}%"
            elif fmt == "currency" or abs(val) >= 1e5:
                display = self._format_indian(val)
            elif isinstance(val, float):
                display = f"{val:,.2f}"
            else:
                display = f"{val:,}"
            lines.append(f"• {label}: {display}")

        actual_block = "\n".join(lines)
        return actual_block + "\n\n" + original_summary

    # ── Bad KPI label patterns (chart-type metrics, not single values) ──
    _BAD_KPI_RE = re.compile(
        r'\btop[\s-]+\d+\b'          # "Top 5 Vendors", "Top-10 Products"
        r'|\btop[\s-]+selling\b'     # "Top-Selling Products" — list metric
        r'|\bbest[\s-]+selling\b'    # "Best Selling" — list metric
        r'|\btrend\b'               # "Revenue Trend" — chart metric
        r'|\bgrowth\s+(?:trend|over|chart|timeline)\b'  # "Growth Trend/Over Time" — chart metric
        r'|\b(?:revenue|sales|order|customer|product|vendor|purchase)\s+growth\b'  # standalone "Revenue Growth" etc.
        r'|\bgrowth\s+rate\b'        # "Growth Rate" — unmeasurable as single scalar
        r'|\byear[- ]over[- ]year\b' # "Year-over-Year" without actual % calculation
        r'|\bmonth[- ]over[- ]month\b' # "Month-over-Month" without actual % calculation
        r'|\b(?:yoy|mom)\s+(?:growth|change|variance)\b'  # abbreviated growth patterns
        r'|\bdistribution\b'        # "Customer Distribution" — chart metric
        r'|\bbreakdown\b'           # "Category Breakdown" — chart metric
        r'|\bconcentration\b'       # "Vendor Concentration" — not a scalar
        r'|\bcomposition\b'         # "Revenue Composition" — chart metric
        r'|\branking\b'             # "Product Ranking" — list metric
        r'|\blist\b'                # "Product List" — not a KPI
        r'|\boverview\b'            # "Sales Overview" — too vague
        r'|\bby\s+\w+\b'            # "Revenue By Month" — chart metric
        r'|\bover\s+time\b'         # "Sales Over Time" — chart metric
        r'|\bmost\s+\w+\b'          # "Most Popular" — list metric
        r'|\bbottom\s+\d+\b',       # "Bottom 5" — list metric
        re.IGNORECASE,
    )

    # ── Semantic KPI label normalization (for synonym detection) ──
    _KPI_SYNONYMS = {
        'total': '', 'overall': '', 'aggregate': '', 'cumulative': '',
        'net': '', 'gross': '', 'all': '', 'entire': '',
    }

    @staticmethod
    def _normalize_kpi_label(label: str) -> str:
        """Normalize a KPI label to a canonical form for semantic comparison.

        Strips filler words so 'Total Revenue' == 'Overall Revenue' == 'Revenue'.
        """
        words = label.lower().strip().split()
        significant = [w for w in words if w not in ReportPipeline._KPI_SYNONYMS]
        return ' '.join(significant) if significant else label.lower().strip()

    @staticmethod
    def _sql_signature(sql: str) -> str:
        """Extract a signature from a SQL query for similarity comparison.

        Normalizes whitespace, removes aliases, and extracts key FROM/WHERE/GROUP.
        Two KPIs with the same signature are semantically redundant.
        """
        if not sql:
            return ''
        s = ' '.join(sql.upper().split())
        # Extract the core structure: FROM tables + WHERE conditions + aggregation
        parts = []
        from_match = re.search(r'FROM\s+(.+?)\s*(?:WHERE|GROUP|ORDER|LIMIT|$)', s)
        if from_match:
            parts.append('FROM:' + from_match.group(1).strip()[:80])
        where_match = re.search(r'WHERE\s+(.+?)\s*(?:GROUP|ORDER|LIMIT|$)', s)
        if where_match:
            parts.append('WHERE:' + where_match.group(1).strip()[:80])
        # Extract the aggregate function
        agg_match = re.search(r'(SUM|COUNT|AVG|MIN|MAX)\s*\([^)]+\)', s)
        if agg_match:
            parts.append('AGG:' + agg_match.group(0))
        return '|'.join(parts)

    def _clean_kpis(self, kpis: list) -> list:
        """Clean, validate and deduplicate KPIs.

        1. Remove KPIs with N/A, None or error values (zero-valued are held as fallback)
        2. Remove KPIs whose labels indicate chart-type metrics (top N, trend, etc.)
        3. Deduplicate KPIs with identical numeric values
        4. Guarantee at least MIN_KPIS=6 survive by restoring filtered KPIs in order:
           value-duplicates → bad-pattern KPIs → zero-valued KPIs → error KPIs (last resort)
        """
        MIN_KPIS = 6
        MAX_KPIS = 6
        original_count = len(kpis)

        _BAD_VALUES = {None, "", "N/A", "null", "None", "none", "n/a", "NaN", "nan"}

        # Step 1: separate non-zero, zero-valued, broken, and error KPIs
        non_zero: list = []
        zero_kpis: list = []
        error_kpis: list = []  # KPIs with SQL errors — last resort
        for k in kpis:
            v = k.get("value")
            if k.get("error"):
                error_kpis.append(k)
                continue
            if v is None:
                error_kpis.append(k)
                continue
            s = str(v).strip()
            if s in _BAD_VALUES:
                error_kpis.append(k)
                continue
            try:
                if float(v) == 0:
                    zero_kpis.append(k)
                    continue
            except (ValueError, TypeError):
                pass
            non_zero.append(k)

        # Step 2: reject bad-pattern labels; keep rejects as fallback candidates
        pattern_ok: list = []
        pattern_bad: list = []
        for k in non_zero:
            if self._BAD_KPI_RE.search(k.get("label", "")):
                pattern_bad.append(k)
            else:
                pattern_ok.append(k)

        if pattern_bad:
            logger.info("Removed %d bad-pattern KPIs (top-N/trend/distribution/breakdown)",
                        len(pattern_bad))

        # Step 3: deduplicate identical numeric values; keep rejects as fallback
        seen: dict = {}
        deduped: list = []
        value_dupes: list = []
        for k in pattern_ok:
            try:
                norm = round(float(k.get("value", 0)), 2)
            except (ValueError, TypeError):
                norm = k.get("value")
            if norm not in seen:
                seen[norm] = k.get("label", "?")
                deduped.append(k)
            else:
                logger.info("Duplicate KPI '%s' (same value as '%s')",
                            k.get("label", "?"), seen[norm])
                value_dupes.append(k)

        # Step 3b: semantic deduplication — detect synonym labels or similar SQL
        label_seen: dict = {}   # normalized_label → index in deduped
        sql_seen: dict = {}     # sql_signature → index in deduped
        semantic_dupes: list = []
        clean_deduped: list = []
        for k in deduped:
            norm_label = self._normalize_kpi_label(k.get("label", ""))
            sql_sig = self._sql_signature(k.get("sql", ""))

            # Check if a KPI with a very similar label already exists
            is_label_dupe = norm_label in label_seen and len(norm_label) > 3
            # Check if a KPI with the same SQL signature exists
            is_sql_dupe = sql_sig in sql_seen and len(sql_sig) > 10

            if is_label_dupe:
                logger.info("Semantic dedup: '%s' is synonym of '%s' (label match)",
                            k.get("label", "?"), deduped[label_seen[norm_label]].get("label", "?"))
                semantic_dupes.append(k)
            elif is_sql_dupe:
                logger.info("Semantic dedup: '%s' has same SQL signature as '%s'",
                            k.get("label", "?"), deduped[sql_seen[sql_sig]].get("label", "?"))
                semantic_dupes.append(k)
            else:
                if norm_label and len(norm_label) > 3:
                    label_seen[norm_label] = len(clean_deduped)
                if sql_sig and len(sql_sig) > 10:
                    sql_seen[sql_sig] = len(clean_deduped)
                clean_deduped.append(k)

        if semantic_dupes:
            logger.info("Removed %d semantically duplicate KPIs", len(semantic_dupes))
            # Add semantic dupes to value_dupes pool for fallback restoration
            value_dupes.extend(semantic_dupes)

        deduped = clean_deduped

        # Cap at MAX_KPIS to keep the report focused
        deduped = deduped[:MAX_KPIS]

        # ── Priority 0: Generate FRESH replacement KPIs via chat pipeline ─────
        # When bad/duplicate KPIs are removed, try to generate genuinely new ones
        # instead of recycling the same bad pool.
        if len(deduped) < MIN_KPIS:
            _replacement_ideas = [
                ("Total Quantity Sold", "SELECT SUM(sol.quantity) AS value FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id WHERE so.status = 'closed'"),
                ("Unique Products Sold", "SELECT COUNT(DISTINCT sol.product_id) AS value FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id WHERE so.status = 'closed'"),
                ("Unique Active Customers", "SELECT COUNT(DISTINCT so.customer_id) AS value FROM sales_order so WHERE so.status = 'closed'"),
                ("Average Line Total", "SELECT ROUND(AVG(solp.line_total)::numeric, 2) AS value FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id WHERE so.status = 'closed'"),
                ("Highest Single Order Value", "SELECT MAX(so.total_amount) AS value FROM sales_order so WHERE so.status = 'closed'"),
                ("Order Fulfillment Rate (%)", "SELECT ROUND(100.0 * COUNT(CASE WHEN status = 'closed' THEN 1 END) / NULLIF(COUNT(*), 0), 2) AS value FROM sales_order"),
                ("Total Product Categories", "SELECT COUNT(DISTINCT pm.category) AS value FROM product_master pm"),
                ("Average Items Per Order", "SELECT ROUND(AVG(item_count)::numeric, 2) AS value FROM (SELECT so.so_id, COUNT(sol.sol_id) AS item_count FROM sales_order so JOIN sales_order_line sol ON so.so_id = sol.so_id WHERE so.status = 'closed' GROUP BY so.so_id) sub"),
            ]

            # Collect existing label signatures to avoid duplicating
            _existing_labels = {self._normalize_kpi_label(k.get("label", "")) for k in deduped}
            _existing_values = set()
            for k in deduped:
                try:
                    _existing_values.add(round(float(k.get("value", 0)), 2))
                except (ValueError, TypeError):
                    pass

            _colors = ["blue", "green", "purple", "orange", "red", "teal"]
            _icons = ["revenue", "orders", "customers", "products", "growth", "average"]

            for idea_label, idea_sql in _replacement_ideas:
                if len(deduped) >= MIN_KPIS:
                    break
                # Skip if a similar label already exists
                norm = self._normalize_kpi_label(idea_label)
                if norm in _existing_labels:
                    continue

                # Execute the replacement SQL
                try:
                    idea_sql = _fix_report_sql(idea_sql)
                    result = execute_sql(idea_sql)
                    if not result["success"] or not result["data"]:
                        continue
                    row = result["data"][0]
                    val = list(row.values())[0] if row else None
                    if val is None:
                        continue
                    try:
                        float_val = round(float(val), 2)
                    except (ValueError, TypeError):
                        float_val = None

                    # Skip if this value already exists
                    if float_val is not None and float_val in _existing_values:
                        continue

                    idx = len(deduped)
                    new_kpi = {
                        "id": f"kpi_gen_{idx}",
                        "label": idea_label,
                        "sql": idea_sql,
                        "value": val,
                        "format": "percent" if "%" in idea_label or "rate" in idea_label.lower() else "number",
                        "icon": _icons[idx % len(_icons)],
                        "color": _colors[idx % len(_colors)],
                        "explanation": {
                            "what": f"Measures {idea_label.lower()}",
                            "how": "Calculated from sales data",
                            "why": "Provides additional business context",
                            "insight": f"Current value: {val}"
                        }
                    }
                    deduped.append(new_kpi)
                    _existing_labels.add(norm)
                    if float_val is not None:
                        _existing_values.add(float_val)
                    logger.info("Generated replacement KPI '%s' = %s", idea_label, val)
                except Exception as exc:
                    logger.warning("Replacement KPI '%s' failed: %s", idea_label, exc)

        # ── Guarantee MIN_KPIS — restore in priority order ────────────────
        if len(deduped) < MIN_KPIS:
            # Priority 1: value-duplicates that DON'T also have bad-pattern labels
            # (avoids restoring doubly-bad KPIs: duplicate value AND bad label)
            for k in value_dupes:
                if len(deduped) >= MIN_KPIS:
                    break
                if not self._BAD_KPI_RE.search(k.get("label", "")):
                    deduped.append(k)
                    logger.info("Restored value-duplicate KPI '%s' to meet minimum count",
                                k.get("label", "?"))

        if len(deduped) < MIN_KPIS:
            # Priority 2: bad-pattern KPIs — ONLY if their value is not already present
            # (prevents e.g. 'Top-Selling Products: ₹1031Cr' when 'Total Revenue: ₹1031Cr' exists)
            _existing_vals = set()
            for k in deduped:
                try:
                    _existing_vals.add(round(float(k.get("value", 0)), 2))
                except (ValueError, TypeError):
                    pass
            for k in pattern_bad:
                if len(deduped) >= MIN_KPIS:
                    break
                try:
                    kv = round(float(k.get("value", 0)), 2)
                except (ValueError, TypeError):
                    kv = None
                if kv not in _existing_vals:
                    deduped.append(k)
                    if kv is not None:
                        _existing_vals.add(kv)
                    logger.info("Restored bad-pattern KPI '%s' (unique value) to meet minimum count",
                                k.get("label", "?"))

        if len(deduped) < MIN_KPIS:
            # Priority 3: last resort — zero-valued KPIs with clean labels
            for k in zero_kpis:
                if len(deduped) >= MIN_KPIS:
                    break
                if not self._BAD_KPI_RE.search(k.get("label", "")):
                    deduped.append(k)
                    logger.info("Restored zero-valued KPI '%s' as last resort",
                                k.get("label", "?"))

        if len(deduped) < MIN_KPIS:
            # Priority 4: absolute last resort — any zero-valued KPI (even bad-pattern labels)
            for k in zero_kpis:
                if len(deduped) >= MIN_KPIS:
                    break
                if k not in deduped:
                    deduped.append(k)
                    logger.info("Restored zero-valued KPI '%s' (any label) as absolute last resort",
                                k.get("label", "?"))

        if len(deduped) < MIN_KPIS:
            # Priority 5: error KPIs — shown as 'Error' card in UI; better than a missing slot
            for k in error_kpis:
                if len(deduped) >= MIN_KPIS:
                    break
                deduped.append(k)
                logger.info("Restored error KPI '%s' to fill missing slot",
                            k.get("label", "?"))

        # ── Final pass: remove any duplicate-value KPIs that slipped through ──
        # Prefer the clean-label KPI when two share the same numeric value.
        final_seen: dict = {}   # norm_value → (list_index, is_bad_label)
        final_deduped: list = []
        for k in deduped:
            try:
                norm = round(float(k.get("value", 0)), 2)
            except (ValueError, TypeError):
                final_deduped.append(k)
                continue
            is_bad = bool(self._BAD_KPI_RE.search(k.get("label", "")))
            if norm not in final_seen:
                final_seen[norm] = (len(final_deduped), is_bad)
                final_deduped.append(k)
            elif is_bad:
                logger.info(
                    "Final dedup: dropped bad-label KPI '%s' (duplicate value of existing)",
                    k.get("label", "?"),
                )
            else:
                existing_idx, existing_is_bad = final_seen[norm]
                if existing_is_bad:
                    # Replace the earlier bad-label KPI with this cleaner one
                    final_deduped[existing_idx] = k
                    final_seen[norm] = (existing_idx, False)
                    logger.info(
                        "Final dedup: replaced bad-label KPI with cleaner '%s'",
                        k.get("label", "?"),
                    )
                else:
                    logger.info(
                        "Final dedup: dropped duplicate-value KPI '%s'",
                        k.get("label", "?"),
                    )
        # Floor: if final dedup left too few KPIs, restore clean-label KPIs
        # (e.g. all 6 KPIs computed identical values due to LLM SQL mistakes —
        # showing 2-3 "same value" KPIs is better than showing 1)
        _FINAL_DEDUP_FLOOR = 3
        if len(final_deduped) < _FINAL_DEDUP_FLOOR:
            for k in deduped:
                if len(final_deduped) >= _FINAL_DEDUP_FLOOR:
                    break
                if k not in final_deduped:
                    if not self._BAD_KPI_RE.search(k.get("label", "")):
                        final_deduped.append(k)
                        logger.info(
                            "Final dedup floor: restored clean-label KPI '%s' to reach minimum %d",
                            k.get("label", "?"), _FINAL_DEDUP_FLOOR,
                        )

        deduped = final_deduped

        if len(deduped) != original_count:
            logger.info("KPIs after cleanup: %d of %d valid", len(deduped), original_count)
        return deduped

    def _execute_kpi_sql(self, kpi: dict) -> dict:
        """Execute a KPI's SQL and populate its value.

        If the initial SQL returns zero/null/error, attempts regeneration
        via the chat pipeline's full chain for data accuracy.
        """
        sql = kpi.get("sql", "")
        if not sql:
            kpi["value"] = "N/A"
            kpi["error"] = "No SQL provided"
            return kpi

        # ── Fix column aliases and table references first (all 7 passes) ──
        sql = _fix_report_sql(sql)
        # ── Then convert ranking queries to scalar aggregates ───────────
        sql = self._fix_kpi_sql(sql)

        kpi_label = kpi.get("label", kpi.get("id", "?"))
        sql, result = self._validate_and_execute_sql(
            sql, context=f"KPI:{kpi_label}",
            sql_description=f"Calculate a single numeric value for: {kpi_label}. The SQL MUST return exactly ONE row with ONE numeric value. Do NOT use GROUP BY or LIMIT > 1.",
        )
        kpi["sql"] = sql  # store corrected SQL

        if not result["success"]:
            kpi["value"] = "N/A"
            kpi["error"] = result["error"]
            # ── Attempt regeneration via chat pipeline ──────────────────
            kpi = self._try_regenerate_kpi(kpi)
            return kpi

        data = result["data"]
        if data and len(data) > 0:
            # ── If result has multiple rows, auto-aggregate ────────────
            if len(data) > 1:
                logger.warning(
                    "KPI '%s' returned %d rows — expected 1. Auto-aggregating.",
                    kpi_label, len(data)
                )
                # Try to sum all numeric values across rows
                first_row = data[0]
                value_cols = [k for k, v in first_row.items()
                              if isinstance(v, (int, float)) or
                              (v is not None and str(v).replace('.', '').replace('-', '').isdigit())]
                if value_cols:
                    # Use the last numeric column (usually the aggregate)
                    val_col = value_cols[-1]
                    total = 0
                    for row in data:
                        try:
                            total += float(row.get(val_col, 0) or 0)
                        except (ValueError, TypeError):
                            pass
                    kpi["value"] = total
                    return kpi
                else:
                    # No numeric column — just count the rows
                    kpi["value"] = len(data)
                    return kpi

            first_row = data[0]
            if not first_row:
                kpi["value"] = "N/A"
                return kpi

            values = list(first_row.values())
            numeric_val = None
            for v in reversed(values):
                if v is not None and isinstance(v, (int, float)):
                    numeric_val = v
                    break
                try:
                    numeric_val = float(v)
                    break
                except (TypeError, ValueError):
                    continue

            if numeric_val is not None:
                kpi["value"] = numeric_val
            else:
                kpi["value"] = values[0] if values else "N/A"
        else:
            kpi["value"] = "N/A"

        # ── If the value is zero or N/A, try regeneration ──────────────
        try:
            val = kpi.get("value")
            if val == "N/A" or val is None or (isinstance(val, (int, float)) and val == 0):
                kpi = self._try_regenerate_kpi(kpi)
        except Exception:
            pass

        return kpi

    def _try_regenerate_kpi(self, kpi: dict) -> dict:
        """Attempt to regenerate a KPI's SQL using the chat pipeline when the
        original SQL returned zero, null, or errored.

        This bridges the accuracy gap between the report pipeline (single LLM call
        generating all SQL at once) and the chat pipeline (2-step guided chain).
        """
        if self._regen_count >= self._MAX_REGEN_PER_REPORT:
            return kpi

        kpi_label = kpi.get("label", kpi.get("id", "?"))
        logger.info("KPI '%s' returned zero/null/error — attempting chat-pipeline regeneration", kpi_label)

        self._regen_count += 1
        regen_sql = self._regenerate_sql(
            f"Calculate a single numeric value for the KPI: '{kpi_label}'. "
            f"The SQL MUST return exactly ONE row with ONE numeric column named 'value'. "
            f"Do NOT use GROUP BY. Do NOT use LIMIT > 1. Return a scalar aggregate."
        )
        if not regen_sql:
            return kpi

        # Validate and execute the regenerated SQL
        regen_sql = _fix_report_sql(regen_sql)
        regen_sql = self._fix_kpi_sql(regen_sql)
        from ai.validator import validate_sql as _val_sql
        is_safe, _ = _val_sql(regen_sql)
        if not is_safe:
            return kpi

        regen_result = execute_sql(regen_sql)
        if not regen_result["success"] or not regen_result["data"]:
            return kpi

        # Extract the value from the regenerated result
        data = regen_result["data"]
        if data and len(data) > 0:
            first_row = data[0]
            values = list(first_row.values())
            numeric_val = None
            for v in reversed(values):
                if v is not None and isinstance(v, (int, float)):
                    numeric_val = v
                    break
                try:
                    numeric_val = float(v)
                    break
                except (TypeError, ValueError):
                    continue

            if numeric_val is not None and (numeric_val != 0 or kpi.get("value") == "N/A"):
                logger.info("KPI '%s' regenerated successfully: %s → %s",
                            kpi_label, kpi.get("value"), numeric_val)
                kpi["value"] = numeric_val
                kpi["sql"] = regen_sql
                kpi.pop("error", None)

        return kpi

    def _execute_chart_sql(self, chart: dict) -> dict:
        """Execute a chart's SQL and populate its data.

        If the SQL fails or returns empty/bad data, attempts regeneration
        via the chat pipeline for data accuracy.
        """
        sql = chart.get("sql", "")
        if not sql:
            chart["data"] = []
            chart["error"] = "No SQL provided"
            return chart

        chart_title = chart.get("title", chart.get("id", "?"))
        chart_type = chart.get("type", "bar")
        sql, result = self._validate_and_execute_sql(
            sql, context=f"Chart:{chart_title}",
            sql_description=f"Query data for chart: {chart_title}. Return rows with a label column and a value column.",
        )
        chart["sql"] = sql  # store corrected SQL

        if not result["success"]:
            chart["data"] = []
            chart["error"] = result["error"]
            # ── Attempt regeneration via chat pipeline ──────────────────
            chart = self._try_regenerate_chart(chart)
            return chart

        chart["data"] = result["data"]

        # ── Validate chart data quality ─────────────────────────────────
        chart = self._validate_chart_data(chart)

        return chart

    def _validate_chart_data(self, chart: dict) -> dict:
        """Check chart data for quality issues and attempt regeneration if bad.

        Detects:
        - Empty data (0 rows)
        - Single-column data (missing label or value)
        - All-zero or all-null value columns
        - Single row (KPI-style result, not chart-worthy)
        """
        data = chart.get("data", [])
        chart_title = chart.get("title", "?")

        if not data or len(data) == 0:
            logger.info("Chart '%s' — empty data, attempting regeneration", chart_title)
            return self._try_regenerate_chart(chart)

        # Check column count
        keys = list(data[0].keys())
        if len(keys) < 2:
            logger.info("Chart '%s' — only %d column(s), attempting regeneration", chart_title, len(keys))
            return self._try_regenerate_chart(chart)

        # Check if all numeric values are zero/null
        value_keys = keys[1:]
        _bad_vals = {None, 0, "", "0", 0.0}
        all_bad = all(
            all(row.get(k) in _bad_vals for k in value_keys)
            for row in data
        )
        if all_bad:
            logger.info("Chart '%s' — all values are zero/null, attempting regeneration", chart_title)
            return self._try_regenerate_chart(chart)

        return chart

    def _try_regenerate_chart(self, chart: dict) -> dict:
        """Attempt to regenerate a chart's SQL using the chat pipeline when the
        original SQL returned empty, errored, or bad data.
        """
        if self._regen_count >= self._MAX_REGEN_PER_REPORT:
            return chart

        chart_title = chart.get("title", chart.get("id", "?"))
        chart_type = chart.get("type", "bar")
        logger.info("Chart '%s' — attempting chat-pipeline SQL regeneration", chart_title)

        self._regen_count += 1
        regen_sql = self._regenerate_sql(
            f"Query data for a {chart_type} chart titled '{chart_title}'. "
            f"Return rows with a label column (text/name) and a numeric value column. "
            f"Use GROUP BY to get multiple data points. Return at least 3 rows."
        )
        if not regen_sql:
            return chart

        # Validate and execute
        regen_sql = _fix_report_sql(regen_sql)
        from ai.validator import validate_sql as _val_sql
        is_safe, _ = _val_sql(regen_sql)
        if not is_safe:
            return chart

        regen_result = execute_sql(regen_sql)
        if not regen_result["success"] or not regen_result["data"]:
            logger.warning("Chart '%s' regeneration failed: %s",
                           chart_title, regen_result.get("error", "empty"))
            return chart

        # Validate regenerated data has 2+ columns and multiple rows
        regen_data = regen_result["data"]
        if regen_data and len(regen_data) >= 1:
            row_keys = list(regen_data[0].keys())
            if len(row_keys) >= 2:
                logger.info("Chart '%s' regenerated successfully (%d rows, %d cols)",
                            chart_title, len(regen_data), len(row_keys))
                chart["data"] = regen_data
                chart["sql"] = regen_sql
                chart.pop("error", None)

        return chart

    def _execute_table_sql(self, table: dict) -> dict:
        """Execute the detail table's SQL and populate data."""
        sql = table.get("sql", "")
        if not sql:
            table["data"] = []
            return table

        table_title = table.get("title", "Detail table")
        sql, result = self._validate_and_execute_sql(
            sql, context="DetailTable",
            sql_description=f"Query detail data for: {table_title}",
        )
        table["sql"] = sql  # store corrected SQL

        if not result["success"]:
            table["data"] = []
            table["error"] = result["error"]
            return table

        table["data"] = result["data"][:200]  # Limit rows for display
        return table

    def _extract_subject_lock(self, question: str, schema_str: str, profile_str: str) -> str:
        """Dynamically extract the report subject and build a subject-locking
        instruction that gets prepended to the LLM question.

        Scans the actual schema for tables/columns matching the user's keywords
        so the LLM knows exactly which tables to JOIN/filter.
        """
        q = question.lower().strip()

        # Remove filter context injected by app.py (e.g. "[ACTIVE FILTERS: ...]")
        q = re.sub(r'\[active filters:.*?\]', '', q, flags=re.IGNORECASE).strip()
        # Remove date context injected by _build_question_with_context
        q = re.sub(r'\[context:.*?\]', '', q, flags=re.IGNORECASE).strip()

        if len(q) < 5:
            return ""

        # ── Find schema tables whose names match words in the question ──
        # Schema format is "TABLE: table_name\n    col_name  type  nullable"
        all_tables = re.findall(r'TABLE:\s*(\S+)', schema_str, re.IGNORECASE)
        matching_tables = []
        for table in all_tables:
            # Check if any word from the table name appears in the question
            table_words = table.lower().replace('_', ' ').split()
            for tw in table_words:
                if len(tw) >= 3 and tw in q and tw not in ('line', 'order', 'master', 'sales'):
                    if table not in matching_tables:
                        matching_tables.append(table)
                        break

        # ── Find matching column values from data profile ──
        matching_values = []
        if profile_str:
            for line in profile_str.split('\n'):
                line_lower = line.lower()
                if 'distinct values' not in line_lower:
                    continue
                # Check if any significant word from the question appears
                q_words = [w for w in q.split() if len(w) >= 3]
                for w in q_words:
                    if w in line_lower and w not in ('the', 'and', 'for', 'report', 'analysis', 'top'):
                        matching_values.append(line.strip())
                        break

        # ── Build the subject lock instruction ──
        lock = [
            "══════════════════════════════════════════════════",
            f"⚠ SUBJECT LOCK: \"{question.strip()}\"",
            "══════════════════════════════════════════════════",
            f"The user's EXACT request is: \"{question.strip()}\"",
            "",
            "ALL 6 KPIs, ALL 6 charts, ALL insights, and the detail table",
            "must be EXCLUSIVELY about this subject. Every KPI label and",
            "chart title must reference the subject. Every SQL query must",
            "filter/scope data to ONLY this subject using appropriate",
            "JOINs and WHERE clauses derived from the schema.",
            "",
            "Generic/unscoped components are FORBIDDEN. Each metric must",
            "be qualified with the subject (e.g., 'Subject Revenue' not",
            "'Total Revenue').",
        ]

        if matching_tables:
            lock.append("")
            lock.append("SCHEMA TABLES matching this subject:")
            for t in matching_tables[:8]:
                # Get columns for this table from schema
                table_section = re.search(
                    rf'TABLE:\s*{re.escape(t)}\n((?:\s+\S+.*\n)*)',
                    schema_str, re.IGNORECASE
                )
                cols = ""
                if table_section:
                    col_lines = table_section.group(1).strip().split('\n')
                    col_names = [cl.strip().split()[0] for cl in col_lines if cl.strip()]
                    cols = f" → columns: {', '.join(col_names[:8])}"
                lock.append(f"  • {t}{cols}")
            lock.append("JOIN these tables to scope queries to the subject.")

        if matching_values:
            lock.append("")
            lock.append("MATCHING DATA VALUES from profile:")
            for v in matching_values[:5]:
                lock.append(f"  • {v[:150]}")

        lock.append("══════════════════════════════════════════════════")

        result = "\n".join(lock)
        logger.info("Subject lock: matching_tables=%s", matching_tables[:5])
        return result

    @staticmethod
    def _cache_key(question: str) -> str:
        """Generate a deterministic cache key from the user's question.

        Normalises whitespace, lowercases, strips common filler words so
        that 'Gold product analysis' and 'gold product   analysis' hit
        the same cache entry.
        """
        q = question.lower().strip()
        # Remove injected context/filters — they change per call
        q = re.sub(r'\[active filters:.*?\]', '', q, flags=re.IGNORECASE)
        q = re.sub(r'\[context:.*?\]', '', q, flags=re.IGNORECASE)
        q = re.sub(r'\s+', ' ', q).strip()
        return hashlib.md5(q.encode()).hexdigest()

    def generate(self, question: str, force_refresh: bool = False) -> dict[str, Any]:
        """Generate a complete report with real data.

        Uses a blueprint cache so the SAME question always produces the
        SAME report structure (titles, chart types, SQL queries).  Data
        values are re-executed fresh from the database each call.

        Set force_refresh=True to bypass the cache and regenerate.
        """
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        # Store context for SQL regeneration fallback
        self._report_schema_str = schema_str
        self._report_rels_str = rels_str
        self._report_profile_str = profile_str
        self._regen_count = 0  # reset per-report regeneration counter

        cache_key = self._cache_key(question)

        # ── Per-key lock: prevents duplicate LLM calls when two users
        #    request the same report simultaneously ─────────────────────
        lock = _get_cache_lock(cache_key)
        with lock:
            # ── Check blueprint cache first ───────────────────────────
            if not force_refresh and cache_key in _blueprint_cache:
                logger.info("Blueprint cache HIT for key %s — reusing cached structure", cache_key[:8])
                report = json.loads(json.dumps(_blueprint_cache[cache_key]))  # deep copy
            else:
                # ── Dynamic subject extraction & enforcement ──────────
                subject_lock = self._extract_subject_lock(question, schema_str, profile_str)
                question_with_date = self._build_question_with_context(question)
                if subject_lock:
                    question_with_date = subject_lock + "\n\n" + question_with_date
                    logger.info("Subject lock prepended to question (%d chars)", len(subject_lock))

                # ── Pre-analyze query using chat pipeline for SQL guidance ──
                sql_guide = self._pre_analyze_query(
                    question, schema_str, rels_str, profile_str
                )
                if sql_guide:
                    question_with_date = question_with_date + "\n" + sql_guide
                    logger.info("SQL guide appended to question (%d chars)", len(sql_guide))

                # ── Analytical framework for report-type-specific guidance ──
                analytical_guide = self._get_analytical_framework(question)
                if analytical_guide:
                    question_with_date = question_with_date + "\n" + analytical_guide
                    logger.info("Analytical framework appended to question")

                logger.info("Report generation — calling LLM for report blueprint (cache MISS)")
                logger.info("Question sent to LLM (first 500 chars): %s", question_with_date[:500])

                # Call LLM to generate report blueprint
                result = self.report_gen(
                    question=question_with_date,
                    schema_info=schema_str,
                    relationships=rels_str,
                    data_profile=profile_str,
                )

                # Parse the JSON output
                try:
                    report = self._extract_json(result.report_json)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.error("Failed to parse report JSON: %s", exc)
                    return {
                        "mode": "report",
                        "error": f"Failed to generate report structure: {str(exc)}",
                        "report": None,
                    }

                # ── Store blueprint in cache (structure only, no data)
                _blueprint_cache[cache_key] = json.loads(json.dumps(report))
                _save_blueprint_cache(_blueprint_cache)
                logger.info("Blueprint cached with key %s (%d entries total)",
                            cache_key[:8], len(_blueprint_cache))

        logger.info("Report blueprint received — executing SQL queries")

        # Execute all KPI SQLs
        for kpi in report.get("kpis", []):
            self._execute_kpi_sql(kpi)

        # Execute all chart SQLs
        for chart in report.get("charts", []):
            self._execute_chart_sql(chart)

        # Execute table SQL
        if "table" in report and report["table"]:
            self._execute_table_sql(report["table"])

        # ── Post-processing: remove failed KPIs and empty charts ──────
        if "kpis" in report:
            report["kpis"] = self._clean_kpis(report["kpis"])

        # Remove charts with empty data, errors, single-column data, or all-zero values
        # but guarantee at least MIN_CHARTS survive
        MIN_CHARTS = 6
        if "charts" in report:
            all_charts = report["charts"]
            if len(all_charts) < 6:
                logger.warning("LLM generated only %d charts (expected 6)", len(all_charts))

            valid_charts = []
            fallback_charts = []   # errored/zero charts kept as fallback
            for chart in all_charts:
                if chart.get("error"):
                    logger.info("Chart '%s' has error: %s", chart.get("title", "?"), chart.get("error"))
                    fallback_charts.append(chart)
                    continue
                if not chart.get("data") or len(chart["data"]) == 0:
                    logger.info("Chart '%s' has empty data", chart.get("title", "?"))
                    fallback_charts.append(chart)
                    continue
                # Check column count — need at least label + value
                row_keys = list(chart["data"][0].keys()) if chart["data"] else []
                if len(row_keys) < 2:
                    # Attempt to salvage: if it's a single-aggregate (KPI-style) result,
                    # convert it into a displayable chart with a label column
                    if len(row_keys) == 1 and len(chart["data"]) == 1:
                        # Single value — convert to a bar with the chart title as label
                        val_key = row_keys[0]
                        chart["data"] = [{"label": chart.get("title", "Value"), val_key: chart["data"][0][val_key]}]
                        chart["type"] = "bar"
                        logger.info("Salvaged chart '%s' — converted single-value to bar", chart.get("title", "?"))
                    elif len(row_keys) == 1 and len(chart["data"]) > 1:
                        # Multiple rows but only one column — add row index as label
                        val_key = row_keys[0]
                        for i, row in enumerate(chart["data"]):
                            row["label"] = f"Item {i+1}"
                        logger.info("Salvaged chart '%s' — added index labels to %d rows", chart.get("title", "?"), len(chart["data"]))
                    else:
                        logger.info("Chart '%s' — only %d columns (need 2+)", chart.get("title", "?"), len(row_keys))
                        fallback_charts.append(chart)
                        continue
                # Check if all numeric values are zero
                value_keys = row_keys[1:]
                all_zero = all(
                    all((v := row.get(k)) is None or v == 0 or v == "" for k in value_keys)
                    for row in chart["data"]
                )
                if all_zero:
                    logger.info("Chart '%s' — all values are zero/null", chart.get("title", "?"))
                    fallback_charts.append(chart)
                    continue

                # ── Per-row zero filtering for categorical charts ──────
                # Remove individual rows where every numeric value is 0/null
                # (e.g. "New: 0" in "New vs Returning" chart).
                # Only applied to small charts (≤30 rows) so time-series
                # charts with legitimate zero months are not affected.
                if len(chart["data"]) <= 30:
                    _def_zero = {None, 0, "", "0", 0.0}
                    non_zero_rows = [
                        row for row in chart["data"]
                        if any(row.get(k) not in _def_zero for k in value_keys)
                    ]
                    if len(non_zero_rows) >= 1 and len(non_zero_rows) < len(chart["data"]):
                        logger.info(
                            "Chart '%s': dropped %d zero-value row(s) (kept %d)",
                            chart.get("title", "?"),
                            len(chart["data"]) - len(non_zero_rows),
                            len(non_zero_rows),
                        )
                        chart["data"] = non_zero_rows

                valid_charts.append(chart)

            # ── Regenerate failed charts via chat pipeline ─────────────
            # If we have fewer than 6 valid charts, try to regenerate SQL
            # for failed charts using the chat pipeline's full chain.
            if len(valid_charts) < MIN_CHARTS and fallback_charts:
                for fb in fallback_charts[:]:
                    if len(valid_charts) >= MIN_CHARTS:
                        break
                    chart_title = fb.get("title", "Chart")
                    chart_type = fb.get("type", "bar")
                    logger.info(
                        "Attempting to regenerate chart '%s' via chat pipeline (%d/%d)",
                        chart_title, len(valid_charts) + 1, MIN_CHARTS
                    )
                    regen_sql = self._regenerate_sql(
                        f"Query data for a {chart_type} chart titled '{chart_title}'. "
                        f"Return rows with a label column and a numeric value column. "
                        f"Use GROUP BY to get multiple data points."
                    )
                    if regen_sql:
                        fb["sql"] = regen_sql
                        fb.pop("error", None)
                        fb["data"] = []
                        self._execute_chart_sql(fb)
                        if fb.get("data") and len(fb["data"]) > 0:
                            row_keys = list(fb["data"][0].keys())
                            if len(row_keys) >= 2:
                                valid_charts.append(fb)
                                fallback_charts.remove(fb)
                                logger.info(
                                    "Chart '%s' regenerated successfully (%d rows)",
                                    chart_title, len(fb["data"])
                                )
                                continue
                    # Regeneration failed — keep as fallback
                    logger.warning("Chart '%s' regeneration failed", chart_title)

            # Fill remaining slots from fallbacks if still short
            if len(valid_charts) < MIN_CHARTS and fallback_charts:
                needed = MIN_CHARTS - len(valid_charts)
                # Prefer fallback charts that have data (even with errors) over empty ones
                fallback_charts.sort(
                    key=lambda c: (len(c.get("data", [])) > 0, not c.get("error")),
                    reverse=True,
                )
                for fb in fallback_charts[:needed]:
                    fb.pop("error", None)  # remove error flag so it renders
                    if not fb.get("data"):
                        fb["data"] = [{"label": "No data available", "value": 0}]
                        fb["type"] = "bar"
                    valid_charts.append(fb)
                    logger.info("Kept fallback chart '%s' to meet minimum (%d/%d)",
                                fb.get("title", "?"), len(valid_charts), MIN_CHARTS)

            if valid_charts:
                report["charts"] = valid_charts
            logger.info("Charts after cleanup: %d valid, %d fallback (%d total of %d original)",
                        len([c for c in valid_charts if c not in fallback_charts]),
                        len([c for c in valid_charts if c in fallback_charts]),
                        len(valid_charts), len(all_charts))

        # ── Smart chart-type auto-correction based on actual data ──────
        if "charts" in report:
            for chart in report["charts"]:
                self._smart_fix_chart_type(chart)

        # ── Enforce chart type diversity (no duplicate types) ─────────
        if "charts" in report and len(report["charts"]) > 1:
            report["charts"] = self._enforce_chart_diversity(report["charts"])

        logger.info("Report generation complete — all SQL executed")

        # ── Rewrite summary using actual KPI values ────────────────────
        report["summary"] = self._regenerate_summary(
            report.get("summary", ""), report.get("kpis", [])
        )

        # ── Detect applicable filters based on SQL content ────────────
        applicable_filters = self._detect_applicable_filters(report)

        return {
            "mode": "report",
            "report": report,
            "applicable_filters": applicable_filters,
            "ui_instructions": {
                "create_new_section": True,
                "open_in_new_tab": True,
                "enable_streaming": True,
                "stream_once": True,
                "include_report_ai": True,
                "report_ai": {
                    "type": "chat_like",
                    "position": "below_report",
                },
                "explanation_feature": {
                    "enabled": True,
                    "trigger": "eye_button",
                },
            },
        }

    def _smart_fix_chart_type(self, chart: dict) -> None:
        """Auto-correct chart type based on actual data patterns.

        Analyzes the first column (labels) and row count to pick the best
        chart type for the data, overriding the LLM's choice when wrong.
        Also trims excessively large datasets to keep charts readable.
        """
        data = chart.get("data")
        if not data or len(data) == 0:
            return

        chart_type = chart.get("type", "bar").lower()
        row_count = len(data)
        keys = list(data[0].keys())
        label_key = keys[0]
        value_keys = keys[1:]
        labels = [str(row.get(label_key, "")) for row in data]

        # ── Detect time-series labels (dates, months, years) ──────────
        time_patterns = [
            r"^\d{4}-\d{2}$",        # 2024-01
            r"^\d{4}-\d{2}-\d{2}$",  # 2024-01-15
            r"^\d{4}-\d{2}-\d{2}T",  # 2024-01-15T00:00:00 (ISO timestamp)
            r"^\d{4}-\d{2}-\d{2}\s", # 2024-01-15 00:00:00
            r"^\d{4}$",              # 2024
            r"^Q[1-4]\s?\d{4}$",     # Q1 2024
            r"^\w{3,9}\s?\d{4}$",    # Jan 2024 / January 2024
        ]
        import re as _re
        is_time_series = False
        if row_count >= 3:
            match_count = sum(
                1 for lbl in labels[:5]
                if any(_re.match(p, lbl.strip()) for p in time_patterns)
            )
            if match_count >= min(3, len(labels[:5])):
                is_time_series = True

        # ── Auto-clean raw timestamp labels to YYYY-MM format ─────────
        # If labels are raw timestamps (e.g., 2025-10-01T00:00:00+00:00),
        # clean them to YYYY-MM for readable chart axes
        if is_time_series:
            iso_pattern = r"^\d{4}-\d{2}-\d{2}[T\s]"
            has_raw_timestamps = any(_re.match(iso_pattern, lbl.strip()) for lbl in labels[:3])
            if has_raw_timestamps:
                # Aggregate to monthly if labels are daily timestamps
                from collections import OrderedDict
                monthly = OrderedDict()
                for row in data:
                    lbl = str(row.get(label_key, ""))
                    month_key = lbl[:7]  # "2025-10-01T..." → "2025-10"
                    if month_key not in monthly:
                        monthly[month_key] = {label_key: month_key}
                        for vk in value_keys:
                            monthly[month_key][vk] = 0
                    for vk in value_keys:
                        try:
                            monthly[month_key][vk] += float(row.get(vk, 0) or 0)
                        except (ValueError, TypeError):
                            pass
                chart["data"] = list(monthly.values())
                data = chart["data"]
                row_count = len(data)
                labels = [str(row.get(label_key, "")) for row in data]
                logger.info("Auto-clean chart '%s': cleaned timestamp labels → YYYY-MM (%d points)",
                            chart.get("title", "?"), row_count)

        # ── Fix raw numeric labels ────────────────────────────────────
        # If the first column (labels) contains large numeric values, columns
        # might be swapped. Try to detect and fix or format them.
        if not is_time_series and data and len(keys) >= 2:
            import re as _re2
            # Check if labels are all large numbers (>1000) — likely wrong column as label
            numeric_labels = 0
            for lbl in labels[:5]:
                try:
                    val = float(lbl.replace(",", ""))
                    if abs(val) > 1000:
                        numeric_labels += 1
                except (ValueError, TypeError):
                    pass

            if numeric_labels >= min(3, len(labels[:5])):
                # Check if second column has string/name values that should be labels
                second_key = keys[1] if len(keys) > 1 else None
                if second_key:
                    second_vals = [str(row.get(second_key, "")) for row in data[:5]]
                    non_numeric_count = sum(
                        1 for v in second_vals
                        if v and not v.replace(".", "").replace(",", "").replace("-", "").isdigit()
                    )
                    if non_numeric_count >= min(3, len(second_vals)):
                        # Swap columns — second column has the real labels
                        logger.info("Auto-fix chart '%s': swapping label column '%s' ↔ '%s'",
                                    chart.get("title", "?"), label_key, second_key)
                        for row in data:
                            row[label_key], row[second_key] = row[second_key], row[label_key]
                        labels = [str(row.get(label_key, "")) for row in data]
                    else:
                        # Both columns numeric — format labels for readability
                        logger.info("Auto-fix chart '%s': formatting numeric labels for readability",
                                    chart.get("title", "?"))
                        for row in data:
                            try:
                                val = float(str(row.get(label_key, 0)).replace(",", ""))
                                if abs(val) >= 1_00_00_000:
                                    row[label_key] = f"₹{val/1_00_00_000:.1f}Cr"
                                elif abs(val) >= 1_00_000:
                                    row[label_key] = f"₹{val/1_00_000:.1f}L"
                                elif abs(val) >= 1000:
                                    row[label_key] = f"₹{val/1000:.1f}K"
                                else:
                                    row[label_key] = f"₹{val:,.0f}"
                            except (ValueError, TypeError):
                                pass
                        labels = [str(row.get(label_key, "")) for row in data]

        original_type = chart_type

        # Rule 1: Time-series data → line or area (never bar/horizontalBar)
        # BUT only for dense time-series (6+ points) — ≤5 points look bad as line/area
        if is_time_series and row_count >= 6 and chart_type in ("bar", "horizontalBar", "pie", "doughnut"):
            chart["type"] = "line"
            logger.info("Auto-fix chart '%s': %s → line (dense time-series, %d points)",
                        chart.get("title", "?"), original_type, row_count)

        # Rule 2: Too many slices for pie/doughnut → trim to top 6 + "Others"
        elif chart_type in ("pie", "doughnut") and row_count > 8 and value_keys:
            first_val_key = value_keys[0]
            try:
                sorted_data = sorted(
                    data,
                    key=lambda r: float(r.get(first_val_key, 0) or 0),
                    reverse=True
                )
                top_slices = sorted_data[:6]
                others_sum = sum(float(r.get(first_val_key, 0) or 0) for r in sorted_data[6:])
                if others_sum > 0:
                    others_row = {label_key: "Others", first_val_key: others_sum}
                    top_slices.append(others_row)
                chart["data"] = top_slices
                logger.info("Auto-fix chart '%s': trimmed %d → %d slices (kept %s)",
                            chart.get("title", "?"), row_count, len(top_slices), chart_type)
            except (ValueError, TypeError):
                chart["type"] = "bar"  # fallback
                logger.info("Auto-fix chart '%s': %s → bar (trim failed)",
                            chart.get("title", "?"), original_type)

        # Rule 3: Too many categories for bar → horizontalBar
        elif chart_type == "bar" and row_count > 12 and not is_time_series:
            chart["type"] = "horizontalBar"
            logger.info("Auto-fix chart '%s': bar → horizontalBar (%d categories)",
                        chart.get("title", "?"), row_count)

        # Rule 4: Few categories in horizontalBar → regular bar
        elif chart_type == "horizontalBar" and row_count <= 6:
            chart["type"] = "bar"
            logger.info("Auto-fix chart '%s': horizontalBar → bar (only %d categories)",
                        chart.get("title", "?"), row_count)

        # ── Rule 5: Trim excessive NON-time-series data (>20) to Top 15 ──
        # Only for categorical charts, never for time-series
        updated_type = chart.get("type", chart_type).lower()
        updated_count = len(chart.get("data", data))
        if (not is_time_series
                and updated_type not in ("pie", "doughnut", "line", "area")
                and updated_count > 20
                and value_keys):
            first_val_key = value_keys[0]
            try:
                current_data = chart.get("data", data)
                sorted_data = sorted(
                    current_data,
                    key=lambda r: float(r.get(first_val_key, 0) or 0),
                    reverse=True
                )
                chart["data"] = sorted_data[:15]
                logger.info("Auto-fix chart '%s': trimmed %d → 15 rows",
                            chart.get("title", "?"), updated_count)
            except (ValueError, TypeError):
                pass

        # ── Rule 6: Remove incompatible secondary series (scale ratio > 1000x) ──
        # e.g. Revenue (crores) + Order Count (hundreds) can NOT share a Y-axis.
        # The small series becomes invisible and its tooltip shows ₹ which is wrong.
        # Fix: keep only the primary (largest magnitude) series.
        current_data = chart.get("data", data)
        if current_data:
            current_keys = list(current_data[0].keys())
            current_vkeys = current_keys[1:]
            if (len(current_vkeys) > 1
                    and chart.get("type", "bar").lower() not in ("pie", "doughnut", "stackedbar")):
                max_by_key = {}
                for vk in current_vkeys:
                    try:
                        mv = max(abs(float(row.get(vk, 0) or 0)) for row in current_data)
                        max_by_key[vk] = mv
                    except (ValueError, TypeError):
                        max_by_key[vk] = 0
                positive_maxes = {k: v for k, v in max_by_key.items() if v > 0}
                if len(positive_maxes) >= 2:
                    max_v = max(positive_maxes.values())
                    min_v = min(positive_maxes.values())
                    if min_v > 0 and max_v / min_v > 1000:
                        primary_key = max(positive_maxes, key=lambda k: positive_maxes[k])
                        lk = current_keys[0]
                        for row in current_data:
                            for vk in list(row.keys()):
                                if vk != lk and vk != primary_key:
                                    del row[vk]
                        chart["data"] = current_data
                        logger.info(
                            "Auto-fix chart '%s': removed incompatible series "
                            "(scale ratio %.0fx, kept '%s')",
                            chart.get("title", "?"), max_v / min_v, primary_key,
                        )

    @staticmethod
    def _enforce_chart_diversity(charts: list) -> list:
        """Ensure chart types are diverse — no type used more than 2 times.

        Rules:
        - Never use polarArea or radar (unreadable with business data)
        - No chart type may appear more than MAX_PER_TYPE times
        - Time-series data (dates in labels) → line or area preferred,
          but excess time-series get converted to bar/stackedBar
        - Proportions/shares (≤8 items) → pie or doughnut
        - Comparisons (>8 items) → horizontalBar
        - Comparisons (≤8 items) → bar
        """
        MAX_PER_TYPE = 2  # hard cap: no type more than 2 times

        GOOD_TYPES = ["bar", "line", "pie", "doughnut", "horizontalBar", "stackedBar", "area"]

        TIME_KEYWORDS = ["trend", "growth", "over time", "monthly", "weekly", "daily",
                         "quarterly", "yearly", "timeline", "history", "date", "period"]
        PROPORTION_KEYWORDS = ["distribution", "share", "breakdown", "composition",
                               "by category", "by type", "proportion", "split", "mix"]

        def _has_date_labels(chart):
            """Check if the chart's data labels look like dates."""
            data = chart.get("data", [])
            if not data:
                return False
            keys = list(data[0].keys())
            if not keys:
                return False
            label_key = keys[0]
            sample_labels = [str(row.get(label_key, "")) for row in data[:5]]
            date_patterns = [r"\d{4}-\d{2}", r"\d{2}/\d{2}", r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"]
            import re as _re
            for label in sample_labels:
                for pat in date_patterns:
                    if _re.search(pat, label, _re.IGNORECASE):
                        return True
            return False

        def _type_available(t, type_counts):
            """Check if a chart type hasn't reached the max limit."""
            return type_counts.get(t, 0) < MAX_PER_TYPE

        def _infer_best_type(chart, type_counts):
            """Pick the best chart type based on title, data shape, and usage counts."""
            title = (chart.get("title") or "").lower()
            data = chart.get("data", [])
            num_rows = len(data)
            num_cols = len(data[0].keys()) if data else 0

            # Time-series → prefer line or area, but fall through to others if maxed
            if _has_date_labels(chart) or any(kw in title for kw in TIME_KEYWORDS):
                for t in ["line", "area", "bar", "stackedBar"]:
                    if _type_available(t, type_counts):
                        return t

            # Proportions → pie or doughnut
            if any(kw in title for kw in PROPORTION_KEYWORDS) and num_rows <= 10:
                for t in ["pie", "doughnut", "bar", "horizontalBar"]:
                    if _type_available(t, type_counts):
                        return t

            # Many categories → horizontalBar or bar
            if num_rows > 8:
                for t in ["horizontalBar", "bar", "stackedBar"]:
                    if _type_available(t, type_counts):
                        return t

            # Multiple value columns → stackedBar
            if num_cols >= 3:
                if _type_available("stackedBar", type_counts):
                    return "stackedBar"

            # Default: pick first available good type
            for t in GOOD_TYPES:
                if _type_available(t, type_counts):
                    return t

            # Absolute fallback (all types at max — very unlikely with 7 types × 2 = 14 slots)
            return "bar"

        from collections import Counter
        type_counts = Counter()
        result = []

        for chart in charts:
            original_type = (chart.get("type") or "bar").lower()

            # Force-replace banned chart types
            if original_type in ("polararea", "polarArea", "radar"):
                new_type = _infer_best_type(chart, type_counts)
                chart["type"] = new_type
                type_counts[new_type] += 1
                logger.info(
                    "Chart fix: replaced '%s' with '%s' for '%s'",
                    original_type, new_type, chart.get("title", "?"),
                )
                result.append(chart)
            elif not _type_available(original_type, type_counts):
                # Type has reached the max — must reassign
                if original_type == "pie" and _type_available("doughnut", type_counts):
                    chart["type"] = "doughnut"
                    type_counts["doughnut"] += 1
                    logger.info("Chart diversity: pie → doughnut for '%s'", chart.get("title", "?"))
                elif original_type == "doughnut" and _type_available("pie", type_counts):
                    chart["type"] = "pie"
                    type_counts["pie"] += 1
                    logger.info("Chart diversity: doughnut → pie for '%s'", chart.get("title", "?"))
                elif original_type in ("line", "area") and _type_available("area" if original_type == "line" else "line", type_counts):
                    swap = "area" if original_type == "line" else "line"
                    chart["type"] = swap
                    type_counts[swap] += 1
                    logger.info("Chart diversity: %s → %s for '%s'", original_type, swap, chart.get("title", "?"))
                else:
                    new_type = _infer_best_type(chart, type_counts)
                    chart["type"] = new_type
                    type_counts[new_type] += 1
                    logger.info(
                        "Chart diversity: changed '%s' (at max %d) to '%s' for '%s'",
                        original_type, MAX_PER_TYPE, new_type, chart.get("title", "?"),
                    )
                result.append(chart)
            else:
                type_counts[original_type] += 1
                result.append(chart)

        return result

    @staticmethod
    def _detect_applicable_filters(report: dict) -> dict:
        """Analyze all SQL in the report to determine which filters are applicable.

        Returns a dict like:
        {
            "date_range": True,   # has sales_order with order_date
            "category": True,     # has product_master
            "product": True,      # has product_master
            "customer": True,     # has customer_master
            "status": True,       # has sales_order with status
        }
        """
        # Collect all SQL from KPIs, charts, and table
        all_sql = []
        for kpi in report.get("kpis", []):
            if kpi.get("sql"):
                all_sql.append(kpi["sql"])
        for chart in report.get("charts", []):
            if chart.get("sql"):
                all_sql.append(chart["sql"])
        if report.get("table", {}).get("sql"):
            all_sql.append(report["table"]["sql"])

        combined = " ".join(all_sql).lower()

        has_sales_order = bool(re.search(r'\bsales_order\b(?!_)', combined))
        has_product_master = bool(re.search(r'\bproduct_master\b', combined))
        has_customer_master = bool(re.search(r'\bcustomer_master\b', combined))
        has_order_date = bool(re.search(r'\border_date\b', combined))

        filters = {}

        # Date range filter — applicable if sales_order is referenced
        if has_sales_order and has_order_date:
            filters["date_range"] = True

        # Category & Product — applicable if product_master is referenced
        if has_product_master:
            filters["category"] = True
            filters["product"] = True

        # Customer — applicable if customer_master is referenced
        if has_customer_master:
            filters["customer"] = True

        # Status — applicable if sales_order is referenced
        if has_sales_order:
            filters["status"] = True

        logger.info("Detected applicable filters: %s", filters)
        return filters

    def apply_filters(self, report: dict, filters: dict) -> dict[str, Any]:
        """Apply filters to an existing report by injecting WHERE clauses.

        This does NOT call the LLM — it modifies existing SQL directly.
        Much faster and more reliable than re-generating.
        """
        import copy
        report = copy.deepcopy(report)

        logger.info("Applying filters to existing report: %s", filters)

        # Apply filters to all KPI SQLs and re-execute
        for kpi in report.get("kpis", []):
            original_sql = kpi.get("sql", "")
            if original_sql:
                filtered_sql = _inject_filters(original_sql, filters)
                filtered_sql = _fix_report_sql(filtered_sql)
                kpi["sql"] = filtered_sql
                # Clear previous error/value
                kpi.pop("error", None)
                kpi.pop("value", None)
            self._execute_kpi_sql(kpi)

        # Apply filters to all chart SQLs and re-execute
        for chart in report.get("charts", []):
            original_sql = chart.get("sql", "")
            if original_sql:
                filtered_sql = _inject_filters(original_sql, filters)
                filtered_sql = _fix_report_sql(filtered_sql)
                chart["sql"] = filtered_sql
                # Clear previous error/data
                chart.pop("error", None)
                chart["data"] = []
            self._execute_chart_sql(chart)

        # Apply filters to table SQL and re-execute
        if "table" in report and report["table"]:
            original_sql = report["table"].get("sql", "")
            if original_sql:
                filtered_sql = _inject_filters(original_sql, filters)
                filtered_sql = _fix_report_sql(filtered_sql)
                report["table"]["sql"] = filtered_sql
                report["table"].pop("error", None)
                report["table"]["data"] = []
            self._execute_table_sql(report["table"])

        logger.info("Filter application complete — all SQL re-executed")

        # ── Post-processing: clean up after filter application ────────
        if "kpis" in report:
            report["kpis"] = self._clean_kpis(report["kpis"])

        if "charts" in report:
            valid_charts = []
            for chart in report["charts"]:
                if chart.get("error"):
                    continue
                if not chart.get("data") or len(chart["data"]) == 0:
                    continue
                row_keys = list(chart["data"][0].keys()) if chart["data"] else []
                if len(row_keys) < 2:
                    continue
                value_keys = row_keys[1:]
                all_zero = all(
                    all((v := row.get(k)) is None or v == 0 or v == "" for k in value_keys)
                    for row in chart["data"]
                )
                if all_zero:
                    continue
                valid_charts.append(chart)
            # Always update — even if empty — so stale pre-filter data is never shown
            report["charts"] = valid_charts

        # ── Smart chart-type auto-correction (same as generate) ──────────
        if "charts" in report:
            for chart in report["charts"]:
                self._smart_fix_chart_type(chart)

        if "charts" in report and len(report["charts"]) > 1:
            report["charts"] = self._enforce_chart_diversity(report["charts"])

        return {
            "mode": "report",
            "report": report,
            "applicable_filters": self._detect_applicable_filters(report),
            "ui_instructions": {
                "create_new_section": True,
                "open_in_new_tab": True,
                "enable_streaming": False,
                "stream_once": False,
            },
        }

    def modify(self, current_report_json: str, modification: str) -> dict[str, Any]:
        """Modify an existing report based on a natural-language command."""
        schema_str = format_schema()

        # ── Step 1: Record original chart types BEFORE the LLM call ──────────
        # Any chart whose type changes in the LLM response was explicitly requested
        # by the user — we must protect those from being overridden by auto-correction.
        _original_types: dict[str, str] = {}
        try:
            _current = (
                json.loads(current_report_json)
                if isinstance(current_report_json, str)
                else current_report_json
            )
            for c in _current.get("charts", []):
                cid = c.get("id") or c.get("title", "")
                if cid:
                    _original_types[cid] = (c.get("type") or "bar").lower()
        except Exception:
            pass

        logger.info("Report modification — command: %s", modification)

        # Strip data arrays before sending to LLM — the model only needs structure
        # and SQL queries, not hundreds of result rows. This prevents token truncation.
        try:
            _lean = json.loads(current_report_json) if isinstance(current_report_json, str) else current_report_json
            _lean_copy = json.loads(json.dumps(_lean))  # deep copy
            for kpi in _lean_copy.get("kpis", []):
                kpi.pop("value", None); kpi.pop("error", None)
            for chart in _lean_copy.get("charts", []):
                chart.pop("data", None); chart.pop("error", None)
            if _lean_copy.get("table"):
                _lean_copy["table"].pop("data", None)
            lean_json = json.dumps(_lean_copy)
        except Exception:
            lean_json = current_report_json  # fallback to original if stripping fails

        result = self.report_mod(
            current_report=lean_json,
            modification=modification,
            schema_info=schema_str,
        )

        try:
            report = self._extract_json(result.updated_report_json)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse modified report JSON (attempt 1): %s — retrying", exc)
            # ── Retry: ask the LLM to fix its own output ──────────────────
            try:
                retry_result = self.report_mod(
                    current_report=lean_json,
                    modification=(
                        f"{modification}\n\n"
                        "CRITICAL: Your previous response was not valid JSON. "
                        "Output ONLY a raw JSON object. "
                        "No markdown, no code fences, no single quotes, no trailing commas. "
                        "Every key and string value MUST use double quotes."
                    ),
                    schema_info=schema_str,
                )
                report = self._extract_json(retry_result.updated_report_json)
            except (json.JSONDecodeError, ValueError) as exc2:
                logger.error("Failed to parse modified report JSON (attempt 2): %s", exc2)
                return {
                    "mode": "report",
                    "error": f"Failed to modify report: {str(exc2)}",
                    "report": None,
                }

        # ── Step 2: Detect which chart types were explicitly changed ──────────
        # These are "user-locked" — auto-correction must NOT touch them.
        _user_locked: dict[str, str] = {}   # {chart_id_or_title: new_type_as_given}
        for chart in report.get("charts", []):
            cid = chart.get("id") or chart.get("title", "")
            new_type = (chart.get("type") or "bar").lower()
            if cid and cid in _original_types and _original_types[cid] != new_type:
                _user_locked[cid] = chart.get("type") or new_type
                logger.info(
                    "Modification: chart '%s' type locked as '%s' (changed from '%s' — user intent)",
                    cid, new_type, _original_types[cid],
                )

        # Re-execute all SQL queries on the modified report
        for kpi in report.get("kpis", []):
            self._execute_kpi_sql(kpi)

        for chart in report.get("charts", []):
            self._execute_chart_sql(chart)

        if "table" in report and report["table"]:
            self._execute_table_sql(report["table"])

        # ── Post-processing (same as generate, but lighter for add operations) ──
        _is_add_kpi = bool(re.search(r'\badd\b.*\bkpi\b', modification, re.IGNORECASE))
        if "kpis" in report:
            if _is_add_kpi:
                logger.info("Skipping _clean_kpis — user explicitly added a KPI")
            else:
                report["kpis"] = self._clean_kpis(report["kpis"])

        if "charts" in report:
            valid_charts = []
            for chart in report["charts"]:
                if chart.get("error"):
                    continue
                if not chart.get("data") or len(chart["data"]) == 0:
                    continue
                row_keys = list(chart["data"][0].keys()) if chart["data"] else []
                if len(row_keys) < 2:
                    continue
                value_keys = row_keys[1:]
                all_zero = all(
                    all((v := row.get(k)) is None or v == 0 or v == "" for k in value_keys)
                    for row in chart["data"]
                )
                if all_zero:
                    continue
                valid_charts.append(chart)
            if valid_charts:
                report["charts"] = valid_charts

        # ── Smart chart-type auto-correction (data-shape fixes only) ──────────
        # Run _smart_fix_chart_type for data-aggregation benefits (e.g. daily→monthly),
        # but immediately restore any type that was explicitly set by the user.
        if "charts" in report:
            for chart in report["charts"]:
                self._smart_fix_chart_type(chart)

        # ── Step 3: Restore user-locked chart types after auto-correction ─────
        # _smart_fix_chart_type may have re-changed the type — undo that for locked charts.
        # _enforce_chart_diversity is intentionally SKIPPED in modify() — it would override
        # the user's explicit request (e.g. "change pie to bar").
        if _user_locked and "charts" in report:
            for chart in report["charts"]:
                cid = chart.get("id") or chart.get("title", "")
                if cid in _user_locked:
                    chart["type"] = _user_locked[cid]
                    logger.info(
                        "Modification: enforced user-requested type '%s' for chart '%s'",
                        _user_locked[cid], cid,
                    )

        applicable_filters = self._detect_applicable_filters(report)

        return {
            "mode": "report",
            "report": report,
            "applicable_filters": applicable_filters,
            "ui_instructions": {
                "create_new_section": True,
                "open_in_new_tab": False,
                "enable_streaming": False,
                "stream_once": False,
                "include_report_ai": True,
                "report_ai": {
                    "type": "chat_like",
                    "position": "below_report",
                },
                "explanation_feature": {
                    "enabled": True,
                    "trigger": "eye_button",
                },
            },
        }
