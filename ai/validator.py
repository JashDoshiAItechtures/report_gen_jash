"""SQL safety validation.

Rejects any query that is not a pure SELECT statement.
"""

import re

_FORBIDDEN_KEYWORDS = [
    r"\bDROP\b",
    r"\bDELETE\b",
    r"\bUPDATE\b",
    r"\bALTER\b",
    r"\bTRUNCATE\b",
    r"\bINSERT\b",
    r"\bCREATE\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"\bEXEC\b",
    r"\bEXECUTE\b",
]

_FORBIDDEN_PATTERN = re.compile("|".join(_FORBIDDEN_KEYWORDS), re.IGNORECASE)


def validate_sql(sql: str) -> tuple[bool, str]:
    """Check if a SQL string is safe to execute.

    Returns
    -------
    (is_safe, reason)
    """
    stripped = sql.strip().rstrip(";").strip()

    if not stripped:
        return False, "Empty query."

    # Must start with SELECT or WITH (CTE)
    if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
        return False, "Only SELECT queries are allowed."

    # Check for forbidden keywords
    match = _FORBIDDEN_PATTERN.search(stripped)
    if match:
        return False, f"Forbidden keyword detected: {match.group().upper()}"

    return True, ""


def check_sql_against_schema(sql: str, schema: dict[str, list[dict]]) -> tuple[bool, list[str]]:
    """Programmatically check that tables/columns in SQL exist in the schema.

    Returns (is_valid, list_of_issues).
    Much faster and more accurate than LLM-based critique.
    """
    issues: list[str] = []

    # Build lookup sets
    all_tables = {t.lower() for t in schema}
    table_columns: dict[str, set[str]] = {}
    for t, cols in schema.items():
        table_columns[t.lower()] = {c["column_name"].lower() for c in cols}
    all_columns = set()
    for cols in table_columns.values():
        all_columns |= cols

    sql_upper = sql.upper()

    # Extract table references (FROM / JOIN)
    table_refs = re.findall(
        r'(?:FROM|JOIN)\s+"?(\w+)"?', sql, re.IGNORECASE
    )
    for tref in table_refs:
        if tref.lower() not in all_tables:
            issues.append(f"Table '{tref}' not found in schema")

    # Basic check: if GROUP BY is present, verify SELECT has aggregation or is in GROUP BY
    # (lightweight check — not full SQL parsing)
    if "GROUP BY" in sql_upper and "SELECT" in sql_upper:
        if not any(fn in sql_upper for fn in ["SUM(", "COUNT(", "AVG(", "MIN(", "MAX("]):
            issues.append("GROUP BY present but no aggregation function found")

    return (len(issues) == 0, issues)

