"""FastAPI application — AI SQL Analyst API and frontend server."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="AI SQL Analyst", version="1.0.0")

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


class ChatResponse(BaseModel):
    sql: str
    data: list
    answer: str
    insights: str


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.post("/generate-sql", response_model=GenerateSQLResponse)
def generate_sql_endpoint(req: QuestionRequest):
    from ai.pipeline import SQLAnalystPipeline

    pipeline = SQLAnalystPipeline(provider=req.provider)
    sql = pipeline.generate_sql_only(req.question)
    return GenerateSQLResponse(sql=sql)


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

    # Persist this turn for future context
    add_turn(conversation_id, req.question, result["answer"], result["sql"])

    return ChatResponse(**result)


# ── Schema info endpoint (for debugging / transparency) ─────────────────────

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
