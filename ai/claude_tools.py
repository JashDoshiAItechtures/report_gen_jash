"""Tool definitions for the Claude multi-agent report pipeline.

Each tool has:
1. A JSON schema (for Claude's tool definition format)
2. A Python handler function (that actually runs the tool)

These tools are the "skills" that agents can call during their execution.
"""

import json
import logging
import re
from typing import Any

from ai.validator import validate_sql, check_sql_against_schema
from ai.sql_pattern_checker import check_sql_patterns
from db.schema import format_schema, get_schema
from db.relationships import format_relationships
from db.profiler import get_data_profile
from db.executor import execute_sql

logger = logging.getLogger(__name__)

# ── Lazy import to avoid circular dependency ────────────────────────────────
# _fix_report_sql is defined in report_generator.py; import at call time.


def _get_fix_report_sql():
    """Lazy import of _fix_report_sql to avoid circular imports."""
    from ai.report_generator import _fix_report_sql
    return _fix_report_sql


# ═══════════════════════════════════════════════════════════════════════════
# TOOL SCHEMAS — Claude API format
# ═══════════════════════════════════════════════════════════════════════════

TOOL_GET_DB_SCHEMA = {
    "name": "get_db_schema",
    "description": (
        "Returns the complete database schema with all tables and their "
        "columns (name, data type, nullability). Use this to understand "
        "what data is available."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOL_GET_RELATIONSHIPS = {
    "name": "get_relationships",
    "description": (
        "Returns discovered relationships between database tables including "
        "foreign keys, exact column matches, and inferred joins with "
        "confidence scores."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOL_GET_DATA_PROFILE = {
    "name": "get_data_profile",
    "description": (
        "Returns a data profile of key business tables including row counts, "
        "categorical column values with frequencies, numeric column ranges "
        "(min/max/avg), date ranges, and business intelligence rules."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOL_EXECUTE_SQL = {
    "name": "execute_sql_query",
    "description": (
        "Execute a PostgreSQL SELECT query against the database and return "
        "the results. The query is auto-corrected for common mistakes, "
        "validated for safety, and executed. Returns the data rows or an "
        "error message if the query fails. If you get an error, read it "
        "carefully and rewrite the query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The PostgreSQL SELECT query to execute.",
            },
            "purpose": {
                "type": "string",
                "description": (
                    "Brief description of what this query is for "
                    "(e.g., 'KPI: Total Revenue', 'Chart: Revenue by Month')."
                ),
            },
        },
        "required": ["sql"],
    },
}

TOOL_VALIDATE_SQL = {
    "name": "validate_sql_query",
    "description": (
        "Validate a SQL query against the actual database schema to check "
        "if all referenced tables and columns exist. Also checks for "
        "structural anti-patterns. Returns validation issues or 'valid'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL query to validate.",
            },
        },
        "required": ["sql"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS — Python functions that execute the tools
# ═══════════════════════════════════════════════════════════════════════════

def handle_get_db_schema() -> str:
    """Return formatted database schema."""
    try:
        return format_schema()
    except Exception as exc:
        logger.error("get_db_schema failed: %s", exc)
        return json.dumps({"error": str(exc)})


def handle_get_relationships() -> str:
    """Return formatted table relationships."""
    try:
        return format_relationships()
    except Exception as exc:
        logger.error("get_relationships failed: %s", exc)
        return json.dumps({"error": str(exc)})


def handle_get_data_profile() -> str:
    """Return data profile for business context."""
    try:
        return get_data_profile()
    except Exception as exc:
        logger.error("get_data_profile failed: %s", exc)
        return json.dumps({"error": str(exc)})


def handle_execute_sql_query(sql: str, purpose: str = "") -> str:
    """Execute SQL with auto-correction and safety validation.

    Applies the full validation chain:
    1. Auto-correct known LLM mistakes (_fix_report_sql)
    2. Safety validation (validate_sql)
    3. Execute and return results or error
    """
    try:
        # Step 1: Auto-correct common mistakes
        fix_sql = _get_fix_report_sql()
        corrected_sql = fix_sql(sql)

        if corrected_sql != sql:
            logger.info(
                "[SQL Tool] Auto-corrected SQL for '%s':\n  BEFORE: %s\n  AFTER:  %s",
                purpose,
                sql[:200],
                corrected_sql[:200],
            )

        # Step 2: Safety validation
        is_safe, reason = validate_sql(corrected_sql)
        if not is_safe:
            return json.dumps({
                "success": False,
                "error": f"Query rejected: {reason}",
                "corrected_sql": corrected_sql,
            })

        # Step 3: Execute
        result = execute_sql(corrected_sql)

        if result["success"]:
            data = result["data"]
            # Truncate large results to keep context manageable
            if len(data) > 50:
                data = data[:50]
                truncated = True
            else:
                truncated = False

            return json.dumps({
                "success": True,
                "data": data,
                "row_count": len(result["data"]),
                "columns": result.get("columns", []),
                "truncated": truncated,
                "executed_sql": corrected_sql,
            }, default=str)
        else:
            return json.dumps({
                "success": False,
                "error": result["error"],
                "executed_sql": corrected_sql,
            })

    except Exception as exc:
        logger.error("[SQL Tool] Execution error: %s", exc)
        return json.dumps({
            "success": False,
            "error": str(exc),
        })


def handle_validate_sql_query(sql: str) -> str:
    """Validate SQL against schema and check for anti-patterns."""
    issues: list[str] = []

    try:
        # Schema validation
        schema = get_schema()
        schema_valid, schema_issues = check_sql_against_schema(sql, schema)
        if not schema_valid:
            issues.extend(schema_issues)

        # Pattern checker
        pattern_issues = check_sql_patterns(sql)
        if pattern_issues:
            for pi in pattern_issues:
                issues.append(
                    f"{pi['pattern_name']}: {pi.get('description', pi.get('fix', ''))}"
                )

        # Safety check
        is_safe, reason = validate_sql(sql)
        if not is_safe:
            issues.append(f"Safety: {reason}")

    except Exception as exc:
        issues.append(f"Validation error: {str(exc)}")

    if issues:
        return json.dumps({"valid": False, "issues": issues})
    return json.dumps({"valid": True, "message": "SQL is valid"})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL COLLECTIONS — grouped by agent
# ═══════════════════════════════════════════════════════════════════════════

# Tool schemas grouped by which agents use them
CONTEXT_AGENT_TOOLS = [TOOL_GET_DB_SCHEMA, TOOL_GET_RELATIONSHIPS, TOOL_GET_DATA_PROFILE]
BA_AGENT_TOOLS = [TOOL_GET_DB_SCHEMA, TOOL_GET_DATA_PROFILE]
SQL_AGENT_TOOLS = [TOOL_EXECUTE_SQL, TOOL_VALIDATE_SQL]
# Data Analyst, Report Writer, QA — no tools (pure reasoning)

# Handler registry mapping tool names to functions
TOOL_HANDLERS: dict[str, Any] = {
    "get_db_schema": handle_get_db_schema,
    "get_relationships": handle_get_relationships,
    "get_data_profile": handle_get_data_profile,
    "execute_sql_query": handle_execute_sql_query,
    "validate_sql_query": handle_validate_sql_query,
}
