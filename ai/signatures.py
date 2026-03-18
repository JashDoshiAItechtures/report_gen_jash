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

    COMPONENT COST BY PRODUCT (diamond cost, gold cost, making charges per product):
      → sales_table_v2_sales_order_line_pricing has ALL cost columns AND quantity.
        It is SELF-SUFFICIENT. NO JOIN to any other table is needed for cost queries.
      → Correct formula:  SUM(column * quantity)  — multiply every time, never skip.
      → Columns (all in sales_order_line_pricing):
          diamond_amount_per_unit  → SUM(diamond_amount_per_unit * quantity)
          gold_amount_per_unit     → SUM(gold_amount_per_unit * quantity)
          making_charges_per_unit  → SUM(making_charges_per_unit * quantity)
      → GROUP BY product_id for "by product", GROUP BY variant_sku for "by variant/SKU".
      → Always prefix the column with the table alias to avoid ambiguity.

      EXACT TEMPLATE — top 10 products by diamond cost:
            SELECT lp.product_id,
                   SUM(lp.diamond_amount_per_unit * lp.quantity) AS diamond_cost
            FROM sales_table_v2_sales_order_line_pricing lp
            GROUP BY lp.product_id
            ORDER BY diamond_cost DESC
            LIMIT 10

      CRITICAL — DO NOT JOIN sales_order_line_diamond or sales_order_line_gold for costs:
        • Those detail tables have MULTIPLE rows per sol_id (one per diamond type/shape/quality).
        • Joining them multiplies every pricing row by the number of detail rows → WRONG totals.
        • They have no quantity column → SUM(diamond_amount_per_unit) there is also WRONG.
        • Only use those detail tables when the question explicitly asks about diamond/gold
          PROPERTIES such as shape, quality, karat, carat weight, size — NOT for cost/revenue.

    PURCHASE ORDER TOTALS:
      → Use: purchase_orders_v6_purchase_order.total_amount
      → For: "total amount of PO123", "PO value", "purchase order cost".
      → NEVER sum gold_amount + diamond_amount from PO line tables — that misses labour.

    ══════════════════════════════════════════════════════════════
    RULE 1A — FAN-OUT: purchase_order + po_sales_order_link
    ══════════════════════════════════════════════════════════════
    po_sales_order_link has MULTIPLE rows per po_id (one per linked SO).
    Joining purchase_order → po_sales_order_link and doing SUM(total_amount)
    counts each PO's amount once per linked SO — completely wrong.

    WRONG (never write this):
      SELECT po.vendor_id, SUM(po.total_amount)
      FROM purchase_orders_v6_purchase_order po
      JOIN purchase_orders_v6_po_sales_order_link pl ON po.po_id = pl.po_id
      JOIN sales_table_v2_sales_order so ON pl.so_id = so.so_id
      WHERE so.status = 'closed'
      GROUP BY po.vendor_id

    CORRECT (DISTINCT subquery first, then SUM):
      SELECT vendor_id, SUM(total_amount) AS total_value
      FROM (
          SELECT DISTINCT po.po_id, po.vendor_id, po.total_amount
          FROM purchase_orders_v6_purchase_order po
          JOIN purchase_orders_v6_po_sales_order_link pl ON po.po_id = pl.po_id
          JOIN sales_table_v2_sales_order so ON pl.so_id = so.so_id
          WHERE so.status = 'closed'
      ) deduped
      GROUP BY vendor_id
      ORDER BY total_value DESC

    ══════════════════════════════════════════════════════════════
    RULE 1B — LAG/LEAD must ORDER BY YEAR then MONTH
    ══════════════════════════════════════════════════════════════
    Data spans multiple years (2024–2026). ORDER BY month alone inside a window
    function compares months across different years — wrong growth rates.

    WRONG: LAG(revenue) OVER (ORDER BY EXTRACT(MONTH FROM order_date::date))
    CORRECT: LAG(revenue) OVER (ORDER BY yr ASC, mo ASC)

    Also: do NOT add WHERE status = 'closed' unless the question asks for it.
    For growth/trend questions, use all orders (no status filter).

    CORRECT MoM template:
      WITH monthly AS (
          SELECT EXTRACT(YEAR  FROM order_date::date) AS yr,
                 EXTRACT(MONTH FROM order_date::date) AS mo,
                 SUM(total_amount) AS revenue
          FROM sales_table_v2_sales_order
          GROUP BY yr, mo
      )
      SELECT yr, mo, revenue,
             LAG(revenue) OVER (ORDER BY yr ASC, mo ASC) AS prev_revenue
      FROM monthly ORDER BY yr, mo

    ══════════════════════════════════════════════════════════════
    RULE 1C — IGI/NC certification is in variant_sku, NOT quality
    ══════════════════════════════════════════════════════════════
    The quality column in diamond tables holds grades like 'GH VVS' — never 'IGI'/'NC'.
    Filtering quality = 'IGI' always returns zero rows.

    WRONG: WHERE T3.quality IN ('IGI', 'Non-IGI')
    CORRECT: WHERE variant_sku LIKE '%-IGI'   (or '%-NC' for non-certified)

    For "customers with both IGI and NC in same order":
      WHERE so.so_id IN (
          SELECT so_id FROM sales_table_v2_sales_order_line WHERE variant_sku LIKE '%-IGI'
          INTERSECT
          SELECT so_id FROM sales_table_v2_sales_order_line WHERE variant_sku LIKE '%-NC'
      )

    ══════════════════════════════════════════════════════════════
    RULE 1D — NO product_master TABLE EXISTS
    ══════════════════════════════════════════════════════════════
    There is no product_master, products, or product_catalog table.
    Use product_id as the only product identifier. Never invent table names.

    ══════════════════════════════════════════════════════════════
    RULE 1C0 — "TOP/BEST PER GROUP" REQUIRES ROW_NUMBER PARTITION BY
    ══════════════════════════════════════════════════════════════
    Questions like "top customer per city", "best product per category",
    "highest revenue vendor per region" are PER-GROUP ranking problems.
    A global ORDER BY + LIMIT returns the global top — NOT one per group.

    WRONG (global sort — returns all rows or wrong subset):
      SELECT city, customer_id, SUM(total_amount) AS rev
      FROM ... GROUP BY city, customer_id
      ORDER BY rev DESC          ← sorts globally, does NOT pick one per city

    CORRECT (ROW_NUMBER partitioned by the group column, filter rank = 1):
      SELECT city, customer_id, customer_name, total_revenue
      FROM (
          SELECT cm.city, cm.customer_id, cm.customer_name,
                 SUM(so.total_amount) AS total_revenue,
                 ROW_NUMBER() OVER (PARTITION BY cm.city
                                    ORDER BY SUM(so.total_amount) DESC) AS rnk
          FROM sales_table_v2_sales_order so
          JOIN sales_table_v2_customer_master cm ON so.customer_id = cm.customer_id
          WHERE so.status = 'closed'
          GROUP BY cm.city, cm.customer_id, cm.customer_name
      ) t
      WHERE rnk = 1
      ORDER BY total_revenue DESC

    Trigger words: "per city", "per region", "per category", "for each X … top/best/highest".

    ══════════════════════════════════════════════════════════════
    RULE 1C1 — "TOP N FOR BOTH X AND Y" REQUIRES TWO INDEPENDENT RANKs
    ══════════════════════════════════════════════════════════════
    "Top 5 by revenue AND top 5 by diamond cost" means a product must be in
    the top 5 on EACH metric independently.
    ORDER BY revenue DESC, cost DESC LIMIT 5 is NOT two rankings — it ranks
    by revenue and uses cost only as a tiebreaker, returning the wrong result.

    WRONG:
      ORDER BY revenue DESC, diamond_cost DESC LIMIT 5   ← not two rankings

    CORRECT (two independent RANK() window functions, filter where both <= N):
      SELECT product_id, revenue, diamond_cost, rev_rank, diamond_rank
      FROM (
          SELECT lp.product_id,
                 SUM(lp.line_total) AS revenue,
                 SUM(lp.diamond_amount_per_unit * lp.quantity) AS diamond_cost,
                 RANK() OVER (ORDER BY SUM(lp.line_total) DESC) AS rev_rank,
                 RANK() OVER (ORDER BY SUM(lp.diamond_amount_per_unit * lp.quantity) DESC)
                              AS diamond_rank
          FROM sales_table_v2_sales_order_line_pricing lp
          JOIN sales_table_v2_sales_order_line sol ON lp.sol_id = sol.sol_id
          JOIN sales_table_v2_sales_order so ON sol.so_id = so.so_id
          WHERE so.status = 'closed'
          GROUP BY lp.product_id
      ) t
      WHERE rev_rank <= 5 AND diamond_rank <= 5

    ══════════════════════════════════════════════════════════════
    RULE 1C2 — CUMULATIVE/RUNNING WINDOW NEEDS PRE-AGGREGATION
    ══════════════════════════════════════════════════════════════
    Applying SUM(...) OVER (ORDER BY date) directly on raw order rows produces
    one cumulative row per ORDER (not per date). Multiple orders on the same
    date get separate cumulative values — wrong.
    Always GROUP BY date first in a subquery, then apply the window on top.

    WRONG (window over raw rows — one row per order, same date repeats):
      SELECT order_date, SUM(total_amount) OVER (ORDER BY order_date) AS cum_rev
      FROM sales_table_v2_sales_order WHERE status = 'closed'

    CORRECT (aggregate by date first, then window):
      SELECT order_date, daily_revenue,
             SUM(daily_revenue) OVER (ORDER BY order_date) AS cumulative_revenue
      FROM (
          SELECT order_date::date AS order_date, SUM(total_amount) AS daily_revenue
          FROM sales_table_v2_sales_order
          WHERE status = 'closed'
          GROUP BY order_date::date
      ) t
      ORDER BY order_date

    ══════════════════════════════════════════════════════════════
    RULE 1D0 — PERCENTAGE / RATIO WITH CASE WHEN — NEVER PRE-FILTER STATUS
    ══════════════════════════════════════════════════════════════
    When computing a percentage breakdown across different statuses
    (e.g. "% closed vs % cancelled"), the denominator must be ALL orders.
    Adding WHERE status IN ('closed', 'cancelled') before grouping removes
    other statuses from the denominator → inflated percentages.

    WRONG (WHERE filter shrinks denominator):
      SELECT customer_id,
          SUM(CASE WHEN status = 'closed' THEN total_amount ELSE 0 END) * 100.0
          / SUM(total_amount) AS pct_closed
      FROM sales_table_v2_sales_order
      WHERE status IN ('closed', 'cancelled')   ← removes open/processing rows
      GROUP BY customer_id

    CORRECT (no WHERE on status — CASE WHEN handles the split):
      SELECT cm.customer_id, cm.customer_name,
          ROUND((SUM(CASE WHEN so.status = 'closed' THEN so.total_amount ELSE 0 END)
                 * 100.0 / SUM(so.total_amount))::numeric, 2) AS pct_closed,
          ROUND((SUM(CASE WHEN so.status = 'cancelled' THEN so.total_amount ELSE 0 END)
                 * 100.0 / SUM(so.total_amount))::numeric, 2) AS pct_cancelled
      FROM sales_table_v2_sales_order so
      JOIN sales_table_v2_customer_master cm ON so.customer_id = cm.customer_id
      GROUP BY cm.customer_id, cm.customer_name

    ══════════════════════════════════════════════════════════════
    RULE 1D1 — PostgreSQL ROUND() REQUIRES ::numeric CAST
    ══════════════════════════════════════════════════════════════
    PostgreSQL's ROUND(value, N) only accepts numeric as the first argument.
    Division or SUM() results are often double precision — passing them to
    ROUND() directly raises: "function round(double precision, integer) does not exist".

    WRONG:   ROUND(SUM(x) * 100.0 / SUM(y), 2)
    CORRECT: ROUND((SUM(x) * 100.0 / SUM(y))::numeric, 2)

    Always cast the expression to ::numeric inside every ROUND(..., N) call.

    ══════════════════════════════════════════════════════════════
    RULE 1D2 — "PER X" DENOMINATOR — READ THE QUESTION CAREFULLY
    ══════════════════════════════════════════════════════════════
    The word after "per" tells you exactly what the denominator must be.
    Using the wrong denominator gives a completely different metric.

    "per order"    → COUNT(DISTINCT so_id)     ← number of sales orders
    "per unit"     → SUM(quantity)             ← number of pieces/items sold
    "per customer" → COUNT(DISTINCT customer_id)
    "per product"  → COUNT(DISTINCT product_id)
    "per vendor"   → COUNT(DISTINCT vendor_id)
    "per SKU"      → COUNT(DISTINCT variant_sku)

    WRONG — "per order" using quantity as denominator:
      SUM(lp.line_total) / SUM(sol.quantity)       ← this is revenue per UNIT, not per ORDER

    CORRECT — "per order" using distinct order count:
      SUM(lp.line_total) / COUNT(DISTINCT so.so_id) ← this is revenue per ORDER

    Similarly for AOV (average order value):
      AVG(total_amount)  or  SUM(total_amount) / COUNT(DISTINCT so_id)
      NEVER  SUM(total_amount) / SUM(quantity)

    ══════════════════════════════════════════════════════════════
    RULE 1E — WHICH TABLE OWNS WHICH COLUMNS (DO NOT MIX)
    ══════════════════════════════════════════════════════════════
    sales_order_line_pricing  → financial rollup only:
        gold_amount_per_unit, diamond_amount_per_unit, making_charges_per_unit,
        base_price_per_unit, selling_price_per_unit, line_total, final_amount,
        quantity, sol_id, variant_sku, product_id
        ✗ Does NOT have: gold_kt, gold_colour, gold_rate_per_gm, metal_weight,
                          diamond_id, shape, quality, pointer, carats

    sales_order_line_gold     → physical gold attributes:
        gold_kt, gold_colour, gold_rate_per_gm, metal_weight_per_unit,
        finding_per_unit, gross_weight_per_unit, gold_amount_per_unit, sol_id
        → JOIN to pricing on sol_id when you need both gold attributes AND costs.

    sales_order_line_diamond  → physical diamond attributes:
        diamond_id, shape, quality, size_mm, pointer, pieces_per_unit,
        carats_per_unit, rate_per_carat, diamond_amount_per_unit, sol_id
        → JOIN to pricing on sol_id ONLY when the question asks about diamond
          properties (shape, quality, karat, carat) — NOT for cost aggregation.

    RULE: If the question asks "by karat" / "by gold_kt" / "by colour" etc.,
    you MUST join sales_order_line_gold. You cannot get gold_kt from pricing.

    Example — total costs by karat type:
      SELECT g.gold_kt,
             SUM(lp.gold_amount_per_unit    * lp.quantity) AS total_gold_amount,
             SUM(lp.diamond_amount_per_unit * lp.quantity) AS total_diamond_amount,
             SUM(lp.making_charges_per_unit * lp.quantity) AS total_making_charges
      FROM sales_table_v2_sales_order_line_pricing lp
      JOIN sales_table_v2_sales_order_line_gold g  ON lp.sol_id = g.sol_id
      JOIN sales_table_v2_sales_order_line     sol ON lp.sol_id = sol.sol_id
      JOIN sales_table_v2_sales_order          so  ON sol.so_id = so.so_id
      WHERE so.status = 'closed'
      GROUP BY g.gold_kt
      ORDER BY g.gold_kt

    ══════════════════════════════════════════════════════════════
    RULE 1A — FAN-OUT: DEDUPLICATE BEFORE AGGREGATING ON JOIN CHAINS
    ══════════════════════════════════════════════════════════════
    purchase_orders_v6_po_sales_order_link has MULTIPLE rows per po_id.
    Joining purchase_order → po_sales_order_link and then doing SUM(total_amount)
    counts the same PO amount once per linked sales order — WRONG.

    WRONG:
      SELECT po.vendor_id, SUM(po.total_amount)
      FROM purchase_orders_v6_purchase_order po
      JOIN purchase_orders_v6_po_sales_order_link lnk ON po.po_id = lnk.po_id
      GROUP BY po.vendor_id

    CORRECT — wrap purchase_order in a DISTINCT subquery first:
      SELECT vendor_id, SUM(total_amount)
      FROM (
          SELECT DISTINCT po.po_id, po.vendor_id, po.total_amount
          FROM purchase_orders_v6_purchase_order po
          JOIN purchase_orders_v6_po_sales_order_link lnk ON po.po_id = lnk.po_id
      ) deduped
      GROUP BY vendor_id

    Apply the DISTINCT-subquery fix whenever po_sales_order_link is in the JOIN chain
    and you are aggregating any column from purchase_orders_v6_purchase_order.

    ══════════════════════════════════════════════════════════════
    RULE 1B — ROW MULTIPLICATION FROM DETAIL TABLE JOINS
    ══════════════════════════════════════════════════════════════
    sales_table_v2_sales_order_line_diamond and purchase_orders_v6_po_line_diamond
    have MULTIPLE rows per line item (one per diamond type/shape/quality).
    Joining them directly to pricing or header tables inflates every SUM.
    For cost calculations: use sales_order_line_pricing which already has
    rolled-up amounts (diamond_amount_per_unit, gold_amount_per_unit).
    Only use detail tables when the question asks about diamond/gold PROPERTIES
    (shape, quality, karat, carat weight) — never for cost or revenue totals.

    ══════════════════════════════════════════════════════════════
    RULE 1C — IGI/NC CERTIFICATION IS IN variant_sku, NOT quality
    ══════════════════════════════════════════════════════════════
    The quality column in diamond tables contains diamond grades (e.g. 'GH VVS').
    IGI and NC are NOT values in that column.
    Certification is the LAST segment of variant_sku:
      105186-10K-Q12-IGI  → IGI certified  → variant_sku LIKE '%-IGI'
      105186-10K-Q12-NC   → non-certified  → variant_sku LIKE '%-NC'
    Apply this filter on sales_order_line or sales_order_line_pricing.

    ══════════════════════════════════════════════════════════════
    RULE 1D — NO product_master TABLE EXISTS
    ══════════════════════════════════════════════════════════════
    There is no product_master, products, or product_catalog table in this schema.
    Product names do not exist — use product_id as the only product identifier.
    Never reference a table that is not in the provided schema.

    ══════════════════════════════════════════════════════════════
    RULE 2 — STATUS FILTERING (DEFAULT = 'closed', ALWAYS)
    ══════════════════════════════════════════════════════════════
    For ANY query that touches sales_table_v2_sales_order, the DEFAULT
    is to always filter WHERE status = 'closed'.

    ONLY skip or change this filter when the question EXPLICITLY mentions
    a different status by name — e.g. "pending orders", "open orders",
    "cancelled orders", "all orders regardless of status".
    If the question does not mention any status word, use status = 'closed'.

    This applies to every type of sales_order query:
      • Revenue, AOV, total sales, order value
      • Order count ("how many orders", "number of orders")
      • Top customers, top products, top SKUs by any metric
      • Any SUM, AVG, COUNT on total_amount, line_total, or component costs
      • Any JOIN that starts from or passes through sales_order
      • Component cost queries from sales_order_line_pricing
        (join back to sales_order and apply status = 'closed')

    IMPORTANT — line-level tables have NO status column:
      sales_order_line_pricing, sales_order_line_gold, sales_order_line_diamond,
      and sales_order_line do NOT have a status column.
      To apply the status filter when using these tables, you MUST join back to
      sales_table_v2_sales_order and filter on so.status = 'closed':
        JOIN sales_table_v2_sales_order_line     sol ON lp.sol_id = sol.sol_id
        JOIN sales_table_v2_sales_order          so  ON sol.so_id = so.so_id
        WHERE so.status = 'closed'

    NEVER apply status = 'closed' to:
      • Purchase order tables (they have a separate status column)
      • Inventory tables
      • Customer master / product lookup tables (no status column)

    ══════════════════════════════════════════════════════════════
    RULE 1.5 — AGGREGATION GRANULARITY (CRITICAL)
    ══════════════════════════════════════════════════════════════
    The word used in the question determines the GROUP BY level.
    NEVER add extra columns to GROUP BY beyond what the question asks for.

    PRODUCT vs VARIANT vs SKU:
      • "by product" / "per product" / "top products"
          → GROUP BY product_id ONLY
          → product_id is the product-level key (e.g. PROD-0020)
          → A product has MANY variants/SKUs — grouping by variant_sku too
            will give per-variant rows, NOT per-product rows (WRONG).
          → There is no separate product name column in this database.
            Use product_id as the product identifier.
      • "by variant" / "per variant" / "by SKU" / "per SKU"
          → GROUP BY variant_sku  (and optionally product_id)
          → variant_sku is the fine-grained key (e.g. 105186-14K-Q12-IGI)
      • "with product names" when asked alongside "by product"
          → Still GROUP BY product_id — do NOT add variant_sku to GROUP BY.
            product_id IS the product name in this database.

    CUSTOMER:
      • "by customer" / "per customer" / "top customers"
          → GROUP BY sales_table_v2_customer_master.customer_id
          → JOIN customer_master to get customer_name

    VENDOR:
      • "by vendor" / "per vendor" / "top vendors"
          → GROUP BY vendor_id (or vendor_name if available in the table)

    ORDER:
      • "by order" / "per order"
          → GROUP BY so_id (sales) or po_id (purchase)

    GENERAL RULE: Match the GROUP BY exactly to the entity noun in the question.
    Never silently add extra columns (like variant_sku) when the question says "product".
    Never group at a finer granularity than what was asked.

    ══════════════════════════════════════════════════════════════
    RULE 2.5 — SALES ORDER vs PURCHASE ORDER DISAMBIGUATION
    ══════════════════════════════════════════════════════════════
    There are TWO completely separate order systems. NEVER confuse them.

    SALES ORDERS (outgoing — what customers buy from us):
      → Primary table: sales_table_v2_sales_order
      → IDs start with "SO" (e.g. SO13579)
      → Keywords: "sales order", "order", "customer order", "AOV", "revenue",
        "highest order", "best order", "what customers spent", "order value"
      → Example questions → sales_order table:
          "highest order" / "biggest sale" / "top sales order" / "total revenue" / "AOV"

    PURCHASE ORDERS (incoming — what we buy from vendors/suppliers):
      → Primary table: purchase_orders_v6_purchase_order
      → IDs start with "PO" (e.g. PO08796)
      → Keywords: "purchase order", "PO", "vendor order", "supplier order",
        "highest purchase order", "best PO", "what we ordered from vendors"
      → Example questions → purchase_order table:
          "highest purchase order" / "total PO value" / "amount of PO12345"

    DISAMBIGUATION RULE:
      - If question mentions "purchase order", "PO", "vendor" → use purchase_orders_v6 tables.
      - If question mentions "sales order", "order", "revenue", "customer" → use sales_table_v2 tables.
      - If ambiguous and no "purchase" keyword → default to sales_table_v2_sales_order.

    ══════════════════════════════════════════════════════════════
    RULE 3 — DATE FILTERING
    ══════════════════════════════════════════════════════════════
    The question includes a [CONTEXT] block at the top with today's date,
    current year, and exact date ranges for "last year" and "this year".
    ALWAYS read and use those exact date ranges from the [CONTEXT] block.

    The order_date column is stored as TEXT in 'YYYY-MM-DD' format.
    Use text comparisons ONLY — never EXTRACT() or CAST():
      → Use the ranges exactly as given in the [CONTEXT] block.
      → "last year": order_date >= '<last_year>-01-01' AND order_date <= '<last_year>-12-31'
      → "this year": order_date >= '<current_year>-01-01' AND order_date <= '<current_year>-12-31'
      → "last month": use appropriate YYYY-MM-DD range relative to today's date.

    Steps:
    1. READ the [CONTEXT] block to get current year and last year values.
    2. Identify: is this SALES ORDER, PURCHASE ORDER, or LINE-ITEM question? (see Rule 2.5)
    3. Pick the correct table and source column per RULE 1 and RULE 2.5.
    4. Identify the MINIMUM tables needed (often just one table).
    5. Apply status and date filters as needed using the exact dates from [CONTEXT].
    6. Produce the simplest correct query plan."""

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
    group_by = dspy.OutputField(desc="GROUP BY columns matching the exact entity in the question (e.g. product_id for 'by product', variant_sku for 'by variant', customer_id for 'by customer'), or 'none'")
    order_by = dspy.OutputField(desc="ORDER BY clause, or 'none'")
    limit_val = dspy.OutputField(desc="LIMIT value, or 'none'")


# ── 2. SQL Generation ──────────────────────────────────────────────────────────

class SQLGeneration(dspy.Signature):
    """Generate a valid PostgreSQL SELECT query based on the query plan.
    The query must be syntactically correct and only reference existing
    tables and columns from the schema.

    CRITICAL RULES:

    0. READ THE [CONTEXT] BLOCK IN THE QUESTION:
       - It tells you today's date, current year, and exact date ranges for "last year"/"this year".
       - Always use those exact year values. NEVER guess the year.

    1. GROUP BY GRANULARITY — MATCH EXACTLY TO THE QUESTION'S ENTITY:
       - "by product" / "top products"     → GROUP BY product_id  (NOT variant_sku, NOT both)
       - "by variant" / "by SKU"           → GROUP BY variant_sku
       - "by customer" / "top customers"   → GROUP BY customer_id  (JOIN for customer_name)
       - "by vendor"  / "top vendors"      → GROUP BY vendor_id or vendor_name
       - "by order"                        → GROUP BY so_id or po_id
       Adding extra columns to GROUP BY (e.g. variant_sku when question says "product")
       is ALWAYS WRONG — it fragments results into variant-level rows.

    2. SALES ORDER vs PURCHASE ORDER — NEVER CONFUSE THEM:
       - "purchase order", "PO", "vendor" → purchase_orders_v6_purchase_order table
       - "sales order", "order", "revenue", "AOV", "highest order" (without "purchase") → sales_table_v2_sales_order table
       - Highest/biggest/top "purchase order" → purchase_orders_v6_purchase_order ORDER BY total_amount DESC
       - Highest/biggest/top "order" or "sale" → sales_table_v2_sales_order ORDER BY total_amount DESC

    3. COMPONENT COSTS (diamond/gold/making charges) — PRICING TABLE ONLY, NO JOINS:
       - ONE table only: sales_table_v2_sales_order_line_pricing (alias: lp)
       - Formula: SUM(lp.diamond_amount_per_unit * lp.quantity)   for diamond cost
                  SUM(lp.gold_amount_per_unit * lp.quantity)      for gold cost
                  SUM(lp.making_charges_per_unit * lp.quantity)   for making charges
       - Always use table alias prefix (lp.product_id, lp.quantity, etc.) — never bare column names.
       - ZERO joins needed. DO NOT join sales_order_line_diamond or sales_order_line_gold.
         Joining those tables introduces duplicate rows (they have multiple rows per sol_id),
         which inflates every SUM by 2x, 3x, or more — silently wrong results.
       - Exact templates:
           Top products by diamond cost:
             SELECT lp.product_id, SUM(lp.diamond_amount_per_unit * lp.quantity) AS diamond_cost
             FROM sales_table_v2_sales_order_line_pricing lp
             GROUP BY lp.product_id ORDER BY diamond_cost DESC LIMIT 10
           Top SKUs by gold cost:
             SELECT lp.variant_sku, SUM(lp.gold_amount_per_unit * lp.quantity) AS gold_cost
             FROM sales_table_v2_sales_order_line_pricing lp
             GROUP BY lp.variant_sku ORDER BY gold_cost DESC LIMIT 10

    4. FAN-OUT — DISTINCT subquery when joining purchase_order to po_sales_order_link:
       po_sales_order_link has multiple rows per po_id → SUM(total_amount) double-counts.
       WRONG:  SELECT po.vendor_id, SUM(po.total_amount) FROM purchase_order po
               JOIN po_sales_order_link lnk ON po.po_id = lnk.po_id GROUP BY po.vendor_id
       CORRECT: SELECT vendor_id, SUM(total_amount) FROM (
                    SELECT DISTINCT po.po_id, po.vendor_id, po.total_amount
                    FROM purchase_orders_v6_purchase_order po
                    JOIN purchase_orders_v6_po_sales_order_link lnk ON po.po_id = lnk.po_id
                ) deduped GROUP BY vendor_id

    4b. ROW MULTIPLICATION — never join sales_order_line_diamond or po_line_diamond
        directly to pricing/header tables for cost calculations; they have multiple rows
        per line item. Use sales_order_line_pricing rolled-up amounts instead.

    4c. CERTIFICATION (IGI/NC) — comes from variant_sku last segment, NOT quality column:
        IGI → variant_sku LIKE '%-IGI'    NC → variant_sku LIKE '%-NC'
        Apply on sales_order_line or sales_order_line_pricing.

    4d. NO product_master TABLE — never reference it; it does not exist in this schema.
        Use product_id only. Never invent tables not present in schema_info.

    4e. REVENUE SOURCE — never mix:
        Order-level: sales_table_v2_sales_order.total_amount
        Line-level:  sales_table_v2_sales_order_line_pricing.line_total

    4. FAN-OUT — when joining purchase_order to po_sales_order_link, use DISTINCT subquery:
       WRONG:   SELECT po.vendor_id, SUM(po.total_amount) FROM purchase_order po
                JOIN po_sales_order_link pl ON po.po_id = pl.po_id ... GROUP BY po.vendor_id
       CORRECT: SELECT vendor_id, SUM(total_amount) FROM (
                    SELECT DISTINCT po.po_id, po.vendor_id, po.total_amount
                    FROM purchase_orders_v6_purchase_order po
                    JOIN purchase_orders_v6_po_sales_order_link pl ON po.po_id = pl.po_id
                    JOIN sales_table_v2_sales_order so ON pl.so_id = so.so_id
                    WHERE so.status = 'closed'
                ) deduped GROUP BY vendor_id

    4b. LAG/LEAD window functions — always ORDER BY yr ASC, mo ASC (never month only):
        Also: do NOT add status = 'closed' for trend/growth queries unless explicitly asked.
        WRONG:   LAG(x) OVER (ORDER BY EXTRACT(MONTH FROM ...))
        CORRECT: LAG(x) OVER (ORDER BY EXTRACT(YEAR FROM order_date::date) ASC,
                                        EXTRACT(MONTH FROM order_date::date) ASC)

    4c. IGI/NC certification — from variant_sku LIKE '%-IGI' / '%-NC', NOT from quality column:
        WRONG:   WHERE quality IN ('IGI', 'Non-IGI')
        CORRECT: WHERE variant_sku LIKE '%-IGI'
        For both in same order: use INTERSECT on sales_order_line.

    4d. NO product_master table — never reference it; use product_id only.

    4c0. "TOP/BEST PER GROUP" → use ROW_NUMBER() PARTITION BY the group column, filter rnk = 1.
         WRONG: GROUP BY city, customer ORDER BY revenue DESC (global sort, not per-city top)
         CORRECT: ROW_NUMBER() OVER (PARTITION BY city ORDER BY revenue DESC) AS rnk … WHERE rnk = 1

    4c1. "TOP N FOR BOTH X AND Y" → two independent RANK() window functions, filter both <= N.
         WRONG: ORDER BY revenue DESC, cost DESC LIMIT 5  (cost is just tiebreaker, not ranked)
         CORRECT: RANK() OVER (ORDER BY revenue DESC) AS rev_rank,
                  RANK() OVER (ORDER BY cost DESC) AS cost_rank … WHERE rev_rank<=5 AND cost_rank<=5

    4c2. CUMULATIVE/RUNNING WINDOW → always GROUP BY date first in a subquery, then apply window.
         WRONG: SUM(total_amount) OVER (ORDER BY order_date) FROM sales_order  (per-row window)
         CORRECT: SUM(daily_revenue) OVER (ORDER BY order_date) FROM (SELECT order_date::date,
                  SUM(total_amount) AS daily_revenue FROM ... GROUP BY order_date::date) t

    4d0. PERCENTAGE WITH CASE WHEN — never add WHERE status filter on the same column:
         When splitting by status with CASE WHEN, the denominator must include ALL rows.
         WRONG:   WHERE status IN ('closed','cancelled') ... SUM(total_amount) as denominator
         CORRECT: No WHERE on status. CASE WHEN handles split; SUM(total_amount) = all orders.

    4d1. ROUND() IN PostgreSQL — always cast to ::numeric first:
         WRONG:   ROUND(SUM(x) / SUM(y), 2)
         CORRECT: ROUND((SUM(x) / SUM(y))::numeric, 2)
         Applies to every ROUND(..., N) call — division results are double precision by default.

    4d2. "PER X" DENOMINATOR — use the correct divisor for what "per" refers to:
         "per order"    → COUNT(DISTINCT so.so_id)      NOT SUM(quantity)
         "per unit"     → SUM(quantity)                 NOT COUNT(DISTINCT so_id)
         "per customer" → COUNT(DISTINCT so.customer_id)
         "per vendor"   → COUNT(DISTINCT vendor_id)
         WRONG:   SUM(line_total) / SUM(quantity)        ← revenue per unit, not per order
         CORRECT: SUM(line_total) / COUNT(DISTINCT so.so_id) ← revenue per order

    4e. TABLE COLUMN OWNERSHIP — never use a column from the wrong table:
        sales_order_line_pricing → has: gold_amount_per_unit, diamond_amount_per_unit,
          making_charges_per_unit, line_total, quantity, sol_id, variant_sku, product_id
          ✗ does NOT have: gold_kt, gold_colour, shape, quality, diamond_id
        sales_order_line_gold → has: gold_kt, gold_colour, gold_rate_per_gm,
          metal_weight_per_unit (JOIN on sol_id when grouping/filtering by karat or colour)
        sales_order_line_diamond → has: shape, quality, diamond_id, carats_per_unit
          (JOIN on sol_id only for property filters, never for cost aggregation)
        WRONG:  SELECT lp.gold_kt ... FROM sales_order_line_pricing lp
        CORRECT: JOIN sales_order_line_gold g ON lp.sol_id = g.sol_id, then use g.gold_kt

    5. USE PRE-COMPUTED TOTALS — NEVER RECONSTRUCT THEM:
       - For order-level metrics (revenue, AOV): use sales_table_v2_sales_order.total_amount
       - For PO totals: use purchase_orders_v6_purchase_order.total_amount
       - NEVER add gold_amount + diamond_amount or any component columns —
         that always gives the WRONG answer (misses labour, taxes, etc.)

    6. STATUS = 'closed' IS THE DEFAULT — AND LINE TABLES HAVE NO STATUS COLUMN:
       sales_order_line_pricing / sales_order_line_gold / sales_order_line_diamond
       do NOT have a status column. When using these tables, you MUST join back to
       sales_table_v2_sales_order to apply the filter:
         JOIN sales_table_v2_sales_order_line sol ON lp.sol_id = sol.sol_id
         JOIN sales_table_v2_sales_order so ON sol.so_id = so.so_id
         WHERE so.status = 'closed'
       Omitting this join means ALL orders (cancelled, pending, open) are included — WRONG.

    6b. STATUS = 'closed' IS THE DEFAULT FOR ALL SALES ORDER QUERIES:
       Unless the question explicitly mentions a different status (e.g. "pending",
       "open", "cancelled", "all orders"), ALWAYS add WHERE so.status = 'closed'.
       This is not optional — omitting it returns incomplete/incorrect data.

       Correct formulas:
       - Revenue:           SUM(so.total_amount) ... WHERE so.status = 'closed'
       - AOV:               AVG(so.total_amount) ... WHERE so.status = 'closed'
       - Order count:       COUNT(DISTINCT so.so_id) ... WHERE so.status = 'closed'
       - Per-product rev:   SUM(lp.line_total) FROM sales_order_line_pricing lp
                            JOIN sales_table_v2_sales_order so ON so.so_id = sol.so_id
                            WHERE so.status = 'closed'
       - Component costs:   SUM(lp.diamond_amount_per_unit * lp.quantity)
                            FROM sales_order_line_pricing lp
                            JOIN sales_table_v2_sales_order_line sol ON lp.sol_id = sol.sol_id
                            JOIN sales_table_v2_sales_order so ON sol.so_id = so.so_id
                            WHERE so.status = 'closed'

    7. DATE FILTERING (order_date is TEXT 'YYYY-MM-DD'):
       - Use the EXACT year values from the [CONTEXT] block in the question.
       - Use: order_date >= 'YYYY-01-01' AND order_date <= 'YYYY-12-31'
       - Do NOT use EXTRACT() or CAST() on order_date.

    8. SIMPLICITY:
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
