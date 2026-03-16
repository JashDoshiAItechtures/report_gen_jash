"""Main DSPy reasoning pipeline — optimized for speed.

Reduced from 9 stages to 4 LLM calls in the happy path:
1. AnalyzeAndPlan  (question understanding + schema analysis + query planning)
2. SQLGeneration
3. SQLCritiqueAndFix  (one pass; only retries on failure)
4. InterpretAndInsight  (interpretation + insights in one call)
"""

import json
import logging
import re
from typing import Any

import dspy

from ai.groq_setup import get_lm
from ai.signatures import (
    AnalyzeAndPlan,
    SQLGeneration,
    SQLRepair,
    InterpretAndInsight,
)
from ai.validator import validate_sql, check_sql_against_schema
from db.schema import format_schema
from db.relationships import format_relationships
from db.profiler import get_data_profile
from db.executor import execute_sql

logger = logging.getLogger(__name__)

MAX_REPAIR_RETRIES = 2


class SQLAnalystPipeline:
    """End-to-end reasoning pipeline: question → SQL → results → insights."""

    def __init__(self, provider: str = "groq"):
        # For now we always use the globally configured LM (Groq).
        self.provider = provider
        self._lm = get_lm(provider)

        # DSPy predict modules — rely on global dspy.settings
        self.analyze = dspy.Predict(AnalyzeAndPlan)
        self.generate_sql = dspy.Predict(SQLGeneration)
        self.interpret = dspy.Predict(InterpretAndInsight)
        self.repair = dspy.Predict(SQLRepair)

    # ── public API ──────────────────────────────────────────────────────

    def run(self, question: str) -> dict[str, Any]:
        """Run the full pipeline and return {sql, data, answer, insights}."""
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        # 1. Analyze & Plan (single LLM call replaces 3 former stages)
        logger.info("Stage 1 — Analyze & Plan")
        plan = self.analyze(
            question=question,
            schema_info=schema_str,
            relationships=rels_str,
            data_profile=profile_str,
        )

        plan_text = (
            f"Intent: {plan.intent}\n"
            f"Tables: {plan.relevant_tables}\n"
            f"Columns: {plan.relevant_columns}\n"
            f"Joins: {plan.join_conditions}\n"
            f"Where: {plan.where_conditions}\n"
            f"Aggregations: {plan.aggregations}\n"
            f"Group By: {plan.group_by}\n"
            f"Order By: {plan.order_by}\n"
            f"Limit: {plan.limit_val}"
        )

        # 2. SQL Generation
        logger.info("Stage 2 — SQL Generation")
        sql_result = self.generate_sql(
            question=question,
            schema_info=schema_str,
            query_plan=plan_text,
        )
        sql = self._clean_sql(sql_result.sql_query)

        # 3. Code-based schema validation (instant — no LLM call)
        logger.info("Stage 3 — Schema Validation")
        from db.schema import get_schema
        schema_valid, schema_issues = check_sql_against_schema(sql, get_schema())
        if not schema_valid:
            logger.warning(f"Schema issues detected: {schema_issues}")
            # Try regenerating SQL once with the issues as feedback
            sql_result = self.generate_sql(
                question=question,
                schema_info=schema_str,
                query_plan=plan_text + f"\n\nPREVIOUS SQL HAD ISSUES: {schema_issues}. Fix them.",
            )
            sql = self._clean_sql(sql_result.sql_query)

        # 4. Safety validation (no LLM call)
        is_safe, reason = validate_sql(sql)
        if not is_safe:
            return {
                "sql": sql,
                "data": [],
                "answer": f"Query rejected: {reason}",
                "insights": "",
            }

        # 5. SQL Execution + repair loop
        logger.info("Stage 4 — Executing SQL")
        exec_result = execute_sql(sql)

        for attempt in range(MAX_REPAIR_RETRIES):
            if exec_result["success"]:
                break
            logger.warning(f"SQL error (attempt {attempt + 1}): {exec_result['error']}")
            repair_result = self.repair(
                sql_query=sql,
                error_message=exec_result["error"],
                schema_info=schema_str,
                question=question,
            )
            sql = self._clean_sql(repair_result.corrected_sql)
            is_safe, reason = validate_sql(sql)
            if not is_safe:
                return {
                    "sql": sql,
                    "data": [],
                    "answer": f"Repaired query rejected: {reason}",
                    "insights": "",
                }
            exec_result = execute_sql(sql)

        if not exec_result["success"]:
            return {
                "sql": sql,
                "data": [],
                "answer": f"Failed after {MAX_REPAIR_RETRIES} repairs. Error: {exec_result['error']}",
                "insights": "",
            }

        data = exec_result["data"]
        data_for_llm = data[:50]
        results_json = json.dumps(data_for_llm, default=str)

        # 6. Interpret & Insight (single LLM call replaces 2 former stages)
        logger.info("Stage 5 — Interpret & Insight")
        result = self.interpret(
            question=question,
            sql_query=sql,
            query_results=results_json,
        )

        return {
            "sql": sql,
            "data": data,
            "answer": result.answer,
            "insights": result.insights,
        }

    def generate_sql_only(self, question: str) -> str:
        """Run the pipeline up to SQL generation and return just the SQL."""
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        plan = self.analyze(
            question=question,
            schema_info=schema_str,
            relationships=rels_str,
            data_profile=profile_str,
        )

        plan_text = (
            f"Intent: {plan.intent}\n"
            f"Tables: {plan.relevant_tables}\n"
            f"Columns: {plan.relevant_columns}\n"
            f"Joins: {plan.join_conditions}\n"
            f"Where: {plan.where_conditions}\n"
            f"Aggregations: {plan.aggregations}\n"
            f"Group By: {plan.group_by}\n"
            f"Order By: {plan.order_by}\n"
            f"Limit: {plan.limit_val}"
        )

        sql_result = self.generate_sql(
            question=question,
            schema_info=schema_str,
            query_plan=plan_text,
        )
        sql = self._clean_sql(sql_result.sql_query)

        # Code-based schema check
        from db.schema import get_schema
        schema_valid, schema_issues = check_sql_against_schema(sql, get_schema())
        if not schema_valid:
            sql_result = self.generate_sql(
                question=question,
                schema_info=schema_str,
                query_plan=plan_text + f"\n\nPREVIOUS SQL HAD ISSUES: {schema_issues}. Fix them.",
            )
            sql = self._clean_sql(sql_result.sql_query)

        return sql

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _clean_sql(raw: str) -> str:
        """Strip markdown fences, trailing prose, and whitespace from LLM SQL."""
        sql = raw.strip()

        # 1. Remove ```sql ... ``` wrappers
        if sql.startswith("```"):
            lines = sql.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            sql = "\n".join(lines).strip()

        # 2. Extract only the first valid SQL statement
        match = re.search(
            r"((?:SELECT|WITH)\b[\s\S]*?)(;|\n\n(?=[A-Z][a-z])|$)",
            sql,
            re.IGNORECASE,
        )
        if match:
            sql = match.group(1).strip()

        # 3. Remove trailing lines that look like natural language
        cleaned_lines: list[str] = []
        for line in sql.split("\n"):
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append(line)
                continue
            if re.match(
                r"^(However|Note|This|The|Please|But|Also|In |It |I |Here|Since|Because|Although|Unfortunately)",
                stripped,
            ):
                break
            cleaned_lines.append(line)

        sql = "\n".join(cleaned_lines).strip()

        # 4. Remove trailing semicolons
        sql = sql.rstrip(";")

        return sql
