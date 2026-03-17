"""DSPy Signature definitions — optimized for speed.

Consolidated from 8 signatures down to 4 to minimize LLM round-trips:
1. AnalyzeAndPlan  (combines question understanding + schema analysis + query planning)
2. SQLGeneration
3. SQLSelfCritique + Repair (combined)
4. InterpretAndInsight (combines result interpretation + insight generation)
"""

import dspy


# ── 1. Analyze & Plan ──────────────────────────────────────────────────────────

class AnalyzeAndPlan(dspy.Signature):
    """You are an expert SQL analyst with strong business intelligence skills.
    Given a user question, a database schema, and a DATA PROFILE showing actual
    values in the database, analyze the question and produce a detailed query plan.

    ══════════════════════════════════════════════════════════════
    RULE 0 — SIMPLICITY FIRST (HIGHEST PRIORITY)
    ══════════════════════════════════════════════════════════════
    Always use the SIMPLEST possible query that correctly answers the question.
    - If a pre-computed total/summary column already exists in the schema
      (e.g. total_amount, grand_total, total_price), USE IT DIRECTLY.
      NEVER reconstruct it by summing component columns — that is always WRONG
      because it misses labour, taxes, making charges, and other components.
    - For single-record lookups (e.g. "total amount of PO12345"), just filter
      and SELECT that column. No extra joins, no SUM.
    - Only JOIN tables when the required column does not exist in the primary table.
    - Only aggregate (SUM, COUNT, AVG) when the question genuinely asks for an
      aggregate across multiple rows.

    ══════════════════════════════════════════════════════════════
    RULE 1 — WHICH COLUMN TO USE (CRITICAL — READ CAREFULLY)
    ══════════════════════════════════════════════════════════════

    ORDER-LEVEL QUESTIONS (revenue, AOV, total sales, order value, total amount):
      → Use: sales_table_v2_sales_order.total_amount
      → This is the PRE-COMPUTED grand total per order (includes all items,
        gold, diamonds, making charges, labour, taxes).
      → Examples: "total revenue", "AOV", "average order value", "total sales",
        "how much did customer X spend", "total amount of order SO123".
      → Formula:
          Revenue   = SUM(total_amount) FROM sales_order WHERE status = 'closed'
          AOV       = AVG(total_amount) FROM sales_order WHERE status = 'closed'
             OR      = SUM(total_amount) / COUNT(DISTINCT so_id) WHERE status = 'closed'
      → NEVER use line_total from sales_order_line_pricing for these — it is a
        per-line amount and will give wrong results.

    LINE-ITEM / PRODUCT-LEVEL QUESTIONS (per-product revenue, top products by sales):
      → Use: sales_table_v2_sales_order_line_pricing.line_total
      → Use ONLY when the question is about individual product/SKU performance.
      → Examples: "revenue per product", "top selling products by revenue",
        "which product generates most sales".
      → JOIN path: sales_order → sales_order_line → sales_order_line_pricing
      → Still filter by sales_order.status = 'closed'.

    PURCHASE ORDER TOTALS:
      → Use: purchase_orders_v6_purchase_order.total_amount
      → For: "total amount of PO123", "PO value", "purchase order cost".
      → NEVER sum gold_amount + diamond_amount from PO line tables — that misses labour.

    ══════════════════════════════════════════════════════════════
    RULE 2 — STATUS FILTERING
    ══════════════════════════════════════════════════════════════
    For ALL revenue, sales, AOV, and financial metrics:
      → WHERE status = 'closed' on sales_table_v2_sales_order
    For product catalog or inventory questions: no status filter needed.

    ══════════════════════════════════════════════════════════════
    RULE 3 — DATE FILTERING
    ══════════════════════════════════════════════════════════════
    The order_date column is stored as TEXT in 'YYYY-MM-DD' format.
    Use text comparisons for date filters:
      → "last year" (2024): order_date >= '2024-01-01' AND order_date <= '2024-12-31'
      → "this year" (2025): order_date >= '2025-01-01' AND order_date <= '2025-12-31'
      → "last month": use appropriate YYYY-MM-DD range.

    Steps:
    1. Identify: is this ORDER-LEVEL or LINE-ITEM-LEVEL or PO question?
    2. Pick the correct source column per RULE 1 above.
    3. Identify the MINIMUM tables needed (often just one table).
    4. Apply status and date filters as needed.
    5. Produce the simplest correct query plan."""

    question = dspy.InputField(desc="The user's natural-language question")
    schema_info = dspy.InputField(desc="Full database schema with table names, columns, and types")
    relationships = dspy.InputField(desc="Known relationships between tables")
    data_profile = dspy.InputField(desc="Data profile showing actual values: distinct categorical values, numeric ranges, date ranges")

    intent = dspy.OutputField(desc="What the user wants to know (1 sentence)")
    relevant_tables = dspy.OutputField(desc="Comma-separated list of tables needed (minimum necessary)")
    relevant_columns = dspy.OutputField(desc="Comma-separated list of table.column pairs needed")
    join_conditions = dspy.OutputField(desc="JOIN conditions to use, or 'none'")
    where_conditions = dspy.OutputField(desc="WHERE conditions including status/date filters, or 'none'")
    aggregations = dspy.OutputField(desc="Aggregation functions to apply, or 'none'")
    group_by = dspy.OutputField(desc="GROUP BY columns, or 'none'")
    order_by = dspy.OutputField(desc="ORDER BY clause, or 'none'")
    limit_val = dspy.OutputField(desc="LIMIT value, or 'none'")


