"""FastAPI application — AI SQL Analyst API and frontend server."""

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="AI SQL Analyst", version="1.0.0")


def _warm_caches():
    """Pre-build schema, relationship, and data-profile caches at startup.

    Runs in a background thread so the server starts instantly.
    Any request that arrives before the profile is ready gets the static
    business rules immediately (non-blocking) and the full profile on the
    next request.
    """
    try:
        logger.info("Cache warm-up — starting background pre-load...")
        from db.schema import format_schema
        from db.relationships import format_relationships
        import db.profiler as _profiler

        format_schema()
        logger.info("Cache warm-up — schema loaded")
        format_relationships()
        logger.info("Cache warm-up — relationships loaded")

        # Try to load from persistent DB cache first (milliseconds)
        loaded = _profiler.load_profile_from_db_cache()
        if loaded:
            logger.info("Cache warm-up — profile loaded from DB cache (instant)")
        else:
            # No DB cache yet (first ever deploy) — build from scratch
            logger.info("Cache warm-up — no DB cache found, building profile...")
            _profiler._do_build()
            logger.info("Cache warm-up — profile built and saved to DB")
    except Exception as exc:
        logger.warning("Cache warm-up failed (non-fatal): %s", exc)


# Kick off cache pre-loading as soon as the module is imported
threading.Thread(target=_warm_caches, daemon=True).start()

# ── CORS ────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response schemas ──────────────────────────────────────────────

class QuestionRequest(BaseModel):
    question: str
    provider: str = "groq"       # "groq" | "openai"
    conversation_id: str | None = None


class GenerateSQLResponse(BaseModel):
    sql: str


class ExecuteSQLRequest(BaseModel):
    sql: str


class ExecuteSQLResponse(BaseModel):
    sql: str
    data: list
    row_count: int
    error: str | None = None


class ChatResponse(BaseModel):
    mode: str = "chat"
    sql: str
    data: list
    row_count: int
    answer: str
    insights: str


class ReportRequest(BaseModel):
    question: str
    provider: str = "groq"
    conversation_id: str | None = None
    # Filters
    date_from: str | None = None
    date_to: str | None = None
    aggregation: str | None = None  # daily|weekly|monthly|quarterly|yearly
    category: str | None = None
    customer: str | None = None
    status: str | None = None
    product: str | None = None


class ReportApplyFiltersRequest(BaseModel):
    """Apply filters to an existing report without re-generating via LLM."""
    report: dict  # The current report JSON
    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None
    customer: str | None = None
    status: str | None = None
    product: str | None = None
    provider: str = "groq"


class ReportModifyRequest(BaseModel):
    report_json: str
    modification: str
    provider: str = "groq"


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.post("/generate-sql", response_model=GenerateSQLResponse)
def generate_sql_endpoint(req: QuestionRequest):
    """Generate SQL for a question without executing it."""
    from ai.pipeline import SQLAnalystPipeline

    pipeline = SQLAnalystPipeline(provider=req.provider)
    sql = pipeline.generate_sql_only(req.question)
    return GenerateSQLResponse(sql=sql)


