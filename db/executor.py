"""Safe SQL execution against PostgreSQL.

Only SELECT queries are allowed. Results are returned as list[dict].
Database errors are captured and returned for the AI repair loop.
"""

from typing import Any

from sqlalchemy import text

from db.connection import get_engine
from ai.validator import validate_sql


def execute_sql(sql: str) -> dict[str, Any]:
    """Execute a SQL query and return results or error.

    Returns
    -------
    dict with keys:
        success : bool
        data    : list[dict]   (on success)
        columns : list[str]    (on success)
        error   : str          (on failure)
    """
    # Safety gate
    is_safe, reason = validate_sql(sql)
    if not is_safe:
        return {"success": False, "data": [], "columns": [], "error": reason}

    try:
        with get_engine().connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return {"success": True, "data": rows, "columns": columns, "error": ""}
    except Exception as exc:
        return {"success": False, "data": [], "columns": [], "error": str(exc)}
