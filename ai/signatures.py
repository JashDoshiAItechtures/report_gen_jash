"""DSPy Signature definitions — optimized for speed.

Consolidated from 8 signatures down to 4 to minimize LLM round-trips:
1. AnalyzeAndPlan  (combines question understanding + schema analysis + query planning)
2. SQLGeneration
3. SQLSelfCritique + Repair (combined)
4. InterpretAndInsight (combines result interpretation + insight generation)
"""

import dspy


# ── 1. Analyze & Plan (combines 3 former stages) ───────────────────────────

class AnalyzeAndPlan(dspy.Signature):
    """You are an expert SQL analyst with strong business intelligence skills.
    Given a user question, a database schema, and a DATA PROFILE showing actual
    values in the database, analyze the question and produce a detailed query plan.

    CRITICAL BUSINESS RULES — you MUST follow these:
    1. When calculating revenue, sales, or monetary metrics, ONLY include
       records with a completed/closed/successful status. Filter out cancelled,
       pending, open, returned, or failed records.
    2. Look at the data profile to see which status/categorical values exist
       and decide which ones represent VALID/COMPLETED transactions.
    3. For AOV (Average Order Value), divide total revenue of CLOSED orders
       by the COUNT of CLOSED orders only.
    4. When a column like 'status' exists, ALWAYS consider whether filtering
       by status is needed for accurate business metrics.
    5. For inventory/stock metrics, consider item states appropriately.
    6. When computing counts, totals, or averages, think about which records
       should logically be included vs excluded.

    Steps:
    1. Understand the user's question (intent, metrics, entities, filters)
    2. Review the DATA PROFILE to understand actual values in the database
    3. Identify which tables and columns are relevant
    4. Determine appropriate filters (especially status-based) for accurate results
    5. Produce a complete logical query plan"""

    question = dspy.InputField(desc="The user's natural-language question")
    schema_info = dspy.InputField(desc="Full database schema with table names, columns, and types")
    relationships = dspy.InputField(desc="Known relationships between tables")
    data_profile = dspy.InputField(desc="Data profile showing actual values: distinct categorical values, numeric ranges, date ranges")

    intent = dspy.OutputField(desc="What the user wants to know (1 sentence)")
    relevant_tables = dspy.OutputField(desc="Comma-separated list of tables needed")
    relevant_columns = dspy.OutputField(desc="Comma-separated list of table.column pairs needed")
    join_conditions = dspy.OutputField(desc="JOIN conditions to use, or 'none'")
    where_conditions = dspy.OutputField(desc="WHERE conditions including status/state filters for accurate business metrics, or 'none'")
    aggregations = dspy.OutputField(desc="Aggregation functions to apply, or 'none'")
    group_by = dspy.OutputField(desc="GROUP BY columns, or 'none'")
    order_by = dspy.OutputField(desc="ORDER BY clause, or 'none'")
    limit_val = dspy.OutputField(desc="LIMIT value, or 'none'")


# ── 2. SQL Generation ──────────────────────────────────────────────────────

class SQLGeneration(dspy.Signature):
    """Generate a valid PostgreSQL SELECT query based on the query plan.
    The query must be syntactically correct and only reference existing
    tables and columns from the schema.

    SIMPLICITY RULES (MUST FOLLOW):
    - If a pre-computed total/summary column exists (e.g. total_amount, grand_total,
      total_price, net_amount), SELECT THAT COLUMN DIRECTLY. NEVER reconstruct it
      by adding component columns (e.g. gold_amount + diamond_amount) — that will give
      wrong answers because it ignores labour, taxes, and other components.
    - For single-record lookups (e.g. "total amount of PO12345"), write:
        SELECT total_amount FROM <table> WHERE po_id = 'PO12345'
      NOT a multi-table join with SUM of parts.
    - Only JOIN tables if the required column does not exist in the primary table.
    - Only use aggregation (SUM, COUNT, AVG, etc.) when the question genuinely asks
      for an aggregate across multiple rows.

    BUSINESS RULES:
    - Include status/state filters from the query plan for accurate metrics.
    - Ensure the query respects business logic (e.g., only closed orders for revenue).

    CRITICAL: Output ONLY the raw SQL. No markdown, no explanation, no comments."""

    question = dspy.InputField(desc="The user's question")
    schema_info = dspy.InputField(desc="Database schema")
    query_plan = dspy.InputField(desc="Detailed logical query plan")

    sql_query = dspy.OutputField(
        desc="The SIMPLEST valid PostgreSQL SELECT query that correctly answers the question. "
             "Use pre-computed total columns when available. Avoid unnecessary joins and aggregations. "
             "Output ONLY the raw SQL code — no markdown, no explanation, no code fences."
    )


# ── 3. SQL Self-Critique & Repair (combined) ───────────────────────────────

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


# ── 4. Interpret & Insight (combined) ──────────────────────────────────────

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


# ── 5. SQL Repair (for execution errors) ──────────────────────────────────

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
