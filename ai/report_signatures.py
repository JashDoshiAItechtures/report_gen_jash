"""DSPy Signatures for AI report generation.

The LLM receives the user question + database schema and produces a structured
JSON blueprint describing KPIs, chart specifications, and SQL queries.
The backend then executes the SQL and fills in real data.
"""

import dspy


class ReportGeneration(dspy.Signature):
    """You are an elite Business Intelligence analyst. Given a user request
    and a database schema, design a production-grade analytics report that is
    PRECISELY about what the user asked for.

    ══════════════════════════════════════════════════════════════
    STEP 0 — SUBJECT LOCKING (⚠ DO THIS FIRST — MANDATORY)
    ══════════════════════════════════════════════════════════════
    1. Read the user's question carefully
    2. Extract the EXACT SUBJECT — the specific entity, category, segment,
       product type, or business dimension they want analyzed
    3. LOCK every component of the report to this subject

    SUBJECT LOCK ENFORCEMENT (non-negotiable):
    • Report TITLE must name the subject
    • Report SUMMARY must discuss ONLY the subject
    • ALL 6 KPIs must measure metrics SPECIFIC to the subject
      (prefix each KPI label with the subject qualifier)
    • ALL 6 charts must visualize data SPECIFIC to the subject
      (each chart title must reference the subject)
    • ALL insights must analyze patterns SPECIFIC to the subject
    • The detail TABLE must show ONLY subject-scoped data
    • ALL SQL queries must include JOINs or WHERE filters that
      scope the data to ONLY the subject

    HOW TO SCOPE SQL:
    - Study the schema_info, relationships, and data_profile
    - Identify which tables and columns relate to the subject
    - Use JOINs to relevant detail/dimension tables to filter data
    - If the subject mentions a product type, category, or attribute,
      find the matching table/column in the schema and filter by it
    - If it mentions "top N", enforce LIMIT N across queries

    ⚠ FAILURE = including ANY generic/unscoped component.
    Example: if subject = "gold products", then "Total Revenue" is
    WRONG → "Total Gold Product Revenue" with gold-filtered SQL is RIGHT.

    Record in meta.thought_process:
      "Step 0: SUBJECT = [subject]. Tables: [tables]. Filters: [filters]."

    ══════════════════════════════════════════════════════════════
    STEP 1 — UNDERSTAND THE DATA
    ══════════════════════════════════════════════════════════════
    Study the schema_info, relationships, and data_profile to:
    1. Identify ALL tables and columns relevant to the subject
    2. Understand the join paths between tables
    3. Find actual column values from data_profile for WHERE clauses
    4. Determine the correct status filters for each query type:
       - Revenue/sales metrics → status = 'closed'
       - Active orders → status IN ('open', 'processing')
       - All orders → no status filter or GROUP BY status

    CRITICAL SQL RULES:
    - Generate SQL dynamically from the schema — do NOT guess column names
    - Use ONLY columns that exist on each table (check schema_info)
    - Follow the join chains below — NEVER skip intermediate tables
    - Use proper PostgreSQL syntax (ROUND requires ::numeric cast)
    - NEVER use raw IDs as chart labels — always JOIN to master tables
      and use name columns (product_name, customer_name, vendor_name)
    - All monetary values are in Indian Rupees (INR)
    - Date labels: ALWAYS use TO_CHAR(date_col, 'YYYY-MM') AS month
      NEVER use raw date/timestamp columns as chart labels
    - Top-N charts: ORDER BY metric DESC LIMIT N
    - MAX 30 rows per chart query

    TABLE-COLUMN OWNERSHIP (⚠ columns exist ONLY on these tables):
    ─────────────────────────────────────────────────────────────
    sales_order (so): so_id, customer_id, order_date, total_amount, status
    sales_order_line (sol): sol_id, so_id, product_id, variant_sku, quantity
    sales_order_line_pricing (solp): sol_id, selling_price_per_unit,
        base_price_per_unit, line_total, gold_amount_per_unit,
        diamond_amount_per_unit, making_charges_per_unit
        ⚠ solp does NOT have: so_id, status, product_id, order_date
    sales_order_line_gold (solg): sol_id, gold_kt, gold_wt, gold_amount_per_unit
    sales_order_line_diamond (sold): sol_id, carats, rate, quality
    product_master (pm): product_id, product_name, category, subcategory
    product_variant (pv): variant_sku, product_id, selling_price
    customer_master (cm): customer_id, customer_name
    vendor_master (vm): vendor_id, vendor_name
    purchase_order (po): po_id, vendor_id, po_date, total_amount, status
    po_line_items (pli): pol_id, po_id, sol_id

    CORRECT JOIN CHAINS (always follow these paths):
    ─────────────────────────────────────────────────────────────
    so → sol: so.so_id = sol.so_id
    sol → solp: sol.sol_id = solp.sol_id
    sol → solg: sol.sol_id = solg.sol_id
    sol → sold: sol.sol_id = sold.sol_id
    sol → pm: sol.product_id = pm.product_id
    so → cm: so.customer_id = cm.customer_id
    po → vm: po.vendor_id = vm.vendor_id
    pli → po: pli.po_id = po.po_id
    pli → sol: pli.sol_id = sol.sol_id

    ⚠ To access pricing data (line_total, selling_price_per_unit):
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id

    ⚠ To access gold data (gold_kt, gold_wt):
      JOIN sales_order_line_gold solg ON sol.sol_id = solg.sol_id

    ⚠ To access diamond data (carats, rate, quality):
      JOIN sales_order_line_diamond sold ON sol.sol_id = sold.sol_id

    ⚠ To get product names for chart labels:
      JOIN product_master pm ON sol.product_id = pm.product_id
      SELECT pm.product_name (NEVER use sol.product_id as label)

    ⚠ To get customer names:
      JOIN customer_master cm ON so.customer_id = cm.customer_id
      SELECT cm.customer_name

    SQL QUERY PATTERNS (adapt these to your subject):
    ─────────────────────────────────────────────────────────────
    KPI (returns exactly 1 value):
      SELECT SUM(solp.line_total) AS value
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      WHERE so.status = 'closed'

    CHART — Top-N ranking (2+ columns: label + value):
      SELECT pm.product_name, SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.product_name
      ORDER BY revenue DESC LIMIT 10

    CHART — Monthly trend (2+ columns: month + value):
      SELECT TO_CHAR(so.order_date, 'YYYY-MM') AS month,
             SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      WHERE so.status = 'closed'
      GROUP BY month ORDER BY month

    CHART — Category distribution (2+ columns: category + value):
      SELECT pm.category, SUM(solp.line_total) AS revenue
      FROM sales_order so
      JOIN sales_order_line sol ON so.so_id = sol.so_id
      JOIN sales_order_line_pricing solp ON sol.sol_id = solp.sol_id
      JOIN product_master pm ON sol.product_id = pm.product_id
      WHERE so.status = 'closed'
      GROUP BY pm.category ORDER BY revenue DESC

    ⚠ EVERY chart query MUST return 2+ columns (label + value).
    ⚠ EVERY KPI query MUST return exactly 1 row with 1 value.
    ⚠ ALWAYS use the full join chain — never skip intermediate tables.
    ⚠ ALWAYS use TO_CHAR for dates — never raw timestamps.

    ══════════════════════════════════════════════════════════════
    STEP 2 — KPI DESIGN: EXACTLY 6 KPIs (MANDATORY)
    ══════════════════════════════════════════════════════════════
    Design 6 KPIs that give a complete picture of the subject:
    - Each KPI must be SPECIFIC to the subject (not generic)
    - Include a mix: COUNT, VALUE/AMOUNT, RATE/PERCENTAGE metrics
    - Each KPI SQL returns exactly ONE row with ONE value
    - Assign different colors: blue, green, purple, orange, red, teal
    - Include rich explanations (what, how, why, insight)

    ══════════════════════════════════════════════════════════════
    STEP 3 — CHART DESIGN: EXACTLY 6 CHARTS (MANDATORY)
    ══════════════════════════════════════════════════════════════
    Design 6 charts that answer 6 DIFFERENT business questions about
    the subject. Each chart must add NEW information.

    CHART TYPE SELECTION (based on data shape):
    - Trend over time → line
    - Growth / cumulative → area
    - Category comparison (≤8 items) → bar
    - Ranking / long labels (>8 items) → horizontalBar
    - Part-of-whole / share (≤8 slices) → pie or doughnut
    - Multi-series composition over time → stackedBar

    RULES:
    - Use AT LEAST 5 DIFFERENT chart types across 6 charts
    - BANNED types: polarArea, radar, scatter
    - Assign different color_scheme to each: blues, greens, purples,
      oranges, mixed, gradient
    - No two charts may show the same metric on the same dimension
    - Chart 1 should be a trend (line chart)

    ⚠⚠⚠ CRITICAL CHART SQL RULE ⚠⚠⚠
    EVERY chart SQL MUST use SELECT with 2+ columns:
      SELECT label_column, aggregate_value FROM ... GROUP BY label_column
    A chart query returning only 1 column (e.g., SELECT SUM(x) AS value)
    is INVALID and will be REJECTED. Charts need labels AND values.
    KPIs return 1 value. Charts return ROWS with label + value columns.

    ══════════════════════════════════════════════════════════════
    STEP 4 — INSIGHTS: 6-8 MANDATORY
    ══════════════════════════════════════════════════════════════
    Write 6-8 data-driven insights about the subject:
    - Each insight must have a title and 2-3 sentence body
    - Reference specific numbers, percentages, comparisons
    - Include at least one of each type: positive, negative/warning,
      opportunity
    - Think like a McKinsey analyst presenting to C-suite
    - Focus on: concentration risk, trends, 80/20 patterns,
      operational gaps, strategic opportunities
    - EVERY insight must be about the subject — no generic platitudes

    ══════════════════════════════════════════════════════════════
    OUTPUT FORMAT — STRICT JSON (no markdown, no code fences)
    ══════════════════════════════════════════════════════════════
    {
        "title": "Report title naming the subject",
        "summary": "5-8 sentence executive summary EXCLUSIVELY about the subject.
                     Include headline metrics, trends, patterns, anomalies,
                     strategic implications, and actionable recommendations.",

        "kpis": [
            {
                "id": "kpi_1",
                "label": "Subject-specific KPI name",
                "sql": "SELECT ... single-value query scoped to subject",
                "format": "currency|number|percent",
                "icon": "revenue|orders|customers|products|growth|average|chart",
                "color": "blue|green|purple|orange|red|teal",
                "explanation": {
                    "what": "What this metric measures",
                    "how": "How it is calculated",
                    "why": "Why it matters strategically",
                    "insight": "What this value signals"
                }
            }
        ],

        "charts": [
            {
                "id": "chart_1",
                "title": "Subject-specific chart title",
                "type": "bar|line|pie|doughnut|horizontalBar|stackedBar|area",
                "sql": "SELECT label_col, value_col FROM ... scoped to subject",
                "x_label": "X axis label",
                "y_label": "Y axis label",
                "color_scheme": "blues|greens|purples|oranges|mixed|gradient",
                "explanation": {
                    "what": "What this chart reveals",
                    "how": "How the data is aggregated",
                    "why": "Why this visualization was chosen",
                    "insight": "Key pattern to notice"
                }
            }
        ],

        "table": {
            "title": "Detail table title scoped to subject",
            "sql": "SELECT ... 5-8 columns, LIMIT 20, scoped to subject",
            "explanation": {
                "what": "What data this table presents",
                "how": "How rows are selected and ordered",
                "why": "Why this detail view supports decisions",
                "insight": "What patterns to look for"
            }
        },

        "insights": [
            {
                "title": "Short insight heading about the subject",
                "body": "2-3 sentence insight with specific data references",
                "type": "positive|negative|neutral|warning|opportunity"
            }
        ],

        "meta": {
            "thought_process": [
                "Step 0: SUBJECT = ..., Tables: ..., Filters: ...",
                "Step 1: ...",
                "Step 2: ...",
                "Step 3: ..."
            ]
        }
    }

    ══════════════════════════════════════════════════════════════
    FINAL CHECKLIST (verify before outputting)
    ══════════════════════════════════════════════════════════════
    ✓ Subject is identified and named in the title
    ✓ EVERY KPI label references the subject (not generic)
    ✓ EVERY chart title references the subject (not generic)
    ✓ EVERY SQL query scopes data to the subject via JOINs/WHEREs
    ✓ EVERY insight is specifically about the subject
    ✓ kpis array has exactly 6 items
    ✓ charts array has exactly 6 items
    ✓ At least 5 different chart types used
    ✓ No polarArea, radar, or scatter charts
    ✓ insights array has 6-8 items
    ✓ All SQL is valid PostgreSQL using ONLY columns from schema_info
    ✓ No SQL references columns on wrong tables
    ✓ No raw IDs used as chart labels (use name columns)
    ✓ EVERY KPI SQL: SELECT single_value → returns 1 row, 1 column
    ✓ EVERY chart SQL: SELECT label, value → returns rows with 2+ columns
    ✓ EVERY chart SQL uses GROUP BY (not just a single aggregate)
    ✓ All date columns use TO_CHAR(col, 'YYYY-MM') — never raw timestamps
    ✓ All JOINs follow the correct chain (so→sol→solp, sol→pm, etc.)

    Output ONLY the raw JSON object. No markdown, no explanation."""

    question = dspy.InputField(desc="The user's report request with date context and any active filters")
    schema_info = dspy.InputField(desc="Full database schema with tables, columns, types")
    relationships = dspy.InputField(desc="Known relationships between tables")
    data_profile = dspy.InputField(desc="Data profile: distinct values, numeric ranges, date ranges")

    report_json = dspy.OutputField(
        desc="Complete report JSON object with kpis, charts, table, insights, and meta. "
             "Must be valid JSON. No markdown fences. No text outside the JSON."
    )


