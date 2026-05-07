# Multi-Agent Report Pipeline — Claude API Implementation Plan

> **Status**: Approved by user. Ready for execution.
> **Scope**: Report generation only. Chat, modifications, filters, and UI remain untouched.

---

## 1. Current Architecture (What We Have)

The current report pipeline in `ai/report_generator.py` works like this:

```
User Question
    ↓
Intent Classification (deterministic regex)
    ↓
Blueprint Cache Check (MD5 hash)
    ↓  (cache miss)
Subject Lock Extraction → Pre-Analysis (AnalyzeAndPlan) → Analytical Framework (keyword matching)
    ↓
Single LLM Call (Groq/Llama via DSPy) → Massive prompt (~6000 tokens) containing:
  - Schema, relationships, data profile
  - Subject lock, SQL guide, analytical framework
  - ALL rules for KPIs, charts, SQL patterns, banned types, etc.
    ↓
JSON Blueprint (6 KPIs + 6 charts + table + insights)
    ↓
Sequential SQL Execution (one by one, with _fix_report_sql + repair loop)
    ↓
Post-processing (_clean_kpis, _validate_chart_data, _enforce_chart_diversity, etc.)
    ↓
Final Report
```

### Problems with the current approach:
1. **Single massive prompt** — One LLM call does everything: understand context, plan KPIs, choose charts, write 12+ SQL queries, and generate insights. This overwhelms the model.
2. **Hardcoded analytical frameworks** — 400+ lines of hardcoded keyword→metric mappings in `_get_analytical_framework()` to compensate for the model's inability to independently pick relevant KPIs.
3. **Massive SQL auto-correction** — 600+ lines of regex in `_fix_report_sql()` to fix the model's constant SQL mistakes (wrong table aliases, missing JOINs, wrong columns).
4. **Bad KPIs** — Despite 200+ lines of KPI cleanup code (`_clean_kpis`, `_BAD_KPI_RE`, `_fix_kpi_sql`), KPIs still come back as chart-type metrics or with wrong values.
5. **Bad chart axes** — The model sometimes puts numeric data on the label axis and text on the value axis.
6. **No quality gate** — If the LLM produces garbage, it goes straight to the user after cosmetic cleanup.

---

## 2. New Architecture (What We're Building)

```
User Question
    ↓
Intent Classification (unchanged — deterministic regex)
    ↓
Blueprint Cache Check (unchanged — MD5 hash)
    ↓  (cache miss)
╔══════════════════════════════════════════════════════════════════╗
║                  CLAUDE MULTI-AGENT PIPELINE                    ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  AGENT 1: Context Agent                                          ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  User question                               │        ║
║  │ Skills: get_db_schema, get_data_profile              │        ║
║  │ Output: Enriched context JSON                        │        ║
║  │         {subject, intent, timeframe, tables,         │        ║
║  │          filters, business_domain}                   │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║  AGENT 2: Business Analyst Agent                                 ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  Enriched context from Agent 1                │        ║
║  │ Skills: get_db_schema, get_data_profile              │        ║
║  │ Output: Report blueprint JSON                        │        ║
║  │         {title, summary, kpis[6], charts[6],         │        ║
║  │          table, insights[6-8]}                       │        ║
║  │         Each KPI/chart has: label, description,      │        ║
║  │         data_requirement (natural language),          │        ║
║  │         chart_type, x_axis, y_axis                   │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║  AGENT 3: SQL Agent                                              ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  Blueprint + schema + relationships           │        ║
║  │ Skills: execute_sql (tool — runs query on DB),       │        ║
║  │         validate_sql (tool — checks safety)          │        ║
║  │ Loop:   For each KPI/chart data_requirement:         │        ║
║  │         1. Write SQL                                 │        ║
║  │         2. Call execute_sql tool                      │        ║
║  │         3. If error → read error → rewrite SQL       │        ║
║  │         4. Return results                            │        ║
║  │ Output: Blueprint with populated SQL + raw data      │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║  AGENT 4: Data Analyst Agent                                     ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  Blueprint with raw data                      │        ║
║  │ Output: Cleaned, validated data                      │        ║
║  │         - Ensures X-axis is categorical (names/dates)│        ║
║  │         - Ensures Y-axis is numeric (amounts/counts) │        ║
║  │         - Removes zero/null rows                     │        ║
║  │         - Pivots multi-column results                │        ║
║  │         - Validates KPIs are scalar values            │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║  AGENT 5: Report Writer Agent                                    ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  Validated blueprint + data                   │        ║
║  │ Output: Final report JSON with:                      │        ║
║  │         - Executive summary with ACTUAL values       │        ║
║  │         - KPI explanations (what/how/why/insight)     │        ║
║  │         - Chart explanations                          │        ║
║  │         - 6-8 data-driven insights                   │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║  AGENT 6: QA Agent (Quality Assurance)                           ║
║  ┌─────────────────────────────────────────────────────┐        ║
║  │ Input:  Original user question + final report        │        ║
║  │ Output: APPROVED or REJECTED with feedback           │        ║
║  │ Checks:                                              │        ║
║  │  ✓ Does the report answer the user's question?       │        ║
║  │  ✓ Are KPIs relevant to the subject?                 │        ║
║  │  ✓ Are chart types appropriate for the data?         │        ║
║  │  ✓ Do axes make sense (labels vs values)?            │        ║
║  │  ✓ Are there at least 4 different chart types?       │        ║
║  │  ✓ Are KPI values meaningful (not all zero/N/A)?     │        ║
║  └──────────────────────┬──────────────────────────────┘        ║
║                         ↓                                        ║
║              (If rejected → loop back to Agent 2)                ║
║              (Max 1 retry)                                       ║
╚══════════════════════════════════════════════════════════════════╝
    ↓
Post-processing (reuse existing: _fix_report_sql, _clean_kpis, chart diversity)
    ↓
Cache Blueprint
    ↓
Final Report → Frontend (same JSON structure as current)
```