@app.post("/execute-sql", response_model=ExecuteSQLResponse)
def execute_sql_endpoint(req: ExecuteSQLRequest):
    """Execute a raw SQL SELECT query and return the results."""
    from ai.validator import validate_sql
    from db.executor import execute_sql

    is_safe, reason = validate_sql(req.sql)
    if not is_safe:
        return ExecuteSQLResponse(
            sql=req.sql,
            data=[],
            row_count=0,
            error=f"Query rejected: {reason}",
        )

    result = execute_sql(req.sql)
    if not result["success"]:
        return ExecuteSQLResponse(
            sql=req.sql,
            data=[],
            row_count=0,
            error=result["error"],
        )

    data = result["data"]
    return ExecuteSQLResponse(
        sql=req.sql,
        data=data,
        row_count=len(data),
    )


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: QuestionRequest):
    from ai.pipeline import SQLAnalystPipeline
    from ai.report_generator import classify_intent
    from db.memory import get_recent_history, add_turn

    logger.info(
        "CHAT request | provider=%s | conversation_id=%s | question=%s",
        req.provider,
        req.conversation_id or "default",
        req.question,
    )

    # Classify intent — deterministic, no LLM call
    intent = classify_intent(req.question)
    logger.info("CHAT intent classified as: %s", intent)

    conversation_id = req.conversation_id or "default"

    history = get_recent_history(conversation_id, limit=5)

    # Augment the question with recent conversation context
    if history:
        logger.info(
            "CHAT context | conversation_id=%s | history_turns=%d",
            conversation_id,
            len(history),
        )
        history_lines: list[str] = ["You are in a multi-turn conversation. Here are the recent exchanges:"]
        for turn in history:
            history_lines.append(f"User: {turn['question']}")
            history_lines.append(f"Assistant: {turn['answer']}")
        history_lines.append(f"Now the user asks: {req.question}")
        question_with_context = "\n".join(history_lines)
    else:
        logger.info(
            "CHAT context | conversation_id=%s | history_turns=0 (no prior context used)",
            conversation_id,
        )
        question_with_context = req.question

    pipeline = SQLAnalystPipeline(provider=req.provider)
    result = pipeline.run(question_with_context)

    logger.info(
        "CHAT result | conversation_id=%s | used_context=%s | sql_preview=%s",
        conversation_id,
        "yes" if history else "no",
        (result.get("sql") or "").replace("\n", " ")[:200],
    )

    # Persist this turn for future context (store up to 200 rows so modal can show them)
    add_turn(
        conversation_id,
        req.question,
        result["answer"],
        result["sql"],
        query_result=(result["data"][:200] if result.get("data") else None),
    )

    return ChatResponse(
        mode="chat",
        sql=result["sql"],
        data=result["data"],
        row_count=len(result.get("data") or []),
        answer=result["answer"],
        insights=result["insights"],
    )


@app.post("/report")
def report_endpoint(req: ReportRequest):
    """Generate a full analytics report from a natural-language question."""
    from ai.report_generator import ReportPipeline

    # Build filter context string for the LLM
    filters = []
    if req.date_from:
        filters.append(f"Date range: from {req.date_from}")
    if req.date_to:
        filters.append(f"to {req.date_to}")
    if req.aggregation:
        filters.append(f"Time aggregation: {req.aggregation}")
    if req.category:
        filters.append(f"Category filter: {req.category}")
    if req.customer:
        filters.append(f"Customer filter: {req.customer}")
    if req.status:
        filters.append(f"Order status filter: {req.status}")
    if req.product:
        filters.append(f"Product filter: {req.product}")

    filter_ctx = ""
    if filters:
        filter_ctx = "\n[ACTIVE FILTERS: " + ", ".join(filters) + ". Apply these filters in ALL SQL WHERE clauses.]"

    question_with_filters = req.question + filter_ctx

    logger.info("REPORT request | question=%s | filters=%s", req.question, filter_ctx or "none")
    pipeline = ReportPipeline(provider=req.provider)
    return pipeline.generate(question_with_filters)


@app.post("/report/apply-filters")
def report_apply_filters_endpoint(req: ReportApplyFiltersRequest):
    """Apply filters to an existing report by injecting WHERE clauses into SQL.

    This does NOT call the LLM — it modifies the existing SQL queries directly,
    making it much faster and more reliable than regenerating the entire report.
    """
    from ai.report_generator import ReportPipeline

    filters = {}
    if req.date_from:
        filters["date_from"] = req.date_from
    if req.date_to:
        filters["date_to"] = req.date_to
    if req.category:
        filters["category"] = req.category
    if req.customer:
        filters["customer"] = req.customer
    if req.status:
        filters["status"] = req.status
    if req.product:
        filters["product"] = req.product

    logger.info("REPORT APPLY-FILTERS | filters=%s", filters)
    pipeline = ReportPipeline(provider=req.provider)
    return pipeline.apply_filters(req.report, filters)


