"""Data profiler — samples the actual database to give the AI business context.

Profiles each table to discover:
- Categorical columns and their distinct values (status, type, category, etc.)
- Numeric column ranges (min, max, avg)
- Date column ranges
- Sample rows

This info is injected into the AI prompts so it can make smart
business decisions (e.g., filter by status='closed' for revenue).
"""

import time
from typing import Any

from sqlalchemy import text

from db.connection import get_engine
from db.schema import get_schema

# ── Cache ───────────────────────────────────────────────────────────────────
_profile_cache: str | None = None
_profile_ts: float = 0.0
_PROFILE_TTL: float = 600.0  # 10 minutes


# Only profile tables that are relevant to business queries.
# Skipping raw material, job card, and other operational tables to keep
# the first-call warm-up fast and the LLM prompt concise.
_KEY_TABLES = {
    "sales_order",
    "sales_order_line",
    "sales_order_line_pricing",
    "sales_order_line_gold",
    "sales_order_line_diamond",
    "purchase_order",
    "po_line_items",
    "po_line_pricing",
    "customer_master",
    "vendor_master",
    "product_master",
    "product_variant",
    "sales_allocation",
    "sales_order_payments",
    "sales_invoices",
    "finished_goods_inventory",
}


def get_data_profile(force_refresh: bool = False) -> str:
    """Return a formatted data profile string for prompt injection."""
    global _profile_cache, _profile_ts

    if not force_refresh and _profile_cache and (time.time() - _profile_ts < _PROFILE_TTL):
        return _profile_cache

    schema = get_schema()
    profile_parts: list[str] = []

    engine = get_engine()
    with engine.connect() as conn:
        for table, columns in schema.items():
            if table not in _KEY_TABLES:
                continue
            table_profile = _profile_table(conn, table, columns)
            if table_profile:
                profile_parts.append(table_profile)

    # Auto-generate business rules
    rules = _generate_business_rules(schema)
    if rules:
        profile_parts.append(rules)

    _profile_cache = "\n".join(profile_parts)
    _profile_ts = time.time()
    return _profile_cache


def _profile_table(conn, table: str, columns: list[dict]) -> str:
    """Profile a single table."""
    lines: list[str] = [f"TABLE PROFILE: {table}"]

    # Row count
    try:
        count = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
        lines.append(f"  Total rows: {count}")
    except Exception:
        return ""

    if count == 0:
        lines.append("  (empty table)")
        return "\n".join(lines)

    # Profile each column
    for col in columns:
        cname = col["column_name"]
        dtype = col["data_type"]

        try:
            if _is_categorical(dtype, cname):
                profile = _profile_categorical(conn, table, cname, count)
                if profile:
                    lines.append(profile)
            elif _is_numeric(dtype):
                profile = _profile_numeric(conn, table, cname)
                if profile:
                    lines.append(profile)
            elif _is_date(dtype):
                profile = _profile_date(conn, table, cname)
                if profile:
                    lines.append(profile)
        except Exception:
            continue

    lines.append("")
    return "\n".join(lines)


def _is_categorical(dtype: str, cname: str) -> bool:
    """Check if a column is likely categorical (status, type, category, etc.)."""
    categorical_types = {"character varying", "text", "varchar", "char", "character"}
    categorical_keywords = {
        "status", "state", "type", "category", "kind", "class",
        "group", "level", "tier", "grade", "priority", "stage",
        "flag", "mode", "role", "region", "country", "city",
        "gender", "channel", "source", "segment", "department",
    }
    if dtype.lower() in categorical_types:
        # Check if the column name suggests it's categorical
        lower_name = cname.lower()
        if any(kw in lower_name for kw in categorical_keywords):
            return True
        # Also profile short text columns
        return True
    return False


def _is_numeric(dtype: str) -> bool:
    numeric_types = {
        "integer", "bigint", "smallint", "numeric", "real",
        "double precision", "decimal", "float", "int",
    }
    return dtype.lower() in numeric_types


def _is_date(dtype: str) -> bool:
    date_types = {
        "date", "timestamp", "timestamp without time zone",
        "timestamp with time zone", "timestamptz",
    }
    return dtype.lower() in date_types