---

## 3. File-by-File Implementation Plan

### 3.1 Configuration

#### [MODIFY] `config.py`
Add Anthropic API key and model configuration:
```python
# ── Anthropic (Claude) ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
```

#### [MODIFY] `.env`
Add:
```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
CLAUDE_MODEL=claude-sonnet-4-20250514
```

---

### 3.2 Claude Client Setup

#### [NEW] `ai/claude_client.py`
A thin wrapper around the `anthropic` SDK that handles:
- Initializing the Anthropic client with the API key
- A reusable `call_agent()` function that takes:
  - `system_prompt` (defines the agent's role)
  - `user_message` (the task)
  - `tools` (list of Claude Tool schemas — the "Skills")
  - `max_tokens`
- A tool execution loop that:
  1. Sends the message to Claude
  2. If Claude responds with `tool_use` blocks, executes the corresponding Python functions
  3. Returns the tool results back to Claude
  4. Repeats until Claude responds with a final `text` block
- JSON extraction helper (reuse `_extract_json` and `_repair_json` from `report_generator.py`)

**Key design decision**: Each "Agent" is NOT a separate class. It is a single function call to `call_agent()` with a different `system_prompt` and `tools` list. This keeps the architecture simple and debuggable.

---

### 3.3 Tool Definitions (Claude Skills)

#### [NEW] `ai/claude_tools.py`
Define the Python functions that Claude can call as tools:

| Tool Name | Used By | Description |
|-----------|---------|-------------|
| `get_db_schema` | Context, BA, SQL Agents | Returns `format_schema()` output |
| `get_relationships` | Context, BA, SQL Agents | Returns `format_relationships()` output |
| `get_data_profile` | Context, BA Agents | Returns `get_data_profile()` output |
| `execute_sql_query` | SQL Agent | Wraps `_fix_report_sql()` → `validate_sql()` → `execute_sql()`. Returns data or error message. |
| `validate_sql_query` | SQL Agent | Runs `check_sql_against_schema()` + `check_sql_patterns()`. Returns issues or "valid". |

Each tool is defined as:
1. A **JSON schema** (for Claude's tool definition format)
2. A **Python handler function** (that actually runs the tool)

---

### 3.4 Agent System Prompts

#### [NEW] `ai/claude_prompts.py`
Contains the system prompts for each agent. These replace the massive 400-line DSPy signature.

**Agent 1 — Context Agent** (~200 tokens):
```
You are a Context Analysis Agent. Given a user's natural language question about
a business database, your job is to:
1. Identify the core SUBJECT (e.g., "gold products", "customer spending", "vendor performance")
2. Determine the TIME FRAME (this year, last month, all time, etc.)
3. Classify the BUSINESS DOMAIN (sales, inventory, procurement, customer, product, material)
4. Identify which database tables and columns are relevant
5. Extract any implicit filters (e.g., "closed orders" for revenue queries)

Use the get_db_schema and get_data_profile tools to understand the database.
Output a JSON object with: subject, intent, timeframe, relevant_tables, filters, business_domain.
```

**Agent 2 — Business Analyst Agent** (~300 tokens):
```
You are a Business Analyst Agent. Given a structured context about a user's report request,
design a comprehensive analytics report blueprint.

You MUST design:
- 6 KPIs (each must be a single scalar value — NOT a list, NOT a trend)
- 6 Charts (at least 4 different chart types, each exploring a different data dimension)
- 1 Detail table
- 6-8 Insights

For each KPI: provide label, format (currency/number/percent), and a natural language
description of what data to fetch (e.g., "Sum of all closed order amounts").
For each chart: provide title, chart_type, x_axis description, y_axis description,
and a natural language description of what data to fetch.

DO NOT write SQL. Only describe what data is needed in plain English.
```

**Agent 3 — SQL Agent** (~300 tokens + schema injected):
```
You are a SQL Agent. You write and execute PostgreSQL queries against a business database.

For each data requirement in the blueprint, write a SQL query and execute it using
the execute_sql_query tool. If a query fails, read the error message and rewrite the query.

RULES:
- Use ONLY columns that exist in the schema (use validate_sql_query to check)
- Follow proper JOIN chains (provided in schema)
- KPI queries must return exactly 1 row with 1 value
- Chart queries must return 2+ columns (label + value) with multiple rows
- Use TO_CHAR for date labels, never raw timestamps
- Always JOIN to master tables for human-readable names (never raw IDs)
```
*(Schema string, relationships, and data profile are injected into the system prompt)*

**Agent 4 — Data Analyst Agent** (~200 tokens):
```
You are a Data Analyst Agent. Review the raw data returned from SQL queries and
ensure it is properly formatted for chart rendering:
1. Verify X-axis data is categorical (text/dates) and Y-axis is numeric
2. Remove rows where all values are zero or null
3. Verify KPI values are meaningful scalars (not lists, not "N/A")
4. Flag any data quality issues
Output the cleaned data in the same JSON structure.
```

**Agent 5 — Report Writer Agent** (~200 tokens):
```
You are a Report Writer Agent. Given a complete report with real data values,
write the narrative components:
1. Executive summary (5-8 sentences referencing ACTUAL data values)
2. KPI explanations (what/how/why/insight for each)
3. Chart explanations (what/how/why/insight for each)
4. 6-8 data-driven insights (with specific numbers, %, comparisons)
Think like a McKinsey analyst presenting to C-suite executives.
```

**Agent 6 — QA Agent** (~150 tokens):
```
You are a Quality Assurance Agent. Compare the user's original question against
the generated report and verify:
1. Does every KPI directly relate to the user's subject?
2. Are chart types appropriate (not all the same, at least 4 different)?
3. Are axis labels sensible (categorical on labels, numeric on values)?
4. Are KPI values meaningful (not all zero, not all N/A)?
5. Does the report actually answer what the user asked?
Output: { "approved": true/false, "feedback": "..." }
```

---

### 3.5 Multi-Agent Orchestrator

#### [NEW] `ai/claude_multi_agent.py`
The main orchestration file that chains the 6 agents together:

```python
class ClaudeReportPipeline:
    """Multi-agent report generation using Claude API."""

    def __init__(self):
        self.client = ClaudeClient()  # from claude_client.py

    def generate(self, question: str, force_refresh: bool = False) -> dict:
        """Generate a complete report using the 6-agent pipeline."""

        # Step 1: Context Agent
        context = self._run_context_agent(question)

        # Step 2: Business Analyst Agent
        blueprint = self._run_ba_agent(context)

        # Step 3: SQL Agent (with tool use loop)
        blueprint_with_data = self._run_sql_agent(blueprint, context)

        # Step 4: Data Analyst Agent
        cleaned = self._run_data_analyst_agent(blueprint_with_data)

        # Step 5: Report Writer Agent
        report = self._run_report_writer_agent(cleaned, context)

        # Step 6: QA Agent
        qa_result = self._run_qa_agent(question, report)
        if not qa_result["approved"] and self._retry_count < 1:
            self._retry_count += 1
            # Feed QA feedback back to BA agent and retry
            blueprint = self._run_ba_agent(context, qa_feedback=qa_result["feedback"])
            # ... repeat steps 3-6

        # Post-processing (reuse existing Python code)
        report = self._post_process(report)

        return {
            "mode": "report",
            "report": report,
            "applicable_filters": self._detect_applicable_filters(report),
            "ui_instructions": { ... }  # same as current
        }
```

**Parallel SQL Execution**: When the SQL Agent returns a list of queries, we execute them in parallel using `asyncio.gather()` against PostgreSQL (NOT parallel API calls to Claude). This is pure DB parallelism.

---

### 3.6 Integration Points

#### [MODIFY] `app.py`
Add a new endpoint OR modify the existing `/report` endpoint to detect provider:

```python
@app.post("/report")
def report_endpoint(req: ReportRequest):
    if req.provider == "claude":
        from ai.claude_multi_agent import ClaudeReportPipeline
        pipeline = ClaudeReportPipeline()
        return pipeline.generate(req.question, force_refresh=req.force_refresh)
    else:
        # Existing DSPy pipeline (unchanged)
        from ai.report_generator import ReportPipeline
        pipeline = ReportPipeline(provider=req.provider)
        return pipeline.generate(question_with_filters, force_refresh=req.force_refresh)
```

#### [UNCHANGED] Files that remain completely untouched:
- `ai/pipeline.py` — Chat pipeline (DSPy/Groq)
- `ai/signatures.py` — DSPy signatures for chat
- `ai/report_signatures.py` — DSPy signatures for reports (kept for legacy)
- `ai/validator.py` — SQL safety validation (reused by Claude pipeline)
- `ai/sql_pattern_checker.py` — Structural pattern checks (reused)
- `db/*` — All database modules (schema, executor, profiler, relationships, memory)
- `frontend/*` — All frontend files
- Report modification endpoint (`/report/modify`)
- Report filter endpoint (`/report/apply-filters`)
- All chat endpoints (`/chat`, `/chat/stream`)

---

## 4. Output Format Compatibility

The Claude pipeline will output the **exact same JSON structure** as the current pipeline:

```json
{
  "mode": "report",
  "report": {
    "title": "...",
    "summary": "...",
    "kpis": [
      {
        "id": "kpi_1",
        "label": "...",
        "sql": "...",
        "value": 12345,
        "format": "currency",
        "icon": "revenue",
        "color": "blue",
        "explanation": { "what": "...", "how": "...", "why": "...", "insight": "..." }
      }
    ],
    "charts": [
      {
        "id": "chart_1",
        "title": "...",
        "type": "bar",
        "sql": "...",
        "data": [ {"label": "...", "value": 123} ],
        "x_label": "...",
        "y_label": "...",
        "color_scheme": "blues",
        "explanation": { ... }
      }
    ],
    "table": { "title": "...", "sql": "...", "data": [...] },
    "insights": [ { "title": "...", "body": "...", "type": "positive" } ]
  },
  "applicable_filters": ["date", "category", "status"],
  "ui_instructions": { ... }
}
```

This means the frontend (`report.html`) requires **zero changes**.

---

## 5. SQL Accuracy Guarantees

The SQL Agent in the Claude pipeline will be wrapped with the **exact same** Python validation layers:

| Layer | Function | What it does |
|-------|----------|-------------|
| 1 | `_fix_report_sql()` | 7-pass regex auto-correction (wrong aliases, missing JOINs, bare columns) |
| 2 | `check_sql_against_schema()` | Validates all tables/columns exist in the actual DB schema |
| 3 | `check_sql_patterns()` | Detects structural anti-patterns (correlated subqueries, etc.) |
| 4 | `validate_sql()` | Safety check (no DROP, DELETE, UPDATE, etc.) |
| 5 | `execute_sql()` + error → Claude retry | If DB returns an error, the error message is sent back to Claude to fix the SQL |

Additionally, Claude itself is significantly stronger at SQL generation than the current Groq/Llama model, so fewer corrections will be needed.

---

## 6. Dependencies

#### [MODIFY] `requirements.txt`
Add:
```
anthropic>=0.40.0
```

---

## 7. New Files Summary

| File | Purpose | Lines (est.) |
|------|---------|-------------|
| `ai/claude_client.py` | Anthropic SDK wrapper + tool loop | ~120 |
| `ai/claude_tools.py` | Tool definitions (schemas + handlers) | ~150 |
| `ai/claude_prompts.py` | System prompts for all 6 agents | ~200 |
| `ai/claude_multi_agent.py` | Main orchestrator (ClaudeReportPipeline) | ~400 |

**Total new code**: ~870 lines
**Total existing code modified**: ~15 lines (config.py + app.py)
**Total existing code deleted**: 0 lines

---

## 8. Verification Plan

### Automated
1. Run `pip install anthropic` — verify package installs
2. Run `python -c "from ai.claude_multi_agent import ClaudeReportPipeline"` — verify imports
3. Run the server and hit `/report` with `provider=claude` and a test question

### Manual Testing
Test with these exact queries (these are known to produce issues on the current pipeline):
1. `"Give me a detailed sales dashboard"` — should produce 6 diverse charts, all sales-scoped
2. `"Customer spending analysis"` — should produce customer-centric KPIs (not generic revenue)
3. `"Gold product report"` — should correctly JOIN to gold tables and filter by gold
4. `"Inorder backorder report"` — should deduce backorder logic from schema (no explicit column)
5. `"Compare this year vs last year performance"` — should produce comparison charts

For each test, verify:
- [ ] 6 KPIs with meaningful, unique values
- [ ] 6 charts with 4+ different chart types
- [ ] X-axis is always categorical, Y-axis is always numeric
- [ ] No raw IDs as chart labels
- [ ] All SQL queries execute without error
- [ ] Report renders correctly in the existing frontend
