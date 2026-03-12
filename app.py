"""FastAPI application — AI SQL Analyst API and frontend server."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s")

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

    pipeline = SQLAnalystPipeline(provider=req.provider)
    result = pipeline.run(req.question)
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
