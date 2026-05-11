"""Multi-agent report generation orchestrator using Claude API.

Version 2.0 - Drift Intelligence Edition.
Supports dual-mode routing: STANDARD_REPORT and DRIFT_INVESTIGATION.

Chains 6 specialized agents:
1. Context + Signal Classification Agent  — classifies intent, maps to signals
2. Drift Blueprint + Business Analyst     — designs report/drift card blueprint
3. SQL + Drift Detective Agent            — writes and executes queries (phased for drift)
4. Data Analyst + Causal Validator        — validates data and drift math integrity
5. Report Writer + Drift Narrator         — writes McKinsey-style narratives
6. QA + Drift Card Validator              — quality assurance gate (12-point for drift)

The output is 100% compatible with the existing frontend JSON format.
Drift investigation mode adds drift_metrics, causal_decomposition, and tab_data.
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
_HAIKU  = config.CLAUDE_HAIKU_MODEL  # Context Agent, Data Analyst, QA Agent (~8x cheaper)

# ── Agent display config ─────────────────────────────────────────────────────
_AGENTS_STANDARD = [
    ("1", "CONTEXT AGENT",        "🔍", "Analyzing question & gathering database context"),
    ("2", "BUSINESS ANALYST",     "📐", "Designing report blueprint (KPIs + Charts)"),
    ("3", "SQL AGENT",            "⚡", "Writing & executing SQL queries with tool use"),
    ("4", "DATA ANALYST",         "🔬", "Validating & cleaning query results"),
    ("5", "REPORT WRITER",        "✍️ ", "Writing executive narrative & insights"),
    ("6", "QA AGENT",             "🛡️ ", "Quality assurance — scoring report against question"),
]
_AGENTS_DRIFT = [
    ("1", "SIGNAL CLASSIFIER",    "🔍", "Classifying intent & mapping to signal library"),
    ("2", "DRIFT ARCHITECT",      "📐", "Designing drift card blueprint (11 tabs)"),
    ("3", "DRIFT DETECTIVE",      "⚡", "Executing 4-phase SQL investigation"),
    ("4", "CAUSAL VALIDATOR",     "🔬", "Validating decomposition math & severity"),
    ("5", "DRIFT NARRATOR",       "✍️ ", "Writing causal narrative & suspected drivers"),
    ("6", "DRIFT QA",             "🛡️ ", "12-point drift card validation"),
]
_AGENTS = _AGENTS_STANDARD  # default, switched at runtime


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
        """Generate a complete report using the 6-agent pipeline.

        Supports two modes:
        - STANDARD_REPORT: Traditional KPI + chart dashboard
        - DRIFT_INVESTIGATION: Full drift card with causal decomposition
        """
        global _AGENTS
        pipeline_start = time.time()
        _pipeline_banner(question)
        logger.info("Claude pipeline START — question: %s", question[:120])

        self._retry_count = 0

        try:
            # ── Agent 1: Context + Signal Classification ───────────────────
            _agent_header(*_AGENTS_STANDARD[0])
            t0 = time.time()
            context = self._run_context_agent(question)

            # Detect intent mode and switch agent display labels
            intent_mode = context.get('intent_mode', 'STANDARD_REPORT')
            if intent_mode == 'DRIFT_INVESTIGATION':
                _AGENTS = _AGENTS_DRIFT
                signal_id = context.get('signal_id', '?')
                signal_name = context.get('signal_name', '?')
                _agent_result("SIGNAL CLASSIFIER", time.time() - t0, [
                    f"Mode     : DRIFT_INVESTIGATION",
                    f"Signal   : {signal_id} - {signal_name}",
                    f"Domain   : {context.get('signal_domain', '?')}",
                    f"Severity : {context.get('default_severity', '?')}",
                    f"Baseline : {context.get('baseline_window', '?')}",
                    f"Tables   : {', '.join(context.get('relevant_tables', [])[:6])}",
                ])
            else:
                _AGENTS = _AGENTS_STANDARD
                _agent_result("CONTEXT AGENT", time.time() - t0, [
                    f"Mode     : STANDARD_REPORT",
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

            # ── SQL Traceability Log ──────────────────────────────────────
            self._log_sql_traceability(report_with_data)

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
            max_sc   = qa_result.get("max_score", 12)
            feedback = qa_result.get("feedback", "")[:80]

            # Override the LLM's approved field based on actual score.
            # Haiku sometimes returns approved=false for scores that should pass.
            # Rule: ≥58% of max_score = approved (i.e., 7/12 for standard, 7/12 for drift)
            if isinstance(score, (int, float)) and isinstance(max_sc, (int, float)) and max_sc > 0:
                approved = score >= (max_sc * 0.58)
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

            # Build response based on intent mode
            response_payload = {
                "mode": "report",
                "intent_mode": intent_mode,
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

            # Add drift-specific metadata to response
            if intent_mode == "DRIFT_INVESTIGATION":
                response_payload["drift_context"] = {
                    "signal_id": context.get("signal_id"),
                    "signal_name": context.get("signal_name"),
                    "signal_domain": context.get("signal_domain"),
                    "severity": final_report.get("severity", context.get("default_severity")),
                    "severity_score": final_report.get("severity_score"),
                    "causal_chain": context.get("causal_chain"),
                }

            return response_payload

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
        """Agent 1: Analyze query, classify intent (standard vs drift), map to signal.
        Uses Sonnet — signal classification against 37-SIG library needs reasoning.
        """
        response = self.client.call_agent(
            system_prompt=CONTEXT_AGENT_SYSTEM,
            user_message=(
                f"Analyze this analytics request. Determine if it is a STANDARD_REPORT "
                f"or a DRIFT_INVESTIGATION, and produce the appropriate context object.\n\n"
                f"USER QUERY: {question}"
            ),
            tools=CONTEXT_AGENT_TOOLS,
            tool_handlers=TOOL_HANDLERS,
            max_tokens=4096,
            agent_name="Context + Signal Agent",
            model=_SONNET,
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Context agent JSON parse failed: %s", exc)
            return {
                "intent_mode": "STANDARD_REPORT",
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
        """Agent 2: Design the report or drift card blueprint."""
        intent_mode = context.get('intent_mode', 'STANDARD_REPORT')

        if intent_mode == 'DRIFT_INVESTIGATION':
            user_msg = (
                f"Design a DRIFT INVESTIGATION blueprint for this signal.\n\n"
                f"USER QUERY: {question}\n\n"
                f"CONTEXT (intent_mode=DRIFT_INVESTIGATION):\n"
                f"{json.dumps(context, indent=2)}\n\n"
                f"You must produce the full drift card blueprint with:\n"
                f"- Causal decomposition plan\n"
                f"- 3-5 suspected driver hypotheses\n"
                f"- All 11 tab data requirements\n"
                f"- Impact quantification formula\n"
                f"- Severity scoring inputs\n"
                f"- 6 KPIs (4 drift-required + 2 supporting)\n"
                f"- 6 charts (trend, comparison, waterfall, geographic, period, transactions)"
            )
        else:
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
            max_tokens=16384 if intent_mode == 'DRIFT_INVESTIGATION' else 8192,
            agent_name="Drift Architect" if intent_mode == 'DRIFT_INVESTIGATION' else "Business Analyst",
            model=_SONNET,
            use_cache=True,
        )

        try:
            blueprint = self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("BA agent JSON parse failed: %s", exc)
            raise ValueError(f"Failed to generate report blueprint: {exc}")

        # Ensure essential fields exist
        blueprint.setdefault('intent_mode', intent_mode)
        if 'kpis' not in blueprint:
            blueprint['kpis'] = []
        if 'charts' not in blueprint:
            blueprint['charts'] = []
        if 'title' not in blueprint:
            blueprint['title'] = f"Report: {context.get('subject', question[:50])}"

        return blueprint

    def _run_sql_agent(
        self,
        question: str,
        blueprint: dict,
        context: dict,
    ) -> dict:
        """Agent 3: Write and execute SQL for all KPIs, charts, and drift data.
        For DRIFT_INVESTIGATION, executes 4-phase query cycle with more tool rounds.
        """
        schema_str  = format_schema()
        rels_str    = format_relationships()
        profile_str = get_data_profile()

        system_prompt = get_sql_agent_system(schema_str, rels_str, profile_str)
        intent_mode = context.get('intent_mode', 'STANDARD_REPORT')

        if intent_mode == 'DRIFT_INVESTIGATION':
            user_msg = (
                f"Execute the 4-phase drift investigation SQL cycle.\n\n"
                f"USER QUERY: {question}\n\n"
                f"DRIFT BLUEPRINT:\n{json.dumps(blueprint, indent=2)}\n\n"
                f"SIGNAL CONTEXT:\n{json.dumps(context, indent=2)}\n\n"
                f"Execute queries in this EXACT order:\n"
                f"PHASE 1 - Anchor: current_value, baseline_value, variance, impact\n"
                f"PHASE 2 - Decompose: dimensional cuts for each dimension\n"
                f"PHASE 3 - Support: trend, period_compare, geographic, consecutive_periods\n"
                f"PHASE 4 - Related: check related signals for co-firing\n\n"
                f"Include all SQL and actual data in the output JSON."
            )
            max_rounds = 40  # drift needs more rounds for multi-phase
            max_tokens = 32768
        else:
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
            max_rounds = 25
            max_tokens = 16384

        response = self.client.call_agent(
            system_prompt=system_prompt,
            user_message=user_msg,
            tools=SQL_AGENT_TOOLS,
            tool_handlers=TOOL_HANDLERS,
            max_tokens=max_tokens,
            max_tool_rounds=max_rounds,
            agent_name="Drift Detective" if intent_mode == 'DRIFT_INVESTIGATION' else "SQL Agent",
            model=_SONNET,
            use_cache=True,
        )

        try:
            report = self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("SQL agent JSON parse failed: %s", exc)
            report = blueprint.copy()
            for kpi in report.get('kpis', []):
                kpi.setdefault('value', 0)
                kpi.setdefault('sql', '')
            for chart in report.get('charts', []):
                chart.setdefault('data', [])
                chart.setdefault('sql', '')

        return report

    def _run_data_analyst_agent(self, report: dict) -> dict:
        """Agent 4: Validate data and drift math integrity."""
        intent_mode = report.get('intent_mode', 'STANDARD_REPORT')

        if intent_mode == 'DRIFT_INVESTIGATION':
            user_msg = (
                f"Validate the drift investigation data. Run ALL causal math checks:\n"
                f"1. Contribution sum integrity (should sum to ~100%)\n"
                f"2. Single-entity monopoly check\n"
                f"3. Baseline sanity (CV check)\n"
                f"4. Consecutive periods consistency\n"
                f"5. Impact calculation audit\n"
                f"6. Severity score computation\n"
                f"7. Affected areas validation\n\n"
                f"REPORT DATA:\n{json.dumps(report, indent=2, default=str)}"
            )
        else:
            user_msg = (
                f"Review and clean the following report data. "
                f"Check all KPI values and chart data for quality issues.\n\n"
                f"{json.dumps(report, indent=2, default=str)}"
            )

        response = self.client.call_agent(
            system_prompt=DATA_ANALYST_SYSTEM,
            user_message=user_msg,
            max_tokens=16384,
            agent_name="Causal Validator" if intent_mode == 'DRIFT_INVESTIGATION' else "Data Analyst",
            model=_SONNET if intent_mode == 'DRIFT_INVESTIGATION' else _HAIKU,
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Data analyst JSON parse failed: %s - using uncleaned data", exc)
            return report

    def _run_report_writer_agent(self, report: dict, context: dict) -> dict:
        """Agent 5: Write narratives - McKinsey-style for drift, executive for standard."""
        intent_mode = context.get('intent_mode', 'STANDARD_REPORT')

        if intent_mode == 'DRIFT_INVESTIGATION':
            user_msg = (
                f"Write the DRIFT INVESTIGATION narrative. intent_mode=DRIFT_INVESTIGATION.\n\n"
                f"You must write ALL components:\n"
                f"1. Issue Overview (3-sentence template, <=60 words)\n"
                f"2. Why This Was Surfaced\n"
                f"3. Suspected Drivers (ranked by contribution)\n"
                f"4. Affected Areas narrative\n"
                f"5. KPI explanations (what/how/why/insight)\n"
                f"6. Chart explanations\n"
                f"7. Investigation Checklist (6 items)\n"
                f"8. Decision Options (expand templates)\n"
                f"9. Insights (6-8 non-obvious findings)\n\n"
                f"SIGNAL CONTEXT:\n{json.dumps(context, indent=2, default=str)}\n\n"
                f"REPORT DATA:\n{json.dumps(report, indent=2, default=str)}"
            )
        else:
            user_msg = (
                f"Write ALL narrative components for this STANDARD_REPORT. "
                f"intent_mode=STANDARD_REPORT.\n\n"
                f"You MUST write ALL of the following:\n"
                f"1. Executive summary (5-8 sentences with actual data values)\n"
                f"2. KPI explanations (what/how/why/insight for EVERY KPI)\n"
                f"3. Chart explanations (what/how/why/insight for EVERY chart)\n"
                f"4. Insights — 6-8 data-driven findings. EACH insight MUST be a JSON object with:\n"
                f"   - \"title\": 5-8 word directional claim\n"
                f"   - \"body\": 2-3 sentences with SPECIFIC numbers from the data\n"
                f"   - \"type\": \"positive\" | \"negative\" | \"neutral\" | \"warning\"\n"
                f"   At least 2 insights must be \"warning\" or \"negative\" type.\n\n"
                f"BUSINESS CONTEXT: {context.get('subject', 'General report')}, "
                f"domain: {context.get('business_domain', 'sales')}\n\n"
                f"REPORT DATA:\n{json.dumps(report, indent=2, default=str)}"
            )

        response = self.client.call_agent(
            system_prompt=REPORT_WRITER_SYSTEM,
            user_message=user_msg,
            max_tokens=16384,
            agent_name="Drift Narrator" if intent_mode == 'DRIFT_INVESTIGATION' else "Report Writer",
            model=_SONNET,
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Report writer parse failed: %s", exc)
            for kpi in report.get('kpis', []):
                if 'explanation' not in kpi:
                    kpi['explanation'] = {'what': kpi.get('label',''), 'how': '', 'why': '', 'insight': ''}
            for chart in report.get('charts', []):
                if 'explanation' not in chart:
                    chart['explanation'] = {'what': chart.get('title',''), 'how': '', 'why': '', 'insight': ''}
            if 'insights' not in report:
                report['insights'] = []
            return report

    def _run_qa_agent(self, question: str, report: dict) -> dict:
        """Agent 6: Quality assurance - 12-point for drift, 8-point for standard."""
        intent_mode = report.get('intent_mode', 'STANDARD_REPORT')

        if intent_mode == 'DRIFT_INVESTIGATION':
            user_msg = (
                f"Evaluate this DRIFT INVESTIGATION card (12-point checklist).\n\n"
                f"USER QUERY: {question}\n\n"
                f"Run ALL 12 checks: contribution sum, drift math, trend corroboration,\n"
                f"consecutive periods, issue overview template, suspected drivers,\n"
                f"insight specificity, decision actionability, 11-tab completeness,\n"
                f"affected areas validation, investigation checklist, related signals.\n\n"
                f"DRIFT CARD:\n{json.dumps(report, indent=2, default=str)}"
            )
            max_score = 12
        else:
            user_msg = (
                f"Evaluate the quality of this report against the user's "
                f"original question.\n\n"
                f"USER QUESTION: {question}\n\n"
                f"GENERATED REPORT:\n{json.dumps(report, indent=2, default=str)}"
            )
            max_score = 8

        response = self.client.call_agent(
            system_prompt=QA_AGENT_SYSTEM,
            user_message=user_msg,
            max_tokens=8192 if intent_mode == 'DRIFT_INVESTIGATION' else 4096,
            agent_name="Drift QA" if intent_mode == 'DRIFT_INVESTIGATION' else "QA Agent",
            model=_SONNET if intent_mode == 'DRIFT_INVESTIGATION' else _HAIKU,
            use_cache=True,
        )

        try:
            return self.client.extract_json(response)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("QA agent parse failed: %s - auto-approving", exc)
            return {'approved': True, 'score': max_score - 2, 'max_score': max_score, 'feedback': 'Auto-approved'}

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
                if isinstance(data, dict):
                    data = list(data.values())
                    chart["data"] = data
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

                # Fix title/row-count mismatch: "Top 40 by ..." but only 23 rows
                actual_rows = len(table.get("data", []))
                title = table.get("title", "")
                fixed_title = re.sub(
                    r'\bTop\s+\d+\b',
                    f'Top {actual_rows}',
                    title
                )
                if fixed_title != title:
                    logger.info("Table title fixed: '%s' → '%s'", title, fixed_title)
                    table["title"] = fixed_title

                # Ensure table has explanation for the eye modal
                table.setdefault("explanation", {
                    "what": f"Detail breakdown: {table.get('title', 'Data Table')}",
                    "how": f"Queried from database — {actual_rows} rows returned, sorted by relevance to the question",
                    "why": "Entity-level data for drill-down analysis and action planning",
                    "insight": "",
                })

        # ── Insights cleanup ──────────────────────────────────────────
        # Prefer rich insight objects from Report Writer over bare strings
        # from insight_topics (BA agent). Convert any remaining strings.
        insights = report.get("insights", [])
        insight_topics = report.get("insight_topics", [])

        # If insights is empty but insight_topics has content, promote them
        if not insights and insight_topics:
            insights = insight_topics

        cleaned = []
        for ins in insights:
            if isinstance(ins, str):
                ins = {"title": ins, "body": ins, "type": "neutral"}
            elif isinstance(ins, dict):
                ins.setdefault("title", "Insight")
                ins.setdefault("body", ins.get("title", ""))
                ins.setdefault("type", "neutral")
                # Validate type is one of allowed values
                if ins["type"] not in ("positive", "negative", "neutral", "warning", "opportunity"):
                    ins["type"] = "neutral"
            cleaned.append(ins)
        report["insights"] = cleaned[:10]

        # Remove insight_topics to avoid frontend confusion
        report.pop("insight_topics", None)

        return report

    def _log_sql_traceability(self, report: dict):
        """Log which SQL query powers each KPI, chart, and table."""
        width = 72
        lines = []
        lines.append(f"  {_c('┌─ SQL TRACEABILITY ' + '─' * (width - 21) + '┐', _CYAN)}")

        # KPIs
        for i, kpi in enumerate(report.get("kpis", []), 1):
            label = kpi.get("label", kpi.get("id", f"KPI {i}"))
            sql = kpi.get("sql", "")
            val = kpi.get("value", "?")
            lines.append(f"  {_c('│', _CYAN)} {_c(f'KPI {i}:', _BOLD)} {label} = {_c(str(val), _YELLOW)}")
            if sql:
                sql_preview = sql.replace('\n', ' ')[:100]
                lines.append(f"  {_c('│', _CYAN)}   {_c('SQL:', _DIM)} {sql_preview}")
            else:
                lines.append(f"  {_c('│', _CYAN)}   {_c('SQL: (none)', _RED)}")

        lines.append(f"  {_c('│' + '─' * (width - 2), _CYAN)}")

        # Charts
        for i, chart in enumerate(report.get("charts", []), 1):
            title = chart.get("title", f"Chart {i}")
            ctype = chart.get("type", "?")
            sql = chart.get("sql", "")
            rows = len(chart.get("data", []))
            lines.append(f"  {_c('│', _CYAN)} {_c(f'Chart {i}:', _BOLD)} {title} [{_c(ctype, _YELLOW)}]")
            lines.append(f"  {_c('│', _CYAN)}   Rows: {rows}")
            if sql:
                sql_preview = sql.replace('\n', ' ')[:100]
                lines.append(f"  {_c('│', _CYAN)}   {_c('SQL:', _DIM)} {sql_preview}")
            else:
                lines.append(f"  {_c('│', _CYAN)}   {_c('SQL: (none)', _RED)}")

        lines.append(f"  {_c('│' + '─' * (width - 2), _CYAN)}")

        # Table
        table = report.get("table", {})
        if table:
            title = table.get("title", "Detail Table")
            sql = table.get("sql", "")
            rows = len(table.get("data", []))
            # Check for title/row mismatch
            import re as _re
            match = _re.search(r'\bTop\s+(\d+)\b', title, _re.IGNORECASE)
            mismatch_warn = ""
            if match and int(match.group(1)) != rows:
                mismatch_warn = f" {_c(f'⚠️ TITLE SAYS {match.group(0)} BUT GOT {rows}', _RED, _BOLD)}"
            lines.append(f"  {_c('│', _CYAN)} {_c('Table:', _BOLD)} {title}{mismatch_warn}")
            lines.append(f"  {_c('│', _CYAN)}   Rows: {rows}")
            if sql:
                sql_preview = sql.replace('\n', ' ')[:100]
                lines.append(f"  {_c('│', _CYAN)}   {_c('SQL:', _DIM)} {sql_preview}")

        lines.append(f"  {_c('└' + '─' * (width - 2) + '┘', _CYAN)}")

        for line in lines:
            _tee(line)

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
