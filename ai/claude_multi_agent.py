"""Multi-agent report generation orchestrator using Claude API.

Chains 6 specialized agents to produce high-quality analytics reports:
1. Context Agent       — understands the user's question and database
2. Business Analyst    — designs the report blueprint
3. SQL Agent           — writes and executes queries (with tool use)
4. Data Analyst        — validates and cleans the data
5. Report Writer       — writes narrative components
6. QA Agent            — quality assurance gate

The output is 100% compatible with the existing frontend JSON format.
"""

import json
import logging
import re
import time
from datetime import date
from typing import Any

import config

from ai.claude_client import ClaudeClient, _c, _tee, _BOLD, _DIM, _CYAN, _GREEN, _YELLOW, _RED, _BLUE, _MAGENTA, _WHITE, _GREY, _R
from ai.claude_tools import (
    CONTEXT_AGENT_TOOLS,
    BA_AGENT_TOOLS,
    SQL_AGENT_TOOLS,
    TOOL_HANDLERS,
)
from ai.claude_prompts import (
    CONTEXT_AGENT_SYSTEM,
    BUSINESS_ANALYST_SYSTEM,
    get_sql_agent_system,
    DATA_ANALYST_SYSTEM,
    REPORT_WRITER_SYSTEM,
    QA_AGENT_SYSTEM,
)
from db.schema import format_schema
from db.relationships import format_relationships
from db.profiler import get_data_profile

logger = logging.getLogger(__name__)

# ── Model aliases ─────────────────────────────────────────────────────────────
_SONNET = config.CLAUDE_MODEL        # Business Analyst, SQL Agent, Report Writer
_HAIKU  = config.CLAUDE_MODEL        # Fallback to Sonnet for now to avoid 404s

# ── Agent display config ─────────────────────────────────────────────────────
_AGENTS = [
    ("1", "CONTEXT AGENT",        "🔍", "Analyzing question & gathering database context"),
    ("2", "BUSINESS ANALYST",     "📐", "Designing report blueprint (KPIs + Charts)"),
    ("3", "SQL AGENT",            "⚡", "Writing & executing SQL queries with tool use"),
    ("4", "DATA ANALYST",         "🔬", "Validating & cleaning query results"),
    ("5", "REPORT WRITER",        "✍️ ", "Writing executive narrative & insights"),
    ("6", "QA AGENT",             "🛡️ ", "Quality assurance — scoring report against question"),
]


def _pipeline_banner(question: str) -> None:
    """Print the pipeline start banner."""
    q_preview = question[:70] + ("..." if len(question) > 70 else "")
    width = 70
    _tee("\n" + _c("╔" + "═" * width + "╗", _CYAN))
    _tee(_c("║", _CYAN) + _c("  🤖  CLAUDE MULTI-AGENT REPORT PIPELINE" + " " * (width - 40) + "  ", _WHITE, _BOLD) + _c("║", _CYAN))
    _tee(_c("║", _CYAN) + "  " + _c(f"Q: {q_preview}", _DIM) + " " * max(0, width - 4 - len(q_preview)) + _c("║", _CYAN))
    _tee(_c("╚" + "═" * width + "╝", _CYAN) + "\n")


def _agent_header(num: str, name: str, icon: str, desc: str) -> None:
    """Print an agent section header."""
    width = 68
    header = f" AGENT {num}: {name} "
    pad = width - len(header) - 2
    _tee(_c("┌" + "─" * width + "┐", _BLUE))
    _tee(_c("│", _BLUE) + _c(f"  {icon} {header}", _CYAN, _BOLD) + " " * pad + _c("│", _BLUE))
    _tee(_c("│", _BLUE) + _c(f"     {desc}", _DIM) + " " * (width - 5 - len(desc)) + _c("│", _BLUE))
    _tee(_c("└" + "─" * width + "┘", _BLUE))


def _agent_result(name: str, elapsed: float, summary_lines: list[str]) -> None:
    """Print the result summary after an agent completes."""
    _tee(
        f"  {_c('✓ ' + name + ' COMPLETE', _GREEN, _BOLD)}  "
        f"{_c(f'{elapsed:.1f}s', _YELLOW)}"
    )
    for line in summary_lines:
        _tee(f"  {_c('  ' + line, _DIM)}")
    _tee("")


