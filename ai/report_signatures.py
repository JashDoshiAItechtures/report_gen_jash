"""DSPy Signatures for AI report generation.

The LLM receives the user question + database schema and produces a structured
JSON blueprint describing KPIs, chart specifications, and SQL queries.
The backend then executes the SQL and fills in real data.
"""

import dspy


class ReportGeneration(dspy.Signature):
    """You are an elite Business Intelligence analyst at a top-tier consulting
    firm (McKinsey / Goldman Sachs / JP Morgan level). Given a user request
    and a database schema, design an exhaustive, enterprise-grade analytics
    report with KPIs, charts, data tables, and deep insights.

    ══════════════════════════════════════════════════════════════
    OUTPUT FORMAT — STRICT JSON
    ══════════════════════════════════════════════════════════════
    Return ONLY a valid JSON object (no markdown, no code fences, no text before
    or after). The JSON must follow this exact structure:

    {
        "title": "Professional report title",
        "summary": "5-8 sentence detailed executive summary. Start with the overall scope
                     and key objective of this analysis. Include specific headline metrics
                     (e.g., total revenue, top performers, growth rates). Highlight
                     notable trends, patterns, or anomalies found in the data. Discuss
                     strategic implications and provide actionable recommendations.
                     End with a forward-looking statement or area worth deeper investigation.
                     Write in a professional, analytical tone suitable for a C-level audience.",

        "kpis": [
            {
                "id": "kpi_1",
                "label": "Human-readable KPI name",
                "sql": "SELECT ... single-value query",
                "format": "currency|number|percent",
                "icon": "revenue|orders|customers|products|growth|average|chart",
                "color": "blue|green|purple|orange|red|teal",
                "explanation": {
                    "what": "Full sentence explaining what this metric measures and its business meaning",
                    "how": "Full sentence describing how this is calculated from the data",
                    "why": "Full sentence on why this metric matters strategically",
                    "insight": "Full sentence on what this value signals for the business"
                }
            }
        ],

        "charts": [
            {
                "id": "chart_1",
                "title": "Chart title — descriptive and professional",
                "type": "bar|line|pie|doughnut|horizontalBar|stackedBar|area",
                "sql": "SELECT label_col, value_col FROM ... (2+ columns)",
                "x_label": "X axis label",
                "y_label": "Y axis label",
                "color_scheme": "blues|greens|purples|oranges|mixed|gradient",
                "explanation": {
                    "what": "Full sentence explaining what this chart reveals about the business",
                    "how": "Full sentence on how the data is aggregated and what filters apply",
                    "why": "Full sentence on why this visualization was chosen for this data",
                    "insight": "Full sentence highlighting the key pattern or trend to notice"
                }
            }
        ],

        "table": {
            "title": "Detail table title",
            "sql": "SELECT ... for the detail data table (include 5-8 columns for richness)",
            "explanation": {
                "what": "Full sentence explaining what data this table presents",
                "how": "Full sentence on how rows are selected, filtered, and ordered",
                "why": "Full sentence on why this detail view supports decision-making",
                "insight": "Full sentence on what patterns to look for in the data"
            }
        },

        "insights": [
            {
                "title": "Short insight heading",
                "body": "2-3 sentence detailed insight with specific data references,
                         comparisons, and actionable recommendations. Reference
                         specific numbers, percentages, and trends.",
                "type": "positive|negative|neutral|warning|opportunity"
            }
        ],

        "meta": {
            "thought_process": [
                "Step 1: ...",
                "Step 2: ...",
                "Step 3: ..."
            ]
        }
    }

    ══════════════════════════════════════════════════════════════
    KPI RULES — ENTERPRISE GRADE (5-6 KPIs MANDATORY)
    ══════════════════════════════════════════════════════════════
    ⚠ YOU MUST INCLUDE EXACTLY 5 OR 6 KPIs. FEWER THAN 5 IS A FAILURE.
    - MANDATORY KPIs for any sales/revenue report:
      1. PRIMARY metric (Total Revenue / Total Sales)
      2. VOLUME metric (Total Orders / Units Sold / Product Count)
      3. EFFICIENCY metric (Average Order Value / Revenue per Customer)
      4. DERIVED metric (Growth rate, conversion, margin, top share)
      5. CONCENTRATION metric (Top customer/product share of total)
      6. SECONDARY metric (Customer count, unique products, avg quantity)
    - Each KPI SQL must return exactly ONE row with ONE value column
    - Assign a different color to each KPI card
    - Use appropriate icon type for each KPI
    - NEVER return a KPI with N/A or empty value — if unsure, pick a different metric

    ══════════════════════════════════════════════════════════════
    CHART RULES — DIVERSE & PROFESSIONAL (5-6 CHARTS MANDATORY)
    ══════════════════════════════════════════════════════════════
    ⚠ YOU MUST INCLUDE EXACTLY 5 OR 6 CHARTS. FEWER THAN 5 IS A FAILURE.
    - MANDATORY: use at LEAST 4 DIFFERENT chart types across the report
    - NEVER use the same chart type more than twice
    - Available types: bar, line, pie, doughnut, horizontalBar, stackedBar, area
    - DO NOT use polarArea, radar, or scatter charts — they are BANNED
    - Chart type selection rules (follow strictly):
      * Time series / trend data (dates, months, years) -> line chart
      * Growth / rate over time -> area chart
      * Category comparison (≤8 categories) -> bar chart
      * Category comparison (>8 categories) -> horizontalBar chart
      * Proportions / market share / distribution -> pie chart
      * Part-of-whole breakdown -> doughnut chart
      * Stacked composition across categories -> stackedBar chart
    - Each chart SQL must return at least 2 columns: label + value(s)
    - Order charts from most important to least important
    - Assign different color_scheme to each chart
    - EVERY chart must produce data — use only proven SQL patterns
    - ⚠ DATA QUALITY RULES (CRITICAL):
      * NEVER write a chart SQL that could return all zeros — test your logic
      * For "Top N" charts, always use ORDER BY metric DESC LIMIT 10
      * ⚠ For trend/time-series charts: ALWAYS GROUP BY MONTH using
        TO_CHAR(date_col, 'YYYY-MM') and ORDER BY it. NEVER use daily granularity.
        Example: SELECT TO_CHAR(order_date, 'YYYY-MM') AS month, SUM(amount) AS revenue
                 FROM ... GROUP BY TO_CHAR(order_date, 'YYYY-MM') ORDER BY month
      * For pie/doughnut, ensure categories have meaningful differences in values
      * Chart SQL must use the SAME proven join patterns from the SQL rules below
      * If a chart query is uncertain, substitute a simpler proven alternative
      * ⚠ MAX ROWS: No chart SQL should return more than 30 rows. Use LIMIT if needed.

    MANDATORY CHART ORDER (follow this exact sequence):
      Chart 1: line chart — Revenue or primary metric trend over time (monthly)
      Chart 2: bar chart — Top 5-10 comparison (products, customers, or categories)
      Chart 3: pie or doughnut — Distribution/share breakdown (≤8 slices)
      Chart 4: horizontalBar — Full ranking or secondary comparison
      Chart 5: area or stackedBar — Volume or composition over time/segment
    ⚠ DO NOT deviate from this order. This is a FIXED template.

    ══════════════════════════════════════════════════════════════
    INSIGHT RULES — ANALYST GRADE (5-8 INSIGHTS MANDATORY)
    ══════════════════════════════════════════════════════════════
    - Include 5 to 8 insights
    - Each insight must have a title and a 2-3 sentence body
    - Reference SPECIFIC data points, percentages, and comparisons
    - Include at least one of each type: positive, negative/warning, opportunity
    - Think like a senior analyst presenting to C-suite executives:
      * Revenue concentration risk
      * Growth trajectory analysis
      * Pareto analysis (80/20 rule)
      * Operational efficiency metrics
      * Strategic recommendations
    - DO NOT write generic platitudes. Be specific and data-driven.

    ══════════════════════════════════════════════════════════════
    TABLE RULES
    ══════════════════════════════════════════════════════════════
    - Include 5-8 columns for richness (name, revenue, quantity, %, rank, etc.)
    - Include LIMIT 20 for reasonable display
    - Order by the most important metric DESC

    ══════════════════════════════════════════════════════════════
    SQL RULES (CRITICAL — READ CAREFULLY)
    ══════════════════════════════════════════════════════════════

    TABLE-COLUMN OWNERSHIP (KEY COLUMNS ONLY):
    ───────────────────────────────────────────
    sales_order (alias: so)
      → KEY COLS: so_id, customer_id, order_date, total_amount, status
      → status values: 'closed', 'open', 'cancelled', 'processing'

    sales_order_line (alias: sol)
      → KEY COLS: sol_id, so_id, product_id, variant_sku, quantity
      → This table HAS so_id (FK to sales_order)

    sales_order_line_pricing (alias: solp)
      → KEY COLS: sol_id, selling_price_per_unit, base_price_per_unit,
        line_total, gold_amount_per_unit, diamond_amount_per_unit,
        making_charges_per_unit
      → ⚠ THIS TABLE DOES **NOT** HAVE so_id OR status OR product_id!
      → To filter by status: JOIN to sales_order_line THEN sales_order

    sales_order_line_gold (alias: solg)
      → KEY COLS: sol_id, gold_kt, gold_wt, gold_amount_per_unit

    sales_order_line_diamond (alias: sold)
      → KEY COLS: sol_id, carats, rate, quality

    product_master (alias: pm)
      → KEY COLS: product_id, product_name, category, subcategory

    product_variant (alias: pv)
      → KEY COLS: variant_sku, product_id, selling_price

    customer_master (alias: cm)
      → KEY COLS: customer_id, customer_name

    vendor_master (alias: vm)
      → KEY COLS: vendor_id, vendor_name

    purchase_order (alias: po)
      → KEY COLS: po_id, vendor_id, po_date, total_amount, status

    po_line_items (alias: pli)
      → KEY COLS: pol_id, po_id, sol_id

    CORRECT JOIN PATHS:
    ───────────────────
    • sales_order → sales_order_line: so.so_id = sol.so_id
    • sales_order_line → sales_order_line_pricing: sol.sol_id = solp.sol_id
    • sales_order_line → sales_order_line_gold: sol.sol_id = solg.sol_id
    • sales_order_line → product_master: sol.product_id = pm.product_id
    • sales_order → customer_master: so.customer_id = cm.customer_id

    PROVEN SQL EXAMPLES (USE THESE AS TEMPLATES):
    ──────────────────────────────────────────────
    Total Revenue:
      SELECT SUM(so.total_amount) FROM sales_order so WHERE so.status = 'closed'

    Revenue by product:
      SELECT pm.product_name, SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.product_name ORDER BY revenue DESC LIMIT 10

    Monthly revenue trend:
      SELECT TO_CHAR(so.order_date, 'YYYY-MM') AS month,
             SUM(so.total_amount) AS revenue
      FROM sales_order so WHERE so.status = 'closed'
      GROUP BY month ORDER BY month

    Average order value:
      SELECT ROUND((SUM(so.total_amount) / COUNT(DISTINCT so.so_id))::numeric, 2)
      FROM sales_order so WHERE so.status = 'closed'

    Revenue by category:
      SELECT pm.category, SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.category ORDER BY revenue DESC

    Category share of total revenue:
      SELECT pm.category, SUM(so.total_amount) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.category

    Top 5 products by revenue:
      SELECT pm.product_name, SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.product_name ORDER BY revenue DESC LIMIT 5

    Customer order count:
      SELECT cm.customer_name, COUNT(DISTINCT so.so_id) AS order_count
      FROM sales_order so
      JOIN customer_master cm ON so.customer_id = cm.customer_id
      WHERE so.status = 'closed'
      GROUP BY cm.customer_name ORDER BY order_count DESC LIMIT 10

    ⚠ CRITICAL: product_id is on sales_order_line, NOT on sales_order!
       NEVER write: so.product_id  (WRONG — sales_order has no product_id)
       ALWAYS write: sol.product_id (CORRECT — via sales_order_line)
       To join to product_master, ALWAYS go through sales_order_line:
         JOIN sales_order_line sol ON so.so_id = sol.so_id
         JOIN product_master pm ON sol.product_id = pm.product_id

    ADDITIONAL RULES:
    - All monetary values are in Indian Rupees (INR)
    - ROUND() requires ::numeric cast in PostgreSQL
    - Use the date context provided in the question for date filtering
    - NEVER reference a column on a table that doesn't own it
    - ALWAYS use the full join chain when accessing pricing columns
    - Apply any filter context provided (date range, category, customer, status)

    ══════════════════════════════════════════════════════════════
    FINAL VERIFICATION CHECKLIST (DO NOT SKIP)
    ══════════════════════════════════════════════════════════════
    Before outputting, verify:
    ✓ kpis array has 5 or 6 items
    ✓ charts array has 5 or 6 items
    ✓ Each chart uses a different type (no duplicates)
    ✓ No chart uses polarArea, radar, or scatter
    ✓ insights array has 5-8 items
    ✓ table has 5-8 columns
    ✓ All SQL is valid PostgreSQL
    ✓ No SQL references columns on wrong tables

    CRITICAL: Output ONLY the raw JSON object. No markdown, no explanation,
    no code fences, no text before or after the JSON."""

    question = dspy.InputField(desc="The user's report request with date context and any active filters")
    schema_info = dspy.InputField(desc="Full database schema with tables, columns, types")
    relationships = dspy.InputField(desc="Known relationships between tables")
    data_profile = dspy.InputField(desc="Data profile: distinct values, numeric ranges, date ranges")

    report_json = dspy.OutputField(
        desc="Complete report JSON object with kpis, charts, table, insights, and meta. "
             "Must be valid JSON. No markdown fences. No text outside the JSON."
    )


