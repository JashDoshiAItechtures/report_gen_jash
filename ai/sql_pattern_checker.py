"""Programmatic SQL pattern checker.

Detects known bad SQL patterns that LLMs generate incorrectly even when
instructed otherwise.  Each detector returns a structured issue dict so the
pipeline can trigger a targeted LLM repair with a precise explanation.
"""

import re
from typing import Any


def _build_alias_map(sql: str) -> dict[str, str]:
    """Extract alias → full_table_name mapping from FROM / JOIN clauses.

    Handles:  FROM table_name alias
              FROM table_name AS alias
              JOIN table_name alias
              JOIN table_name AS alias
    Returns lower-cased keys and values.
    """
    alias_map: dict[str, str] = {}
    pattern = re.compile(
        r'(?:FROM|JOIN)\s+"?(\w+)"?\s+(?:AS\s+)?"?(\w+)"?',
        re.IGNORECASE,
    )
    for table, alias in pattern.findall(sql):
        alias_map[alias.lower()] = table.lower()
        # also map table → table in case no alias is used
        alias_map[table.lower()] = table.lower()
    return alias_map


def check_column_table_mismatches(sql: str) -> list[dict[str, Any]]:
    """Schema-aware check: detect alias.column references where the column
    does not exist in the aliased table.

    Uses the live database schema so it works for ANY table/column — nothing
    is hardcoded.  Returns issue dicts in the same format as check_sql_patterns.
    """
    try:
        from db.schema import get_schema
        schema = get_schema()
    except Exception:
        return []   # schema unavailable, skip check

    # Build {table_name_lower: {col_lower, ...}}
    table_cols: dict[str, set[str]] = {
        t.lower(): {c["column_name"].lower() for c in cols}
        for t, cols in schema.items()
    }

    alias_map = _build_alias_map(sql)
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Find all alias.column references in the SQL
    for alias, col in re.findall(r'\b(\w+)\.(\w+)\b', sql):
        alias_l = alias.lower()
        col_l   = col.lower()
        key = f"{alias_l}.{col_l}"
        if key in seen:
            continue
        seen.add(key)

        table_l = alias_map.get(alias_l)
        if table_l is None:
            continue  # unknown alias (subquery alias, CTE name, etc.) — skip
        if table_l not in table_cols:
            continue  # table not in schema — already caught by schema validator

        if col_l not in table_cols[table_l]:
            # Find which tables DO have this column
            tables_with_col = [
                t for t, cols in table_cols.items() if col_l in cols
            ]
            # Build a helpful correction hint
            if tables_with_col:
                hint = (
                    f"Column '{col}' does NOT exist in '{table_l}'. "
                    f"It is available in: {', '.join(tables_with_col)}. "
                    f"JOIN the correct table on sol_id / so_id / po_id as appropriate "
                    f"and reference that table's alias instead."
                )
            else:
                hint = (
                    f"Column '{col}' does NOT exist in '{table_l}' "
                    f"or any other table in the schema. "
                    f"Remove it or use a column that actually exists."
                )
            issues.append({
                "pattern_name": f"wrong_table_for_{col_l}",
                "description": (
                    f"CRITICAL BUG — column '{col}' referenced via alias '{alias}' "
                    f"which maps to table '{table_l}', but that column does not exist there."
                ),
                "correction": hint,
            })

    return issues