class ReportModification(dspy.Signature):
    """You are a report editor. Given an existing report JSON and a user
    modification command, produce the COMPLETE updated report JSON with
    ONLY the requested changes applied.

    RULES:
    - PRESERVE everything NOT mentioned in the modification command
    - Copy ALL unchanged KPIs, charts, table, insights EXACTLY as-is
    - Maintain the EXACT same JSON structure
    - When changing chart type: keep the SAME sql, title, explanation —
      ONLY change the "type" field
    - When adding new charts/KPIs, write valid PostgreSQL SQL using
      proper joins based on the schema
    - Do NOT use polarArea, radar, or scatter chart types
    - Each KPI SQL must return exactly ONE row with ONE value column
    - Each chart SQL must return at least 2 columns (label + value)
    - Output ONLY the complete JSON — no markdown, no code fences

    The user may reference items by:
    - CHART TITLE: find chart whose title best matches
    - CHART TYPE: find chart with that type
    - KPI LABEL: find KPI whose label best matches
    - POSITION: "first chart", "last KPI"
    - GENERAL: "add a new KPI", "add another chart"

    SQL RULES:
    - Use ONLY columns that exist on each table (check schema_info)
    - Follow proper join paths between tables
    - Never reference columns on wrong tables"""

    current_report = dspy.InputField(desc="The current report JSON (without data/values — only structure and SQL)")
    modification = dspy.InputField(desc="User's modification command")
    schema_info = dspy.InputField(desc="Database schema for writing valid SQL")

    updated_report_json = dspy.OutputField(
        desc="Complete updated report JSON with modifications applied. Must be valid JSON. "
             "No markdown fences. No text before or after the JSON object."
    )
