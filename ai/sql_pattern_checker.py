"""Programmatic SQL pattern checker.

Detects known bad SQL patterns that LLMs generate incorrectly even when
instructed otherwise.  Each detector returns a structured issue dict so the
pipeline can trigger a targeted LLM repair with a precise explanation.
"""

import re
from typing import Any


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