def check_sql_patterns(sql: str) -> list[dict[str, Any]]:
    """Detect known bad patterns in a generated SQL string.

    Returns a list of issue dicts:
      {
        "pattern_name": str,   # short id
        "description":  str,   # what is wrong and why
        "correction":   str,   # exact fix to apply
      }
    An empty list means no issues found.
    """
    issues: list[dict[str, Any]] = []
    sql_lower = sql.lower()

    # ── Pattern 1 ────────────────────────────────────────────────────────────
    # Fan-out: purchase_order JOINed to sales_allocation with SUM but no
    # DISTINCT subquery → total_amount counted once per linked SO.
    if (
        "sales_allocation" in sql_lower
        and "purchase_order" in sql_lower
        and re.search(r"\bsum\s*\(", sql_lower)
        and "distinct" not in sql_lower
    ):
        issues.append({
            "pattern_name": "fanout_po_link",
            "description": (
                "CRITICAL BUG — fan-out on sales_allocation: "
                "sales_allocation has MULTIPLE rows per po_id "
                "(one per linked sales order). Joining purchase_order to this table "
                "and then doing SUM(total_amount) counts the same PO amount 2-3 times, "
                "producing an inflated result (e.g. ₹4,239 Cr instead of ₹1,580 Cr)."
            ),
            "correction": (
                "Wrap purchase_order in a DISTINCT subquery FIRST, then aggregate outside:\n"
                "\n"
                "CORRECT pattern:\n"
                "SELECT vendor_id, SUM(total_amount) AS total_value\n"
                "FROM (\n"
                "    SELECT DISTINCT po.po_id, po.vendor_id, po.total_amount\n"
                "    FROM purchase_order po\n"
                "    JOIN sales_allocation pl ON po.po_id = pl.po_id\n"
                "    JOIN sales_order so ON pl.so_id = so.so_id\n"
                "    WHERE so.status = 'closed'\n"
                ") deduped\n"
                "GROUP BY vendor_id\n"
                "ORDER BY total_value DESC\n"
                "\n"
                "NEVER do: SUM(po.total_amount) directly after joining sales_allocation."
            ),
        })

    # ── Pattern 2 ────────────────────────────────────────────────────────────
    # LAG/LEAD window function with ORDER BY that includes month but not year.
    # The data spans multiple years — ordering by month alone compares months
    # across different years incorrectly.
    if re.search(r"\blag\s*\(|\blead\s*\(", sql_lower):
        # Find all OVER (...) clauses
        over_blocks = re.findall(
            r"over\s*\(([^)]*order\s+by[^)]*)\)", sql, re.IGNORECASE
        )
        for block in over_blocks:
            block_lower = block.lower()
            has_month = bool(re.search(r"\bmonth\b", block_lower))
            has_year  = bool(re.search(r"\byear\b",  block_lower))
            if has_month and not has_year:
                issues.append({
                    "pattern_name": "lag_month_only_order",
                    "description": (
                        "CRITICAL BUG — LAG/LEAD window function orders by MONTH only. "
                        "The sales data spans multiple years (2024–2026). Ordering by month "
                        "alone makes the window function compare months across different years "
                        "(e.g. December 2024 followed by January 2024 instead of January 2025), "
                        "producing incorrect growth rates."
                    ),
                    "correction": (
                        "Always ORDER BY YEAR first, then MONTH inside window functions:\n"
                        "\n"
                        "CORRECT pattern:\n"
                        "WITH monthly AS (\n"
                        "    SELECT\n"
                        "        EXTRACT(YEAR  FROM order_date::date) AS yr,\n"
                        "        EXTRACT(MONTH FROM order_date::date) AS mo,\n"
                        "        SUM(total_amount) AS revenue\n"
                        "    FROM sales_order\n"
                        "    GROUP BY yr, mo\n"
                        ")\n"
                        "SELECT yr, mo, revenue,\n"
                        "       LAG(revenue) OVER (ORDER BY yr ASC, mo ASC) AS prev_revenue\n"
                        "FROM monthly\n"
                        "ORDER BY yr ASC, mo ASC\n"
                        "\n"
                        "Also GROUP BY yr, mo — never just mo."
                    ),
                })
                break  # one report is enough

    # ── Pattern 3 ────────────────────────────────────────────────────────────
    # IGI/NC read from the 'quality' column of diamond tables.
    # The quality column holds diamond grades (e.g. 'GH VVS'), never 'IGI'/'NC'.
    # Certification is always in the last segment of variant_sku.
    if re.search(r"\bquality\b", sql_lower):
        # Check if IGI or NC appear as filter values near the quality column
        if re.search(
            r"quality\s*(=|in\s*\(|like)\s*['\"]?\s*(igi|nc|non.?igi|non.?certified)",
            sql_lower,
        ):
            issues.append({
                "pattern_name": "igi_nc_from_quality_column",
                "description": (
                    "CRITICAL BUG — IGI/NC filtered from the quality column. "
                    "The quality column in diamond tables contains diamond grades "
                    "like 'GH VVS', 'EF VVS-VS' — the values 'IGI' and 'NC' do NOT "
                    "exist there, so this filter always returns zero rows."
                ),
                "correction": (
                    "Read certification from the LAST segment of variant_sku:\n"
                    "  IGI certified  → variant_sku LIKE '%-IGI'\n"
                    "  Non-certified  → variant_sku LIKE '%-NC'\n"
                    "\n"
                    "Apply on sales_order_line or sales_order_line_pricing.\n"
                    "\n"
                    "CORRECT pattern (customers with both IGI and NC in same order):\n"
                    "SELECT customer_id FROM sales_order so\n"
                    "WHERE so.so_id IN (\n"
                    "    SELECT so_id FROM sales_order_line\n"
                    "    WHERE variant_sku LIKE '%-IGI'\n"
                    "    INTERSECT\n"
                    "    SELECT so_id FROM sales_order_line\n"
                    "    WHERE variant_sku LIKE '%-NC'\n"
                    ")"
                ),
            })

    # ── Pattern 1a ───────────────────────────────────────────────────────────
    # Missing DISTINCT when selecting a header ID after joining to line tables.
    # One header (so_id / po_id) matches many line rows → same ID repeated per line.
    # Detectable: SELECT has a header ID, JOINs include line tables, no DISTINCT,
    # no aggregation (COUNT/SUM/AVG/etc.) in the SELECT list.
    SALES_LINE_TABLES_SET = {
        "sales_order_line",
        "sales_order_line_pricing",
        "sales_order_line_gold",
        "sales_order_line_diamond",
        "po_line_items",
        "po_line_pricing",
        "po_line_diamond",
        "po_line_gold",
    }
    HEADER_IDS = {"so_id", "po_id", "sol_id", "pol_id"}

    tables_referenced = {t.lower() for t in re.findall(r'\b(\w+)\b', sql_lower)}
    joins_line_table = bool(SALES_LINE_TABLES_SET & tables_referenced)

    if joins_line_table:
        # Extract SELECT list (between SELECT and FROM)
        select_match = re.search(r'\bselect\b(.*?)\bfrom\b', sql_lower, re.DOTALL)
        if select_match:
            select_list = select_match.group(1).strip()
            has_distinct = select_list.startswith("distinct")
            has_aggregation = bool(re.search(r'\b(sum|count|avg|min|max)\s*\(', select_list))
            # Check if only header IDs (and maybe names) are selected
            selected_cols = {c.strip().split('.')[-1].split(' ')[0]
                             for c in select_list.split(',')}
            selects_only_header_id = bool(HEADER_IDS & selected_cols) and not has_aggregation

            if selects_only_header_id and not has_distinct:
                issues.append({
                    "pattern_name": "missing_distinct_header_id_with_line_join",
                    "description": (
                        "DUPLICATE ROWS — selecting a header ID (so_id/po_id) after joining "
                        "to line-level tables without DISTINCT. One order can have many line "
                        "items; without DISTINCT the same so_id appears once per matching "
                        "line, inflating row count (e.g. 11,111 rows instead of 8,079 orders)."
                    ),
                    "correction": (
                        "Add DISTINCT immediately after SELECT:\n"
                        "  WRONG:   SELECT so.so_id FROM ... JOIN sales_order_line ...\n"
                        "  CORRECT: SELECT DISTINCT so.so_id FROM ... JOIN sales_order_line ...\n"
                        "\n"
                        "Also: when comparing per-unit columns against each other in WHERE, "
                        "do not multiply both sides by quantity — it cancels out:\n"
                        "  REDUNDANT:  making_charges_per_unit * quantity > diamond_amount_per_unit * quantity\n"
                        "  SIMPLIFIED: making_charges_per_unit > diamond_amount_per_unit"
                    ),
                })

    # ── Pattern 1b ───────────────────────────────────────────────────────────
    # "Top X per group" answered as a global sort instead of PARTITION BY ranking.
    #
    # Symptom: GROUP BY has 2+ columns, ORDER BY present, no LIMIT (all rows
    # returned) and no window ranking function (ROW_NUMBER/RANK/DENSE_RANK/
    # PARTITION BY). This returns every group-entity combination sorted globally
    # instead of the top-1 (or top-N) within each group.
    #
    # Example: "top customer per city"
    #   WRONG: GROUP BY city, customer ORDER BY revenue DESC  → 126 rows (all)
    #   RIGHT: ROW_NUMBER() OVER (PARTITION BY city ORDER BY revenue DESC), WHERE rnk=1
    has_window_ranking = bool(re.search(
        r"\b(?:row_number|rank|dense_rank)\s*\(|\bpartition\s+by\b",
        sql_lower,
    ))
    has_limit = bool(re.search(r"\blimit\s+\d+", sql_lower))
    has_order_by = bool(re.search(r"\border\s+by\b", sql_lower))

    if not has_window_ranking and has_order_by and not has_limit:
        # Count distinct columns in GROUP BY clause
        group_by_match = re.search(r"\bgroup\s+by\b(.+?)(?:\border\s+by\b|\blimit\b|\bhaving\b|$)",
                                   sql_lower, re.DOTALL)
        if group_by_match:
            group_cols = [c.strip() for c in group_by_match.group(1).split(",") if c.strip()]
            if len(group_cols) >= 2:
                issues.append({
                    "pattern_name": "top_per_group_missing_partition_by",
                    "description": (
                        "POSSIBLE WRONG RESULT — 'top per group' answered as a global sort. "
                        "The query uses GROUP BY with multiple columns and ORDER BY, but has "
                        "no PARTITION BY or ROW_NUMBER/RANK window function and no LIMIT. "
                        "This returns ALL rows sorted globally — not one top row per group. "
                        "For questions like 'top customer per city' or 'best product per category', "
                        "you must use ROW_NUMBER() OVER (PARTITION BY group_col ORDER BY metric DESC) "
                        "in a subquery, then filter WHERE rnk = 1 outside."
                    ),
                    "correction": (
                        "Re-read the question. If it asks for the top item WITHIN each group "
                        "(e.g. 'per city', 'per category', 'for each X'), use this pattern:\n"
                        "\n"
                        "SELECT group_col, entity_col, metric\n"
                        "FROM (\n"
                        "    SELECT group_col, entity_col,\n"
                        "           SUM(metric_col) AS metric,\n"
                        "           ROW_NUMBER() OVER (\n"
                        "               PARTITION BY group_col\n"
                        "               ORDER BY SUM(metric_col) DESC\n"
                        "           ) AS rnk\n"
                        "    FROM ...\n"
                        "    WHERE so.status = 'closed'\n"
                        "    GROUP BY group_col, entity_col\n"
                        ") t\n"
                        "WHERE rnk = 1\n"
                        "ORDER BY metric DESC\n"
                        "\n"
                        "If the question asks for a global top (not per group), add LIMIT N "
                        "to the original query instead."
                    ),
                })

    # ── Pattern 2a ───────────────────────────────────────────────────────────
    # Cumulative/running window applied directly to raw table rows without
    # pre-aggregating by date.  SUM(...) OVER (ORDER BY date) on a raw scan
    # produces one row per ORDER, not one per date.
    # Detectable: OVER (ORDER BY ...) present + no subquery/CTE with GROUP BY.
    if re.search(r"\bover\s*\(.*?order\s+by\b", sql_lower, re.DOTALL):
        has_window = bool(re.search(r"\bsum\s*\([^)]+\)\s+over\s*\(", sql_lower))
        # Count how many times SELECT appears — more than one means a subquery exists
        select_count = len(re.findall(r"\bselect\b", sql_lower))
        # GROUP BY anywhere in the SQL (covers both CTE and inline subquery patterns)
        has_any_group_by = bool(re.search(r"\bgroup\s+by\b", sql_lower))
        # If there's a subquery (multiple SELECTs) with GROUP BY, treat it as pre-aggregated
        has_pre_aggregation = has_any_group_by and select_count > 1
        if has_window and not has_pre_aggregation:
            issues.append({
                "pattern_name": "cumulative_window_without_pre_aggregation",
                "description": (
                    "WRONG RESULT — SUM(...) OVER (ORDER BY date) applied directly to raw rows. "
                    "With multiple orders per date, the window produces one cumulative value "
                    "per ORDER ROW, not per date — same date appears multiple times with "
                    "different cumulative totals. The correct approach is to GROUP BY date "
                    "first in a subquery, then apply the cumulative window on top."
                ),
                "correction": (
                    "Aggregate by date first, then apply the window:\n"
                    "\n"
                    "CORRECT:\n"
                    "SELECT order_date, daily_revenue,\n"
                    "       SUM(daily_revenue) OVER (ORDER BY order_date) AS cumulative_revenue\n"
                    "FROM (\n"
                    "    SELECT order_date::date AS order_date,\n"
                    "           SUM(total_amount) AS daily_revenue\n"
                    "    FROM sales_order\n"
                    "    WHERE status = 'closed'\n"
                    "    GROUP BY order_date::date\n"
                    ") t\n"
                    "ORDER BY order_date"
                ),
            })

    # ── Pattern 2b ───────────────────────────────────────────────────────────
    # "Top N for BOTH metric A and metric B" — using ORDER BY a, b LIMIT N
    # ranks by a (b is just tiebreaker). Needs two independent RANK() windows.
    # Detectable: ORDER BY has two or more columns AND LIMIT present AND no RANK/ROW_NUMBER.
    if (
        re.search(r"\border\s+by\b[^;]+,", sql_lower)        # ORDER BY with multiple cols
        and re.search(r"\blimit\s+\d+", sql_lower)
        and not re.search(r"\b(?:rank|row_number|dense_rank)\s*\(", sql_lower)
        and re.search(r"\bsum\s*\(", sql_lower)               # aggregation present
    ):
        issues.append({
            "pattern_name": "dual_metric_limit_not_dual_rank",
            "description": (
                "POSSIBLE BUG — ORDER BY metricA, metricB LIMIT N is NOT two independent "
                "rankings. metricB is only a tiebreaker; the LIMIT picks top-N by metricA. "
                "If the question asks for items that rank in the top N for BOTH metrics "
                "independently, you must use two separate RANK() window functions."
            ),
            "correction": (
                "Use two independent RANK() windows and filter where both ranks <= N:\n"
                "\n"
                "SELECT * FROM (\n"
                "    SELECT product_id,\n"
                "           SUM(metric_a) AS metric_a,\n"
                "           SUM(metric_b) AS metric_b,\n"
                "           RANK() OVER (ORDER BY SUM(metric_a) DESC) AS rank_a,\n"
                "           RANK() OVER (ORDER BY SUM(metric_b) DESC) AS rank_b\n"
                "    FROM ...\n"
                "    GROUP BY product_id\n"
                ") t\n"
                "WHERE rank_a <= N AND rank_b <= N"
            ),
        })

    # ── Pattern 3a ───────────────────────────────────────────────────────────
    # WHERE status filter alongside CASE WHEN status — wrong denominator.
    # When computing "percentage of X vs Y", the WHERE clause must NOT pre-filter
    # by status because that shrinks the denominator (misses open/processing orders).
    # CASE WHEN inside SUM() handles the split; no WHERE on status needed.
    if (
        re.search(r"\bcase\s+when\b.*?\bstatus\b", sql_lower, re.DOTALL)
        and re.search(r"\bwhere\b.*?\bstatus\s+in\s*\(", sql_lower, re.DOTALL)
        and re.search(r"\bsum\s*\(", sql_lower)
    ):
        issues.append({
            "pattern_name": "case_when_status_with_where_filter",
            "description": (
                "WRONG DENOMINATOR — a WHERE status IN (...) filter is combined with "
                "CASE WHEN so.status = ... inside SUM(). "
                "The WHERE clause removes rows before aggregation, making the denominator "
                "(SUM of all orders) too small and inflating every percentage. "
                "For percentage breakdowns across statuses, the CASE WHEN handles the split "
                "and the WHERE clause on status must be removed."
            ),
            "correction": (
                "Remove the WHERE status filter. Let CASE WHEN handle the split:\n"
                "\n"
                "CORRECT pattern:\n"
                "SELECT cm.customer_id, cm.customer_name,\n"
                "    ROUND((SUM(CASE WHEN so.status = 'closed' THEN so.total_amount ELSE 0 END)\n"
                "           * 100.0 / SUM(so.total_amount))::numeric, 2) AS pct_closed,\n"
                "    ROUND((SUM(CASE WHEN so.status = 'cancelled' THEN so.total_amount ELSE 0 END)\n"
                "           * 100.0 / SUM(so.total_amount))::numeric, 2) AS pct_cancelled\n"
                "FROM sales_order so\n"
                "JOIN customer_master cm ON so.customer_id = cm.customer_id\n"
                "GROUP BY cm.customer_id, cm.customer_name\n"
                "\n"
                "No WHERE on status — SUM(so.total_amount) must include ALL orders as denominator."
            ),
        })

    # ── Pattern 3b ───────────────────────────────────────────────────────────
    # "per order" metric computed with SUM(quantity) as denominator instead of
    # COUNT(DISTINCT so_id).  SUM(quantity) = revenue per unit; "per order"
    # requires COUNT(DISTINCT so_id).
    # Heuristic: division where the denominator contains sum(...quantity...)
    if re.search(r"/\s*sum\s*\([^)]*quantit", sql_lower):
        issues.append({
            "pattern_name": "per_unit_instead_of_per_order",
            "description": (
                "POSSIBLE BUG — dividing by SUM(quantity) gives revenue per UNIT (per piece). "
                "If the question asks for 'per order', the denominator must be "
                "COUNT(DISTINCT so_id), not SUM(quantity). "
                "These are completely different metrics: "
                "SUM(line_total)/SUM(quantity) = avg revenue per item sold; "
                "SUM(line_total)/COUNT(DISTINCT so_id) = avg revenue each time product appears in an order."
            ),
            "correction": (
                "Check the question: does it say 'per order' or 'per unit/piece'?\n"
                "  'per order'    → SUM(lp.line_total) / COUNT(DISTINCT so.so_id)\n"
                "  'per unit'     → SUM(lp.line_total) / SUM(lp.quantity)\n"
                "  'per customer' → SUM(lp.line_total) / COUNT(DISTINCT so.customer_id)\n"
                "If the question says 'per order', rewrite using COUNT(DISTINCT so.so_id)."
            ),
        })

    # ── Pattern 3c ───────────────────────────────────────────────────────────
    # PostgreSQL ROUND() requires numeric, not double precision.
    # ROUND(expr, N) fails with "function round(double precision, integer) does not exist"
    # if expr evaluates to double precision. Fix: cast to ::numeric before ROUND().
    if re.search(r"\bround\s*\(", sql_lower):
        # Check if any ROUND( call lacks a ::numeric cast inside it
        round_calls = re.findall(r"round\s*\(([^;]+?),\s*\d+\s*\)", sql, re.IGNORECASE)
        for call in round_calls:
            if "::numeric" not in call.lower() and "::decimal" not in call.lower():
                issues.append({
                    "pattern_name": "round_missing_numeric_cast",
                    "description": (
                        "PostgreSQL TYPE ERROR — ROUND(value, N) only accepts numeric as first "
                        "argument. If value is double precision (e.g. result of division or "
                        "SUM()), PostgreSQL raises: "
                        "'function round(double precision, integer) does not exist'. "
                        "You must cast to ::numeric before calling ROUND."
                    ),
                    "correction": (
                        "Always cast the expression to ::numeric inside ROUND:\n"
                        "  WRONG:   ROUND(SUM(x) * 100.0 / SUM(y), 2)\n"
                        "  CORRECT: ROUND((SUM(x) * 100.0 / SUM(y))::numeric, 2)\n"
                        "\n"
                        "Apply this to every ROUND(..., N) call in the query."
                    ),
                })
                break  # one report per query is enough

    # ── Pattern 4 ────────────────────────────────────────────────────────────
    # Schema-aware: detect alias.column where column doesn't exist in that table.
    # Generic — works for gold_kt on pricing table, or any future similar mistake.
    issues.extend(check_column_table_mismatches(sql))

    # ── Pattern 5 ────────────────────────────────────────────────────────────
    # Sales line tables used without joining sales_order for the status filter.
    # Any query on line-level tables (pricing, gold, diamond, sales_order_line)
    # must join back to sales_order and apply status = 'closed'
    # unless a different status is explicitly present in the SQL.
    SALES_LINE_TABLES = {
        "sales_order_line_pricing",
        "sales_order_line_gold",
        "sales_order_line_diamond",
        "sales_order_line",
    }
    SALES_HEADER = "sales_order"

    tables_in_sql = {t.lower() for t in re.findall(r'\b(\w+)\b', sql_lower)}
    uses_line_table = bool(SALES_LINE_TABLES & tables_in_sql)
    has_sales_header = SALES_HEADER in tables_in_sql
    has_status_filter = bool(re.search(r"\bstatus\s*=", sql_lower))

    if uses_line_table and not has_status_filter:
        # Only flag if the header table is absent (status can't be filtered)
        # OR if the header is present but status filter is still missing
        issues.append({
            "pattern_name": "missing_status_closed_on_line_tables",
            "description": (
                "MISSING status = 'closed' filter: the query uses sales line tables "
                "(sales_order_line_pricing / sales_order_line_gold / sales_order_line_diamond) "
                "but does not filter by sales_order status. "
                "Line tables have no status column — you must JOIN sales_order "
                "and add WHERE so.status = 'closed' to exclude incomplete/cancelled orders."
            ),
            "correction": (
                "Add a JOIN to sales_order and filter by status:\n"
                "\n"
                "JOIN sales_order_line     sol ON lp.sol_id = sol.sol_id\n"
                "JOIN sales_order          so  ON sol.so_id = so.so_id\n"
                "WHERE so.status = 'closed'\n"
                "\n"
                "Full corrected structure example:\n"
                "SELECT g.gold_kt,\n"
                "       SUM(lp.gold_amount_per_unit    * lp.quantity) AS total_gold_amount,\n"
                "       SUM(lp.diamond_amount_per_unit * lp.quantity) AS total_diamond_amount,\n"
                "       SUM(lp.making_charges_per_unit * lp.quantity) AS total_making_charges\n"
                "FROM sales_order_line_pricing lp\n"
                "JOIN sales_order_line_gold g  ON lp.sol_id = g.sol_id\n"
                "JOIN sales_order_line     sol ON lp.sol_id = sol.sol_id\n"
                "JOIN sales_order          so  ON sol.so_id = so.so_id\n"
                "WHERE so.status = 'closed'\n"
                "GROUP BY g.gold_kt\n"
                "ORDER BY g.gold_kt"
            ),
        })

    return issues


def format_issues_for_repair(issues: list[dict[str, Any]]) -> str:
    """Format detected issues into a clear repair instruction for the LLM."""
    lines = [
        "YOUR GENERATED SQL HAS THE FOLLOWING CRITICAL BUGS — REWRITE TO FIX ALL OF THEM:\n"
    ]
    for i, issue in enumerate(issues, 1):
        lines.append(f"BUG {i}: {issue['description']}")
        lines.append(f"FIX {i}: {issue['correction']}")
        lines.append("")
    return "\n".join(lines)