def _pipeline_complete(total_elapsed: float, report: dict) -> None:
    """Print the pipeline completion banner."""
    width = 70
    kpi_count   = len(report.get("kpis",    []))
    chart_count = len(report.get("charts",  []))
    ins_count   = len(report.get("insights",[]))
    _tee(_c("╔" + "═" * width + "╗", _GREEN))
    _tee(_c("║", _GREEN) + _c("  ✅  PIPELINE COMPLETE" + " " * (width - 22) + "  ", _GREEN, _BOLD) + _c("║", _GREEN))
    _tee(_c("║", _GREEN) + f"  {_c(f'Time: {total_elapsed:.1f}s  KPIs: {kpi_count}  Charts: {chart_count}  Insights: {ins_count}', _DIM)}" + " " * max(0, width - 47) + _c("║", _GREEN))
    _tee(_c("╚" + "═" * width + "╝\n", _GREEN))


class ClaudeReportPipeline:
    """Multi-agent report generation using Claude API."""

    def __init__(self):
        self.client = ClaudeClient()
        self._retry_count = 0

    # ═══════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════

    def generate(self, question: str, force_refresh: bool = False) -> dict[str, Any]:
        """Generate a complete report using the 6-agent pipeline."""
        pipeline_start = time.time()
        _pipeline_banner(question)
        logger.info("Claude pipeline START — question: %s", question[:120])

        self._retry_count = 0

        try:
            # ── Agent 1: Context Agent ─────────────────────────────────────
            _agent_header(*_AGENTS[0])
            t0 = time.time()
            context = self._run_context_agent(question)
            _agent_result("CONTEXT AGENT", time.time() - t0, [
                f"Subject  : {context.get('subject', '?')}",
                f"Domain   : {context.get('business_domain', '?')}",
                f"Intent   : {context.get('intent', '?')}",
                f"Timeframe: {context.get('timeframe', 'all time')}",
                f"Tables   : {', '.join(context.get('relevant_tables', [])[:6])}",
            ])

            # ── Agent 2: Business Analyst Agent ───────────────────────────
            _agent_header(*_AGENTS[1])
            t0 = time.time()
            blueprint = self._run_ba_agent(question, context)
            kpi_labels   = [k.get("label","?") for k in blueprint.get("kpis",   [])[:6]]
            chart_titles = [c.get("title","?") for c in blueprint.get("charts", [])[:6]]
            _agent_result("BUSINESS ANALYST", time.time() - t0, [
                f"Title   : {blueprint.get('title','?')}",
                f"KPIs    : {', '.join(kpi_labels)}",
                f"Charts  : {', '.join(chart_titles)}",
            ])

            # ── Agent 3: SQL Agent ─────────────────────────────────────────
            _agent_header(*_AGENTS[2])
            t0 = time.time()
            report_with_data = self._run_sql_agent(question, blueprint, context)
            kpi_values = [
                f"{k.get('label','?')}={k.get('value','?')}"
                for k in report_with_data.get("kpis", [])[:3]
            ]
            chart_rows = [
                f"{c.get('title','?')} ({len(c.get('data',[]))} rows)"
                for c in report_with_data.get("charts", [])[:4]
            ]
            _agent_result("SQL AGENT", time.time() - t0, [
                f"KPI samples  : {', '.join(kpi_values)}",
                f"Chart data   : {', '.join(chart_rows)}",
            ])

            # ── Agent 4: Data Analyst Agent ───────────────────────────────
            _agent_header(*_AGENTS[3])
            t0 = time.time()
            cleaned_report = self._run_data_analyst_agent(report_with_data)
            notes = cleaned_report.get("data_quality_notes", "No issues found")
            notes_str = notes if isinstance(notes, str) else json.dumps(notes)[:100]
            _agent_result("DATA ANALYST", time.time() - t0, [
                f"Quality notes: {notes_str[:100]}",
            ])

            # ── Agent 5: Report Writer Agent ──────────────────────────────
            _agent_header(*_AGENTS[4])
            t0 = time.time()
            final_report = self._run_report_writer_agent(cleaned_report, context)
            summary_preview = final_report.get("summary", "")[:120].replace("\n", " ")
            ins_count = len(final_report.get("insights", []))
            _agent_result("REPORT WRITER", time.time() - t0, [
                f"Summary   : {summary_preview}...",
                f"Insights  : {ins_count} generated",
            ])

            # ── Agent 6: QA Agent ─────────────────────────────────────────
            _agent_header(*_AGENTS[5])
            t0 = time.time()
            qa_result = self._run_qa_agent(question, final_report)
            approved = qa_result.get("approved", True)
            score    = qa_result.get("score", "?")
            max_sc   = qa_result.get("max_score", 8)
            feedback = qa_result.get("feedback", "")[:80]
            status_col = _GREEN if approved else _RED
            _tee(
                f"  {_c('QA VERDICT', _BOLD)}: {_c('APPROVED' if approved else 'REJECTED', status_col, _BOLD)}  "
                f"{_c(f'Score: {score}/{max_sc}', _YELLOW)}  {_c(f'{time.time()-t0:.1f}s', _DIM)}"
            )
            _tee(f"  {_c(f'  Feedback: {feedback}', _DIM)}\n")

            # ── QA retry if rejected ───────────────────────────────────────
            if not approved and self._retry_count < 1:
                self._retry_count += 1
                _tee(f"\n  {_c('QA REJECTED — retrying pipeline with feedback...', _YELLOW, _BOLD)}\n")
                logger.info("QA rejected — retrying (attempt %d)", self._retry_count)
                fb_msg = qa_result.get("feedback", "Quality issues")
                blueprint          = self._run_ba_agent(question, context, qa_feedback=fb_msg)
                report_with_data   = self._run_sql_agent(question, blueprint, context)
                cleaned_report     = self._run_data_analyst_agent(report_with_data)
                final_report       = self._run_report_writer_agent(cleaned_report, context)

            # ── Post-processing ────────────────────────────────────────────
            final_report = self._post_process(final_report)
            applicable_filters = self._detect_applicable_filters(final_report)

            total_elapsed = time.time() - pipeline_start
            _pipeline_complete(total_elapsed, final_report)
            logger.info("Claude pipeline COMPLETE — %.1fs", total_elapsed)

            return {
                "mode": "report",
                "report": final_report,
                "applicable_filters": applicable_filters,
                "ui_instructions": {
                    "create_new_section": True,
                    "open_in_new_tab": True,
                    "enable_streaming": True,
                    "stream_once": True,
                    "include_report_ai": True,
                    "report_ai": {
                        "type": "chat_like",
                        "position": "below_report",
                    },
                    "explanation_feature": {
                        "enabled": True,
                        "trigger": "eye_button",
                    },
                },
            }

        except Exception as exc:
            total_elapsed = time.time() - pipeline_start
            _tee(f"\n  {_c(f'PIPELINE FAILED after {total_elapsed:.1f}s: {exc}', _RED, _BOLD)}\n")
            logger.error("Claude pipeline failed: %s", exc, exc_info=True)
            return {
                "mode": "report",
                "error": f"Report generation failed: {str(exc)}",
                "report": None,
            }

    # ═══════════════════════════════════════════════════════════════════════
    # AGENT RUNNERS
    # ═══════════════════════════════════════════════════════════════════════

    def _run_context_agent(self, question: str) -> dict:
        """Agent 1: Analyze the user's question and gather database context.
        Uses Haiku — simple entity extraction, no heavy reasoning needed.
        """
        response = self.client.call_agent(
            system_prompt=CONTEXT_AGENT_SYSTEM,
            user_message=(
                f"Analyze this report request and gather the necessary "
                f"database context:\n\n{question}"
            ),
            tools=CONTEXT_AGENT_TOOLS,
            tool_handlers=TOOL_HANDLERS,
            max_tokens=4096,
            agent_name="Context Agent",
            model=_HAIKU,
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Context agent JSON parse failed: %s", exc)
            return {
                "subject": question,
                "intent": "overview",
                "timeframe": "all time",
                "business_domain": "sales",
                "relevant_tables": ["sales_order", "sales_order_line"],
                "filters": {},
                "key_metrics_to_analyze": [],
            }

    def _run_ba_agent(
        self,
        question: str,
        context: dict,
        qa_feedback: str | None = None,
    ) -> dict:
        """Agent 2: Design the report blueprint (KPIs, charts, table)."""
        user_msg = (
            f"Design a comprehensive analytics report for this request.\n\n"
            f"USER QUESTION: {question}\n\n"
            f"CONTEXT ANALYSIS:\n{json.dumps(context, indent=2)}"
        )

        if qa_feedback:
            user_msg += (
                f"\n\nQA FEEDBACK FROM PREVIOUS ATTEMPT:\n{qa_feedback}\n"
                f"Please address these issues in the new blueprint."
            )

        response = self.client.call_agent(
            system_prompt=BUSINESS_ANALYST_SYSTEM,
            user_message=user_msg,
            tools=BA_AGENT_TOOLS,
            tool_handlers=TOOL_HANDLERS,
            max_tokens=8192,
            agent_name="Business Analyst Agent",
            model=_SONNET,   # Needs creativity for blueprint design
            use_cache=True,
        )

        try:
            blueprint = self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("BA agent JSON parse failed: %s", exc)
            raise ValueError(f"Failed to generate report blueprint: {exc}")

        if "kpis" not in blueprint:
            blueprint["kpis"] = []
        if "charts" not in blueprint:
            blueprint["charts"] = []
        if "title" not in blueprint:
            blueprint["title"] = f"Report: {context.get('subject', question[:50])}"

        return blueprint

    def _run_sql_agent(
        self,
        question: str,
        blueprint: dict,
        context: dict,
    ) -> dict:
        """Agent 3: Write and execute SQL for all KPIs and charts."""
        schema_str  = format_schema()
        rels_str    = format_relationships()
        profile_str = get_data_profile()

        system_prompt = get_sql_agent_system(schema_str, rels_str, profile_str)

        user_msg = (
            f"Execute SQL queries to populate this report blueprint with real data.\n\n"
            f"USER QUESTION: {question}\n\n"
            f"REPORT BLUEPRINT:\n{json.dumps(blueprint, indent=2)}\n\n"
            f"CONTEXT:\n{json.dumps(context, indent=2)}\n\n"
            f"For each KPI and chart, write a SQL query, execute it using "
            f"the execute_sql_query tool, and include the actual data in "
            f"the output. If a query fails, fix it and try again.\n\n"
            f"IMPORTANT:\n"
            f"- KPI queries must return exactly 1 row with 1 numeric value\n"
            f"- Chart queries must return rows with a label column and value column(s)\n"
            f"- Table query should return detailed rows (limit 20)\n"
            f"- Include the SQL used and actual data for each element"
        )

        response = self.client.call_agent(
            system_prompt=system_prompt,
            user_message=user_msg,
            tools=SQL_AGENT_TOOLS,
            tool_handlers=TOOL_HANDLERS,
            max_tokens=16384,
            max_tool_rounds=25,
            agent_name="SQL Agent",
            model=_SONNET,   # Needs strong reasoning for complex PostgreSQL
            use_cache=True,  # BIGGEST WIN: caches the entire schema block
        )

        try:
            report = self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("SQL agent JSON parse failed: %s", exc)
            report = blueprint.copy()
            for kpi in report.get("kpis", []):
                kpi.setdefault("value", 0)
                kpi.setdefault("sql", "")
            for chart in report.get("charts", []):
                chart.setdefault("data", [])
                chart.setdefault("sql", "")

        return report

    def _run_data_analyst_agent(self, report: dict) -> dict:
        """Agent 4: Validate and clean the data."""
        response = self.client.call_agent(
            system_prompt=DATA_ANALYST_SYSTEM,
            user_message=(
                f"Review and clean the following report data. "
                f"Check all KPI values and chart data for quality issues.\n\n"
                f"{json.dumps(report, indent=2, default=str)}"
            ),
            max_tokens=16384,
            agent_name="Data Analyst Agent",
            model=_HAIKU,    # Basic validation — no complex reasoning needed
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Data analyst JSON parse failed: %s — using uncleaned data", exc)
            return report

    def _run_report_writer_agent(self, report: dict, context: dict) -> dict:
        """Agent 5: Write the narrative components."""
        response = self.client.call_agent(
            system_prompt=REPORT_WRITER_SYSTEM,
            user_message=(
                f"Write the narrative components for this report. "
                f"Use the actual data values in your writing.\n\n"
                f"BUSINESS CONTEXT: {context.get('subject', 'General report')}, "
                f"domain: {context.get('business_domain', 'sales')}\n\n"
                f"REPORT DATA:\n{json.dumps(report, indent=2, default=str)}"
            ),
            max_tokens=16384,
            agent_name="Report Writer Agent",
            model=_SONNET,   # McKinsey-quality writing needs full Sonnet
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Report writer parse failed: %s", exc)
            for kpi in report.get("kpis", []):
                if "explanation" not in kpi:
                    kpi["explanation"] = {"what": kpi.get("label",""), "how": "", "why": "", "insight": ""}
            for chart in report.get("charts", []):
                if "explanation" not in chart:
                    chart["explanation"] = {"what": chart.get("title",""), "how": "", "why": "", "insight": ""}
            if "insights" not in report:
                report["insights"] = []
            return report

    def _run_qa_agent(self, question: str, report: dict) -> dict:
        """Agent 6: Quality assurance check."""
        response = self.client.call_agent(
            system_prompt=QA_AGENT_SYSTEM,
            user_message=(
                f"Evaluate the quality of this report against the user's "
                f"original question.\n\n"
                f"USER QUESTION: {question}\n\n"
                f"GENERATED REPORT:\n{json.dumps(report, indent=2, default=str)}"
            ),
            max_tokens=4096,
            agent_name="QA Agent",
            model=_HAIKU,    # JSON comparison — no heavy reasoning needed
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("QA agent parse failed: %s — auto-approving", exc)
            return {"approved": True, "score": 6, "max_score": 8, "feedback": "Auto-approved"}

    # ═══════════════════════════════════════════════════════════════════════
    # POST-PROCESSING
    # ═══════════════════════════════════════════════════════════════════════

    def _post_process(self, report: dict) -> dict:
        """Apply post-processing to clean up the report."""
        # ── KPI cleanup ───────────────────────────────────────────────
        if "kpis" in report:
            cleaned_kpis = []
            for kpi in report["kpis"]:
                val = kpi.get("value")
                if val is None or val == "" or val == "N/A":
                    kpi["value"] = 0
                elif isinstance(val, str):
                    try:
                        kpi["value"] = float(val.replace(",", "").replace("₹", "").strip())
                    except (ValueError, AttributeError):
                        kpi["value"] = 0

                kpi.setdefault("id", f"kpi_{len(cleaned_kpis) + 1}")
                kpi.setdefault("format", "number")
                kpi.setdefault("icon", "revenue")
                kpi.setdefault("color", "blue")
                kpi.setdefault("sql", "")
                kpi.setdefault("explanation", {"what": kpi.get("label",""), "how": "", "why": "", "insight": ""})
                cleaned_kpis.append(kpi)

            report["kpis"] = cleaned_kpis[:6]

        # ── Chart cleanup ─────────────────────────────────────────────
        if "charts" in report:
            valid_charts = []
            for chart in report["charts"]:
                chart.setdefault("id", f"chart_{len(valid_charts) + 1}")
                chart.setdefault("type", "bar")
                chart.setdefault("sql", "")
                chart.setdefault("x_label", "Category")
                chart.setdefault("y_label", "Value")
                chart.setdefault("color_scheme", "blues")
                chart.setdefault("explanation", {"what": chart.get("title",""), "how": "", "why": "", "insight": ""})

                data = chart.get("data", [])
                if not data or len(data) == 0:
                    chart["data"] = [{"label": "No data available", "value": 0}]
                    chart["type"] = "bar"
                else:
                    row_keys = list(data[0].keys())
                    if len(row_keys) < 2:
                        if len(row_keys) == 1 and len(data) == 1:
                            val_key = row_keys[0]
                            chart["data"] = [{"label": chart.get("title","Value"), val_key: data[0][val_key]}]
                        elif len(row_keys) == 1 and len(data) > 1:
                            val_key = row_keys[0]
                            for i, row in enumerate(data):
                                row["label"] = f"Item {i + 1}"

                    row_keys = list(chart["data"][0].keys()) if chart["data"] else []
                    if len(row_keys) >= 2:
                        value_keys = row_keys[1:]
                        non_zero = [
                            row for row in chart["data"]
                            if any(row.get(k) not in (None, 0, "", "0", 0.0) for k in value_keys)
                        ]
                        if non_zero:
                            chart["data"] = non_zero

                valid_charts.append(chart)

            report["charts"] = valid_charts[:6]
            report["charts"] = self._enforce_chart_diversity(report["charts"])

        # ── Table cleanup ─────────────────────────────────────────────
        if "table" in report:
            table = report["table"]
            if not isinstance(table, dict):
                report["table"] = {"title": "Detail Table", "sql": "", "data": []}
            else:
                table.setdefault("title", "Detail Table")
                table.setdefault("sql", "")
                table.setdefault("data", [])

        # ── Insights cleanup ──────────────────────────────────────────
        if "insights" in report:
            cleaned = []
            for ins in report["insights"]:
                if isinstance(ins, str):
                    ins = {"title": ins, "body": ins, "type": "neutral"}
                elif isinstance(ins, dict):
                    ins.setdefault("title", "Insight")
                    ins.setdefault("body", ins.get("title", ""))
                    ins.setdefault("type", "neutral")
                cleaned.append(ins)
            report["insights"] = cleaned[:8]

        return report

    @staticmethod
    def _enforce_chart_diversity(charts: list[dict]) -> list[dict]:
        """Ensure at least 4 different chart types across all charts."""
        if len(charts) <= 1:
            return charts

        type_count: dict[str, int] = {}
        for chart in charts:
            ct = chart.get("type", "bar").lower()
            type_count[ct] = type_count.get(ct, 0) + 1

        if len(type_count) >= 4:
            return charts

        all_types   = ["bar", "line", "pie", "doughnut", "horizontalBar", "area"]
        used_types  = set(type_count.keys())
        unused_types = [t for t in all_types if t not in used_types]

        seen_types: set[str] = set()
        for chart in charts:
            ct = chart.get("type", "bar").lower()
            if ct in seen_types and unused_types:
                new_type = unused_types.pop(0)
                chart["type"] = new_type
            seen_types.add(chart.get("type", "bar").lower())

        return charts

    @staticmethod
    def _detect_applicable_filters(report: dict) -> dict:
        """Analyze all SQL in the report to determine which filters apply."""
        all_sql = []
        for kpi in report.get("kpis", []):
            if kpi.get("sql"):
                all_sql.append(kpi["sql"])
        for chart in report.get("charts", []):
            if chart.get("sql"):
                all_sql.append(chart["sql"])
        if report.get("table", {}).get("sql"):
            all_sql.append(report["table"]["sql"])

        combined = " ".join(all_sql).lower()

        has_sales_order    = bool(re.search(r"\bsales_order\b(?!_)", combined))
        has_product_master = bool(re.search(r"\bproduct_master\b", combined))
        has_customer_master= bool(re.search(r"\bcustomer_master\b", combined))
        has_order_date     = bool(re.search(r"\border_date\b", combined))

        filters = {}
        if has_sales_order and has_order_date:
            filters["date_range"] = True
        if has_product_master:
            filters["category"] = True
            filters["product"]  = True
        if has_customer_master:
            filters["customer"] = True
        if has_sales_order:
            filters["status"] = True

        logger.info("Detected applicable filters: %s", filters)
        return filters