def _profile_categorical(conn, table: str, col: str, total_rows: int) -> str | None:
    """Get distinct values for categorical columns (up to 25 values)."""
    result = conn.execute(text(
        f'SELECT "{col}", count(*) as cnt FROM "{table}" '
        f'WHERE "{col}" IS NOT NULL '
        f'GROUP BY "{col}" ORDER BY cnt DESC LIMIT 25'
    )).fetchall()

    if not result:
        return None

    distinct_count = len(result)

    # Only profile if it's truly categorical (not too many unique values)
    if distinct_count > 20:
        # Check total distinct count
        total_distinct = conn.execute(text(
            f'SELECT count(DISTINCT "{col}") FROM "{table}" WHERE "{col}" IS NOT NULL'
        )).scalar()
        if total_distinct > 50:
            return f"  {col}: {total_distinct} distinct values (high cardinality - not categorical)"

    values_str = ", ".join(
        f"'{r[0]}' ({r[1]} rows)" for r in result[:15]
    )
    return f"  {col}: DISTINCT VALUES = [{values_str}]"


def _profile_numeric(conn, table: str, col: str) -> str | None:
    """Get min, max, avg for numeric columns."""
    result = conn.execute(text(
        f'SELECT min("{col}"), max("{col}"), round(avg("{col}")::numeric, 2) '
        f'FROM "{table}" WHERE "{col}" IS NOT NULL'
    )).fetchone()

    if not result or result[0] is None:
        return None

    return f"  {col}: min={result[0]}, max={result[1]}, avg={result[2]}"


def _profile_date(conn, table: str, col: str) -> str | None:
    """Get date range."""
    result = conn.execute(text(
        f'SELECT min("{col}"), max("{col}") '
        f'FROM "{table}" WHERE "{col}" IS NOT NULL'
    )).fetchone()

    if not result or result[0] is None:
        return None

    return f"  {col}: from {result[0]} to {result[1]}"