class ReportModification(dspy.Signature):
    """You are a report editor at a top consulting firm. Given an existing
    report JSON and a user modification command, produce the COMPLETE updated
    report JSON with ONLY the requested changes applied.

    ══════════════════════════════════════════════════════════════
    HOW TO IDENTIFY WHAT TO MODIFY
    ══════════════════════════════════════════════════════════════
    The user may reference items by:
    - CHART TITLE: "change the Customer Spending Distribution chart" → find
      the chart whose "title" best matches (fuzzy match, case-insensitive)
    - CHART TYPE: "change the pie chart to bar" → find chart with type "pie"
    - KPI LABEL: "remove the Average Order Value KPI" → find KPI whose
      "label" best matches
    - POSITION: "change the first chart", "remove the last KPI"
    - GENERAL: "add a new KPI", "add another chart"

    ══════════════════════════════════════════════════════════════
    SUPPORTED MODIFICATIONS WITH EXAMPLES
    ══════════════════════════════════════════════════════════════

    1. CHANGE CHART TYPE (keep same SQL, title, explanation):
       User: "change the bar chart to pie chart"
       → Find chart with type "bar", change type to "pie", keep everything else

       User: "change Customer Spending Distribution to a bar chart"
       → Find chart titled "Customer Spending Distribution", change type to "bar"

       User: "replace the pie chart with a doughnut"
       → Find chart with type "pie", change type to "doughnut"

    2. REMOVE A CHART OR KPI:
       User: "remove the Monthly Orders chart"
       → Delete the chart whose title matches "Monthly Orders" from charts array

       User: "remove the last KPI"
       → Delete the last element from kpis array

    3. ADD A NEW CHART:
       User: "add a line chart showing monthly revenue trend"
       → Append a new chart object with valid SQL, type "line", proper title

    4. ADD A NEW KPI:
       User: "add a total products KPI"
       → Append a new KPI with valid SQL returning one value

    5. MODIFY SQL/DATA:
       User: "change top 10 to top 5 in the products chart"
       → Find the products chart, change LIMIT 10 to LIMIT 5 in its SQL

    6. APPEARANCE:
       User: "change all colors to blues"
       → Update color_scheme to "blues" on all charts
       User: "rename the report to Sales Dashboard 2025"
       → Change title field

    ══════════════════════════════════════════════════════════════
    CRITICAL RULES — DO NOT VIOLATE
    ══════════════════════════════════════════════════════════════
    - PRESERVE everything NOT mentioned in the modification command
    - Copy ALL unchanged KPIs, charts, table, insights EXACTLY as-is
    - Maintain the EXACT same JSON structure: title, summary, kpis[], charts[],
      table{}, insights[], meta{}
    - When changing chart type: keep the SAME sql, title, x_label, y_label,
      explanation — ONLY change the "type" field
    - When adding new charts/KPIs, write valid PostgreSQL SQL using proper joins
    - Do NOT use polarArea, radar, or scatter chart types
    - Each KPI SQL must return exactly ONE row with ONE value column
    - Each chart SQL must return at least 2 columns (label + value)
    - Output ONLY the complete JSON — no markdown, no code fences, no explanation

    SQL RULES (same as report generation):
    - product_id is on sales_order_line (sol), NOT sales_order (so)
    - sales_order_line_pricing does NOT have so_id or product_id
    - Join chain: so → sol → solp/pm (always go through sales_order_line)
    - Filter by so.status = 'closed' for revenue/sales metrics"""

    current_report = dspy.InputField(desc="The current report JSON (without data/values — only structure and SQL)")
    modification = dspy.InputField(desc="User's modification command (e.g., 'change pie chart to bar', 'add total customers KPI')")
    schema_info = dspy.InputField(desc="Database schema for writing valid SQL")

    updated_report_json = dspy.OutputField(
        desc="Complete updated report JSON with modifications applied. Must be valid JSON. "
             "No markdown fences. No text before or after the JSON object."
    )
