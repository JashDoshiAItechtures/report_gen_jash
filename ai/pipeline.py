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
from datetime import date
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
from ai.sql_pattern_checker import check_sql_patterns, format_issues_for_repair
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

    @staticmethod
    def _build_question_with_context(question: str) -> str:
        """Prepend today's date so the model can resolve relative time references."""
        today = date.today()
        current_year = today.year
        last_year = current_year - 1
        return (
            f"[CONTEXT: Today is {today.isoformat()}. "
            f"Current year = {current_year}. "
            f"'Last year' = {last_year} ({last_year}-01-01 to {last_year}-12-31). "
            f"'This year' = {current_year} ({current_year}-01-01 to {current_year}-12-31).]\n\n"
            f"{question}"
        )

    def run(self, question: str) -> dict[str, Any]:
        """Run the full pipeline and return {sql, data, answer, insights}."""
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        question_with_date = self._build_question_with_context(question)

        # 1. Analyze & Plan (single LLM call replaces 3 former stages)
        logger.info("Stage 1 — Analyze & Plan")
        plan = self.analyze(
            question=question_with_date,
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
            question=question_with_date,
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
            sql_result = self.generate_sql(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text + f"\n\nPREVIOUS SQL HAD ISSUES: {schema_issues}. Fix them.",
            )
            sql = self._clean_sql(sql_result.sql_query)

        # 3.5 Pattern check — detect known structural bad patterns, force targeted repair
        pattern_issues = check_sql_patterns(sql)
        if pattern_issues:
            logger.warning(
                "Pattern issues detected: %s",
                [i["pattern_name"] for i in pattern_issues],
            )
            repair_instruction = format_issues_for_repair(pattern_issues)
            sql_result = self.generate_sql(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text + "\n\n" + repair_instruction,
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
                question=question_with_date,
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
        data_for_llm = self._annotate_indian_numbers(data[:25])
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

    def run_staged(self, question: str):
        """Generator that yields progress dicts after each pipeline stage.

        Used by the SSE streaming endpoint to send real-time progress
        to the frontend. Each yield is a dict with:
          - stage: str (stage identifier)
          - text: str  (user-friendly status message)
        The final yield includes the complete result payload.
        """
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        question_with_date = self._build_question_with_context(question)

        # Stage 1: Analyze & Plan
        yield {"stage": "analyzing", "text": "Analyzing your question..."}
        logger.info("Stage 1 — Analyze & Plan")
        plan = self.analyze(
            question=question_with_date,
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

        # Stage 2: SQL Generation
        yield {"stage": "generating_sql", "text": "Generating SQL query..."}
        logger.info("Stage 2 — SQL Generation")
        sql_result = self.generate_sql(
            question=question_with_date,
            schema_info=schema_str,
            query_plan=plan_text,
        )
        sql = self._clean_sql(sql_result.sql_query)

        # Stage 3: Schema Validation (instant)
        from db.schema import get_schema
        schema_valid, schema_issues = check_sql_against_schema(sql, get_schema())
        if not schema_valid:
            logger.warning(f"Schema issues detected: {schema_issues}")
            sql_result = self.generate_sql(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text + f"\n\nPREVIOUS SQL HAD ISSUES: {schema_issues}. Fix them.",
            )
            sql = self._clean_sql(sql_result.sql_query)

        # Pattern check
        pattern_issues = check_sql_patterns(sql)
        if pattern_issues:
            repair_instruction = format_issues_for_repair(pattern_issues)
            sql_result = self.generate_sql(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text + "\n\n" + repair_instruction,
            )
            sql = self._clean_sql(sql_result.sql_query)

        # Safety validation
        is_safe, reason = validate_sql(sql)
        if not is_safe:
            yield {
                "stage": "complete",
                "data": {"sql": sql, "data": [], "answer": f"Query rejected: {reason}", "insights": ""},
            }
            return

        # Stage 4: Execution
        yield {"stage": "executing", "text": "Running query on database..."}
        logger.info("Stage 4 — Executing SQL")
        exec_result = execute_sql(sql)

        for attempt in range(MAX_REPAIR_RETRIES):
            if exec_result["success"]:
                break
            yield {"stage": "repairing", "text": f"Fixing SQL (attempt {attempt + 1})..."}
            logger.warning(f"SQL error (attempt {attempt + 1}): {exec_result['error']}")
            repair_result = self.repair(
                sql_query=sql,
                error_message=exec_result["error"],
                schema_info=schema_str,
                question=question_with_date,
            )
            sql = self._clean_sql(repair_result.corrected_sql)
            is_safe, reason = validate_sql(sql)
            if not is_safe:
                yield {
                    "stage": "complete",
                    "data": {"sql": sql, "data": [], "answer": f"Repaired query rejected: {reason}", "insights": ""},
                }
                return
            exec_result = execute_sql(sql)

        if not exec_result["success"]:
            yield {
                "stage": "complete",
                "data": {"sql": sql, "data": [], "answer": f"Failed after {MAX_REPAIR_RETRIES} repairs. Error: {exec_result['error']}", "insights": ""},
            }
            return

        data = exec_result["data"]
        data_for_llm = self._annotate_indian_numbers(data[:25])
        results_json = json.dumps(data_for_llm, default=str)

        # Stage 5: Interpret
        yield {"stage": "interpreting", "text": "🧠 Generating insights..."}
        logger.info("Stage 5 — Interpret & Insight")
        result = self.interpret(
            question=question,
            sql_query=sql,
            query_results=results_json,
        )

        yield {
            "stage": "complete",
            "data": {
                "sql": sql,
                "data": data,
                "answer": result.answer,
                "insights": result.insights,
            },
        }

    def generate_sql_only(self, question: str) -> str:
        """Run the pipeline up to SQL generation and return just the SQL."""
        schema_str = format_schema()
        rels_str = format_relationships()
        profile_str = get_data_profile()

        question_with_date = self._build_question_with_context(question)

        plan = self.analyze(
            question=question_with_date,
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
            question=question_with_date,
            schema_info=schema_str,
            query_plan=plan_text,
        )
        sql = self._clean_sql(sql_result.sql_query)

        # Code-based schema check
        from db.schema import get_schema
        schema_valid, schema_issues = check_sql_against_schema(sql, get_schema())
        if not schema_valid:
            sql_result = self.generate_sql(
                question=question_with_date,
                schema_info=schema_str,
                query_plan=plan_text + f"\n\nPREVIOUS SQL HAD ISSUES: {schema_issues}. Fix them.",
            )
            sql = self._clean_sql(sql_result.sql_query)

        return sql

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _format_indian(num: float) -> str:
        """Format a number with Indian notation suffix for LLM clarity."""
        abs_num = abs(num)
        if abs_num >= 1e7:
            return f"₹{num / 1e7:.2f} Cr"
        elif abs_num >= 1e5:
            return f"₹{num / 1e5:.2f} L"
        elif abs_num >= 1e3:
            return f"₹{num / 1e3:.2f} K"
        return str(num)

    @classmethod
    def _annotate_indian_numbers(cls, rows: list) -> list:
        """Annotate large numeric values with Indian notation so the LLM
        doesn't miscalculate Cr/L/K conversions."""
        if not rows:
            return rows
        annotated = []
        for row in rows:
            new_row = {}
            for k, v in row.items():
                try:
                    num = float(v)
                    if abs(num) >= 1e5:
                        new_row[k] = cls._format_indian(num)
                    else:
                        new_row[k] = v
                except (TypeError, ValueError):
                    new_row[k] = v
            annotated.append(new_row)
        return annotated

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