@app.post("/report/modify")
def report_modify_endpoint(req: ReportModifyRequest):
    """Modify an existing report based on a natural-language command."""
    from ai.report_generator import ReportPipeline

    logger.info("REPORT MODIFY | command=%s", req.modification)
    pipeline = ReportPipeline(provider=req.provider)
    return pipeline.modify(req.report_json, req.modification)


# ── Filter values endpoint ──────────────────────────────────────────────────

@app.get("/report/filters")
def report_filters_endpoint():
    """Return distinct filter values for the report filter bar."""
    from db.executor import execute_sql

    result = {}

    # Categories
    cat_res = execute_sql("SELECT DISTINCT category FROM product_master WHERE category IS NOT NULL ORDER BY category")
    result["categories"] = [r["category"] for r in (cat_res["data"] if cat_res["success"] else [])]

    # Customers
    cust_res = execute_sql("SELECT DISTINCT customer_name FROM customer_master WHERE customer_name IS NOT NULL ORDER BY customer_name LIMIT 100")
    result["customers"] = [r["customer_name"] for r in (cust_res["data"] if cust_res["success"] else [])]

    # Products (top 100)
    prod_res = execute_sql("SELECT DISTINCT product_name FROM product_master WHERE product_name IS NOT NULL ORDER BY product_name LIMIT 100")
    result["products"] = [r["product_name"] for r in (prod_res["data"] if prod_res["success"] else [])]

    # Statuses
    stat_res = execute_sql("SELECT DISTINCT status FROM sales_order WHERE status IS NOT NULL ORDER BY status")
    result["statuses"] = [r["status"] for r in (stat_res["data"] if stat_res["success"] else [])]

    # Date range
    date_res = execute_sql("SELECT MIN(order_date)::text AS min_date, MAX(order_date)::text AS max_date FROM sales_order")
    if date_res["success"] and date_res["data"]:
        result["date_range"] = date_res["data"][0]
    else:
        result["date_range"] = {"min_date": None, "max_date": None}

    return result


# ── Schema info endpoint (for debugging / transparency) ─────────────────────

@app.get("/history")
def history_endpoint(conversation_id: str = "default"):
    from db.memory import get_full_history
    return get_full_history(conversation_id)


@app.delete("/history/{turn_id}")
def delete_turn_endpoint(turn_id: int):
    from db.memory import delete_turn
    delete_turn(turn_id)
    return {"ok": True}


@app.get("/history/{turn_id}/sql")
def history_sql_endpoint(turn_id: int):
    """Return just the SQL query for a specific history turn."""
    from db.memory import get_turn_by_id
    turn = get_turn_by_id(turn_id)
    if not turn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Turn not found")
    return {"turn_id": turn_id, "sql": turn.get("sql_query")}


@app.get("/history/{turn_id}/result")
def history_result_endpoint(turn_id: int):
    """Return just the query result data for a specific history turn."""
    from db.memory import get_turn_by_id
    turn = get_turn_by_id(turn_id)
    if not turn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Turn not found")
    data = turn.get("query_result") or []
    return {"turn_id": turn_id, "data": data, "row_count": len(data)}


@app.get("/history/{turn_id}/answer")
def history_answer_endpoint(turn_id: int):
    """Return just the AI answer/explanation for a specific history turn."""
    from db.memory import get_turn_by_id
    turn = get_turn_by_id(turn_id)
    if not turn:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Turn not found")
    return {"turn_id": turn_id, "question": turn.get("question"), "answer": turn.get("answer")}


@app.get("/schema")
def schema_endpoint():
    from db.schema import get_schema
    return get_schema()


@app.get("/relationships")
def relationships_endpoint():
    from db.relationships import discover_relationships
    rels = discover_relationships()
    return [
        {
            "table_a": r.table_a, "column_a": r.column_a,
            "table_b": r.table_b, "column_b": r.column_b,
            "confidence": r.confidence, "source": r.source,
        }
        for r in rels
    ]


# ── Frontend static files ──────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent / "frontend"

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/report-view")
def serve_report_view():
    """Serve the standalone report viewer page (opens in new tab)."""
    return FileResponse(str(FRONTEND_DIR / "report.html"))


# ── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