def _generate_business_rules(schema: dict[str, list[dict]]) -> str:
    """Auto-infer business rules from column patterns across all tables."""
    rules: list[str] = [
        "=" * 60,
        "BUSINESS INTELLIGENCE RULES — YOU MUST FOLLOW THESE",
        "=" * 60,
    ]

    # ── Rule 0: Query type awareness
    rules.append("")
    rules.append("RULE 0 — KNOW YOUR QUERY TYPE:")
    rules.append("  PRODUCT ATTRIBUTE queries (category, name, weight, details):")
    rules.append("    → Use product/variant catalog tables directly.")
    rules.append("    → No status filter needed.")
    rules.append("  PRODUCT PRICE queries (most expensive, cheapest, price lookup):")
    rules.append("    → Use sales_order_line_pricing.selling_price_per_unit as source of truth.")
    rules.append("    → JOIN to product_master for product_name. GROUP BY to avoid duplicates.")
    rules.append("  TRANSACTIONAL queries (revenue, AOV, order counts, sales trends):")
    rules.append("    → Use sales tables. MUST filter by sales_order.status = 'closed'.")
    rules.append("    → Examples: 'total revenue', 'AOV', 'top customers by spending'")

    # ── Rule 1: Avoiding duplicates
    rules.append("")
    rules.append("RULE 1 — AVOID DUPLICATE ROWS (CRITICAL):")
    rules.append("  When JOINing tables, products may have MULTIPLE variants (different karat, quality, etc.).")
    rules.append("  This causes duplicate product names in results.")
    rules.append("  ALWAYS use one of these to prevent duplicates:")
    rules.append("    - GROUP BY product_id (or product_name) with MAX/MIN/AVG on value columns")
    rules.append("    - SELECT DISTINCT when you only need unique values")
    rules.append("    - Use subqueries with aggregation before joining")
    rules.append("  NEVER return raw joins that produce repeated product names.")

    # ── Rule 2: Product price lookup
    rules.append("")
    rules.append("RULE 2 — PRODUCT PRICE LOOKUP (SOURCE OF TRUTH):")
    rules.append("  The SOURCE OF TRUTH for product prices is the sales_order_line_pricing table.")
    rules.append("  It has 'selling_price_per_unit' which is the actual price per 1 unit of a product.")
    rules.append("  For 'most expensive products', 'cheapest products', 'product price':")
    rules.append("    → Query sales_order_line_pricing and JOIN to product tables for product_name")
    rules.append("    → Use selling_price_per_unit (NOT line_total, NOT selling_price from catalog)")
    rules.append("    → GROUP BY product_id, product_name and use MAX(selling_price_per_unit)")
    rules.append("    → Join path: sales_order_line_pricing.product_id = product_master.product_id")
    rules.append("  Do NOT use product_variant.selling_price as price — it is catalog/list price, not transaction price.")
    rules.append("  — those are catalog/list prices, not actual transaction prices.")
    rules.append("  For 'highest revenue products' or 'best selling products':")
    rules.append("    → Use SUM(line_total) grouped by product, filtered by status='closed'")

    # ── Rule 3: Status filtering (only for transactional queries)
    rules.append("")
    rules.append("RULE 3 — STATUS FILTERING (TRANSACTIONAL ONLY):")
    rules.append("  The 'status' column on the sales_order table has values: closed, open, cancelled, processing.")
    rules.append("  For revenue, AOV, sales counts: WHERE status = 'closed'")
    rules.append("  For product catalog queries: NO status filter needed")
    rules.append("  IMPORTANT: The 'status' column is ONLY on the sales_order table.")
    rules.append("  Do NOT look for payment_status or status on pricing/line tables — it does not exist there.")

    # ── Rule 4: Unit price vs total price
    rules.append("")
    rules.append("RULE 4 — UNIT PRICE vs TOTAL PRICE:")
    rules.append("  line_total = selling_price_per_unit × quantity (total for order line)")
    rules.append("  selling_price_per_unit = the actual price of 1 unit of the product")
    rules.append("  base_price_per_unit = cost price of 1 unit before margin")
    rules.append("  NEVER use line_total as a product's price — it includes quantity.")
    rules.append("  To get a product's price: use selling_price_per_unit or selling_price column")

    # ── Rule 5: Common metrics formulas
    rules.append("")
    rules.append("RULE 5 — METRIC FORMULAS:")
    rules.append("  AOV = SUM(so.total_amount) / COUNT(DISTINCT so.so_id) WHERE so.status='closed'")
    rules.append("  Revenue = SUM(so.total_amount) WHERE so.status='closed'")
    rules.append("  Most Expensive Product = MAX(pv.selling_price_per_unit) FROM sales_order_line_pricing pv GROUP BY product_id")
    rules.append("  Margin % = (selling_price - base_price) / selling_price × 100")
    rules.append("  Order Count = COUNT(DISTINCT so.so_id) WHERE so.status='closed'")

    # ── Rule 6: Table relationships
    rules.append("")
    rules.append("RULE 6 — TABLE JOIN PATHS:")
    rules.append("  Sales chain: sales_order(so_id) → sales_order_line(so_id, sol_id) → sales_order_line_pricing(sol_id)")
    rules.append("  Gold detail: sales_order_line(sol_id) → sales_order_line_gold(sol_id) [gold_kt, gold_amount_per_unit, etc.]")
    rules.append("  Diamond detail: sales_order_line(sol_id) → sales_order_line_diamond(sol_id) [carats, rate, quality, etc.]")
    rules.append("  Product chain: product_master(product_id) → product_variant(product_id, variant_sku)")
    rules.append("  Sales ↔ Product: sales_order_line.variant_sku = product_variant.variant_sku")
    rules.append("  Sales ↔ Customer: sales_order.customer_id = customer_master.customer_id")
    rules.append("  Sales ↔ Payment: sales_order.so_id = sales_order_payments.so_id")
    rules.append("  PO chain: purchase_order(po_id) → po_line_items(po_id, pol_id, sol_id)")
    rules.append("  PO pricing: po_line_items(pol_id) → po_line_pricing(pol_id)")
    rules.append("  PO ↔ Sales (link): sales_allocation(po_id, so_id, sol_id, pol_id) — many-to-many bridge")
    rules.append("  PO ↔ Vendor: purchase_order.vendor_id = vendor_master.vendor_id")

    return "\n".join(rules)

