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

    Runs in a background thread so the server starts instantly and the first
    user request hits pre-warmed caches instead of waiting 60+ seconds.
    """
    try:
        logger.info("Cache warm-up — starting background pre-load...")
        from db.schema import get_schema, format_schema
        from db.relationships import format_relationships
        from db.profiler import get_data_profile

        format_schema()
        logger.info("Cache warm-up — schema loaded")
        format_relationships()
        logger.info("Cache warm-up — relationships loaded")
        get_data_profile()
        logger.info("Cache warm-up — data profile loaded (all caches ready)")
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
    sql: str
    data: list
    row_count: int
    answer: str
    insights: str


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
    from db.memory import get_recent_history, add_turn

    logger.info(
        "CHAT request | provider=%s | conversation_id=%s | question=%s",
        req.provider,
        req.conversation_id or "default",
        req.question,
    )

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
        sql=result["sql"],
        data=result["data"],
        row_count=len(result.get("data") or []),
        answer=result["answer"],
        insights=result["insights"],
    )


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


# ── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
