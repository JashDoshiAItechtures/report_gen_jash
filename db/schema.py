"""Database schema introspection via information_schema.

Provides cached access to table/column metadata so the AI pipeline
always works with the real database structure.
"""

import time
from typing import Any

from sqlalchemy import text

from db.connection import get_engine

# ── Cache ───────────────────────────────────────────────────────────────────
_schema_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_CACHE_TTL: float = 300.0  # 5 minutes


def get_schema(force_refresh: bool = False) -> dict[str, list[dict]]:
    """Return {table_name: [{column_name, data_type, is_nullable}, …]}.

    Results are cached for 5 minutes.
    """
    global _schema_cache, _cache_ts

    if not force_refresh and _schema_cache and (time.time() - _cache_ts < _CACHE_TTL):
        return _schema_cache

    query = text("""
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)

    schema: dict[str, list[dict]] = {}
    with get_engine().connect() as conn:
        rows = conn.execute(query).fetchall()

    for row in rows:
        table = row[0]
        col_info = {
            "column_name": row[1],
            "data_type": row[2],
            "is_nullable": row[3],
        }
        schema.setdefault(table, []).append(col_info)

    _schema_cache = schema
    _cache_ts = time.time()
    return schema


def format_schema(schema: dict[str, list[dict]] | None = None) -> str:
    """Format schema as a readable string for prompt injection."""
    if schema is None:
        schema = get_schema()

    lines: list[str] = []
    for table, columns in schema.items():
        col_strs = []
        for c in columns:
            nullable = "NULL" if c["is_nullable"] == "YES" else "NOT NULL"
            col_strs.append(f"    {c['column_name']}  {c['data_type']}  {nullable}")
        lines.append(f"TABLE: {table}")
        lines.extend(col_strs)
        lines.append("")

    return "\n".join(lines)


def get_table_names() -> list[str]:
    """Return all public table names."""
    return list(get_schema().keys())
