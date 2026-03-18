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
    # Fan-out: purchase_order JOINed to po_sales_order_link with SUM but no
    # DISTINCT subquery → total_amount counted once per linked SO.
    if (
        "po_sales_order_link" in sql_lower
        and "purchase_order" in sql_lower
        and re.search(r"\bsum\s*\(", sql_lower)
        and "distinct" not in sql_lower
    ):
        issues.append({
            "pattern_name": "fanout_po_link",
            "description": (
                "CRITICAL BUG — fan-out on po_sales_order_link: "
                "purchase_orders_v6_po_sales_order_link has MULTIPLE rows per po_id "
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
                "    FROM purchase_orders_v6_purchase_order po\n"
                "    JOIN purchase_orders_v6_po_sales_order_link pl ON po.po_id = pl.po_id\n"
                "    JOIN sales_table_v2_sales_order so ON pl.so_id = so.so_id\n"
                "    WHERE so.status = 'closed'\n"
                ") deduped\n"
                "GROUP BY vendor_id\n"
                "ORDER BY total_value DESC\n"
                "\n"
                "NEVER do: SUM(po.total_amount) directly after joining po_sales_order_link."
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
                        "    FROM sales_table_v2_sales_order\n"
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
                    "Apply on sales_table_v2_sales_order_line or sales_order_line_pricing.\n"
                    "\n"
                    "CORRECT pattern (customers with both IGI and NC in same order):\n"
                    "SELECT customer_id FROM sales_table_v2_sales_order so\n"
                    "WHERE so.so_id IN (\n"
                    "    SELECT so_id FROM sales_table_v2_sales_order_line\n"
                    "    WHERE variant_sku LIKE '%-IGI'\n"
                    "    INTERSECT\n"
                    "    SELECT so_id FROM sales_table_v2_sales_order_line\n"
                    "    WHERE variant_sku LIKE '%-NC'\n"
                    ")"
                ),
            })

    # ── Pattern 4 ────────────────────────────────────────────────────────────
    # Schema-aware: detect alias.column where column doesn't exist in that table.
    # Generic — works for gold_kt on pricing table, or any future similar mistake.
    issues.extend(check_column_table_mismatches(sql))

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