# ── 2. SQL Generation ──────────────────────────────────────────────────────────

class SQLGeneration(dspy.Signature):
    """Generate a valid PostgreSQL SELECT query based on the query plan.
    The query must be syntactically correct and only reference existing
    tables and columns from the schema.

    CRITICAL RULES:

    1. USE PRE-COMPUTED TOTALS — NEVER RECONSTRUCT THEM:
       - For order-level metrics (revenue, AOV): use sales_table_v2_sales_order.total_amount
       - For PO totals: use purchase_orders_v6_purchase_order.total_amount
       - NEVER add gold_amount + diamond_amount or any component columns —
         that always gives the WRONG answer (misses labour, taxes, etc.)

    2. CORRECT FORMULAS:
       - Revenue:  SELECT SUM(total_amount) FROM sales_table_v2_sales_order WHERE status = 'closed'
       - AOV:      SELECT AVG(total_amount) FROM sales_table_v2_sales_order WHERE status = 'closed'
       - Per-product revenue: SUM(line_total) FROM sales_order_line_pricing
                              JOIN sales_order_line JOIN sales_order WHERE status = 'closed'

    3. DATE FILTERING (order_date is TEXT 'YYYY-MM-DD'):
       - Use: order_date >= 'YYYY-01-01' AND order_date <= 'YYYY-12-31'
       - Do NOT use EXTRACT() or CAST() on order_date

    4. SIMPLICITY:
       - Single-record lookup = simple WHERE filter, no aggregation
       - Only JOIN when needed, only aggregate when needed

    CRITICAL: Output ONLY the raw SQL. No markdown, no explanation, no comments."""

    question = dspy.InputField(desc="The user's question")
    schema_info = dspy.InputField(desc="Database schema")
    query_plan = dspy.InputField(desc="Detailed logical query plan")

    sql_query = dspy.OutputField(
        desc="The SIMPLEST valid PostgreSQL SELECT query that correctly answers the question. "
             "Use pre-computed total_amount for order/PO totals. "
             "Use AVG(total_amount) or SUM(total_amount)/COUNT(DISTINCT so_id) for AOV — "
             "NEVER SUM or AVG of line_total for AOV. "
             "Output ONLY raw SQL — no markdown, no explanation, no code fences."
    )


# ── 3. SQL Self-Critique & Repair ─────────────────────────────────────────────

class SQLCritiqueAndFix(dspy.Signature):
    """Evaluate a generated SQL query for correctness against the schema.
    Check that all tables exist, all columns exist, JOINs are valid,
    GROUP BY matches aggregations, and filters reference real columns.
    If any issues are found, output the corrected SQL. If valid, repeat the SQL exactly."""

    sql_query = dspy.InputField(desc="The generated SQL query")
    schema_info = dspy.InputField(desc="Database schema")
    question = dspy.InputField(desc="The original question")

    is_valid = dspy.OutputField(desc="yes or no")
    issues = dspy.OutputField(desc="List of issues found, or 'none'")
    corrected_sql = dspy.OutputField(
        desc="Corrected SQL query if issues found, otherwise repeat the original SQL exactly. "
             "Output ONLY raw SQL code with no explanation or text."
    )


# ── 4. Interpret & Insight ────────────────────────────────────────────────────

class InterpretAndInsight(dspy.Signature):
    """Interpret SQL query results for a non-technical user and generate insights.

    All monetary values are in INDIAN RUPEES (INR).
    When talking about amounts, you MUST:
    - Prefer the Indian number system (thousands, lakhs, crores) instead of millions/billions.
    - Example conversions:
        - 1,00,000  = 1 lakh
        - 10,00,000 = 10 lakhs
        - 1,00,00,000 = 1 crore
    - Never say "million" or "billion". Use "lakhs" and "crores" instead when numbers are large.
    - If exact conversion is unclear, keep numbers as raw INR amounts with commas (e.g., 12,34,56,789 INR).

    1. Summarize the main findings in plain English (2-3 sentences)
    2. Identify patterns, dominant contributors, outliers, and business implications"""

    question = dspy.InputField(desc="The original question")
    sql_query = dspy.InputField(desc="The SQL query that was executed")
    query_results = dspy.InputField(desc="The query results as JSON")

    answer = dspy.OutputField(
        desc="A clear, non-technical explanation of the results (2-3 sentences)"
    )
    insights = dspy.OutputField(
        desc="3-5 bullet-point analytical insights about the data"
    )


# ── 5. SQL Repair ─────────────────────────────────────────────────────────────

class SQLRepair(dspy.Signature):
    """Given a SQL query that produced a database error, generate a
    corrected query that avoids the error."""

    sql_query = dspy.InputField(desc="The SQL query that failed")
    error_message = dspy.InputField(desc="The database error message")
    schema_info = dspy.InputField(desc="Database schema")
    question = dspy.InputField(desc="The original user question")

    corrected_sql = dspy.OutputField(
        desc="A corrected PostgreSQL SELECT query. Output ONLY the raw SQL code. "
             "Do NOT include any explanation, comments, or text before or after the SQL."
    )
