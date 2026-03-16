"""Conversation memory stored in PostgreSQL (Neon).

Keeps the last N turns per conversation so the AI can
use recent context for follow‑up questions.
"""

from __future__ import annotations

from typing import Any, List, Dict

from sqlalchemy import text

from db.connection import get_engine


_TABLE_CREATED = False


def _ensure_table() -> None:
    """Create the chat_history table if it doesn't exist."""
    global _TABLE_CREATED
    if _TABLE_CREATED:
        return

    ddl = text(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            sql_query TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(ddl)

    _TABLE_CREATED = True


def add_turn(conversation_id: str, question: str, answer: str, sql_query: str | None) -> None:
    """Append a single Q/A turn to the history."""
    _ensure_table()
    engine = get_engine()
    insert_stmt = text(
        """
        INSERT INTO chat_history (conversation_id, question, answer, sql_query)
        VALUES (:conversation_id, :question, :answer, :sql_query)
        """
    )
    with engine.begin() as conn:
        conn.execute(
            insert_stmt,
            {
                "conversation_id": conversation_id,
                "question": question,
                "answer": answer,
                "sql_query": sql_query,
            },
        )


def get_recent_history(conversation_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Return the most recent `limit` turns for a conversation (oldest first)."""
    _ensure_table()
    engine = get_engine()
    query = text(
        """
        SELECT question, answer, sql_query, created_at
        FROM chat_history
        WHERE conversation_id = :conversation_id
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            query, {"conversation_id": conversation_id, "limit": limit}
        ).mappings().all()

    # Reverse so caller sees oldest → newest
    return list(reversed([dict(r) for r in rows]))

