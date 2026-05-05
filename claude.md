# SQLBOT — AI SQL Analyst (Project Overview)fd

This document gives a detailed overview of the project:
- What the app does
- Tech stack
- Project structure
- Responsibilities of each module/file
- How the end‑to‑end pipeline works
- How data and schema stay in sync as Excel files change

---

## 1. What this project does

**Goal:** Provide an AI SQL analyst that can answer natural-language questions about your PostgreSQL (Neon) data, generating SQL, executing it safely, and returning both results and human-readable explanations.

Key properties:

- **Dynamic schema awareness**: No hardcoded table/column lists. The app introspects the live Neon database on every run.
- **Excel-driven data**: Source data lives in Excel files. A sync script loads them into Neon as normalized tables.
- **Safe SQL execution**: Only `SELECT` queries are allowed; dangerous commands are blocked.
- **Multi-turn memory**: The chatbot remembers the last few Q&A turns per browser session (stored in Neon) to handle follow-up questions.
- **Deployable**:
  - As a Docker app on Hugging Face Spaces (`sdk: docker`).
  - Mirrored in a GitHub repo.

---

## 2. Tech stack

- **Backend framework**: FastAPI  
- **Application server**: uvicorn  
- **Database**: PostgreSQL (Neon cloud), accessed via SQLAlchemy engines  
- **Data loading**: pandas + SQLAlchemy `to_sql` from Excel into Postgres  
- **AI / LLM orchestration**:
  - dspy for defining prompt “signatures” and multi-step pipelines
  - groq client (and optionally openai) via litellm / client libraries
- **Config / env**: python-dotenv and a central `config.py`  
- **Frontend**: Vanilla HTML / CSS / JS (no framework), served by FastAPI  
- **Containerization**: Dockerfile for Hugging Face Spaces (`sdk: docker`)  
- **Version control**: git, with remotes to Hugging Face Space and GitHub

---

## 3. High-level architecture

The system has four main layers:

1. **Data layer (Neon + Excel sync)**
   - Excel files (`inventory_v5.xlsx`, `purchase_orders_v6.xlsx`, `sales_table_v2.xlsx`, etc.)
   - `data_sync.py` converts Excel sheets → normalized Postgres tables.
   - Dynamic schema + relationship + profiling components.

2. **AI reasoning layer**
   - `ai/signatures.py`: prompt contracts (Analyze & Plan, Generate SQL, Repair, Interpret & Insight).
   - `ai/pipeline.py`: orchestrates LLM calls, validation, and execution.
   - `ai/groq_setup.py`: loads LLM clients from environment/config.

3. **API layer (FastAPI)**
   - `app.py`: defines REST endpoints (`/chat`, `/generate-sql`, `/schema`, etc.).
   - Handles conversation IDs and stores/retrieves chat history from Neon.

4. **Frontend**
   - `frontend/index.html` + `style.css` + `script.js`.
   - SPA-style UI that calls `/chat` and renders SQL, table results, explanations, and insights.

---

## 4. Project structure and file responsibilities

### 4.1. Top-level

#### `app.py`

Main FastAPI application and entrypoint when run with uvicorn.

Responsibilities:

- Create FastAPI app and configure CORS.
- Define request/response models:
  - `QuestionRequest`: `{ question, provider, conversation_id }`
  - `GenerateSQLResponse`: `{ sql }`
  - `ChatResponse`: `{ sql, data, answer, insights }`
- Endpoints:
  - `POST /generate-sql`
    - Uses `SQLAnalystPipeline.generate_sql_only(question)` to return SQL only.
  - `POST /chat`
    - Imports `SQLAnalystPipeline` and `db.memory` functions.
    - Accepts `question`, `provider`, `conversation_id`.
    - Fetches last 5 conversation turns for that `conversation_id`.
    - Builds an augmented prompt including recent Q&A context.
    - Runs `pipeline.run(question_with_context)`.
    - Stores the new turn (original question, answer, sql) to the `chat_history` table.
  - `GET /schema`
    - Returns structured schema from `db.schema.get_schema()`.
  - `GET /relationships`
    - Returns inferred table relationships from `db.relationships.discover_relationships()`.
  - Frontend serving:
    - Mounts `/static` to serve `frontend` assets.
    - `GET /` returns `frontend/index.html`.
- Local dev entrypoint: `if __name__ == "__main__": uvicorn.run("app:app", ...)`.

#### `config.py`

Central configuration for environment variables and defaults.

- Loads `.env` via `dotenv.load_dotenv()`.
- Exposes:
  - `DATABASE_URL`
  - `GROQ_API_KEY`, `GROQ_MODEL`
  - `OPENAI_API_KEY`, `OPENAI_MODEL`

#### `data_sync.py`

Excel → PostgreSQL data synchronization script.

- CLI usage:
  - `python data_sync.py path/to/file.xlsx`
  - `python data_sync.py path/to/folder/`
- `normalize_column(name)`: cleans and normalizes column names (lowercase, non-alphanumeric → `_`, dedupe).
- `sync_dataframe(df, table_name)`: writes DataFrame to Postgres with `if_exists="replace"`.
- `sync_excel(filepath)`:
  - If one sheet: table name from file name.
  - If multiple sheets: each becomes `filebasename_sheetname` (normalized).

This script is how new Excel data is pushed into Neon; the chatbot then automatically picks up the new schema.

#### `Dockerfile`

Container build for Hugging Face Spaces (Docker SDK).

- Base image: `python:3.11-slim`.
- Installs system deps, copies project, installs `requirements.txt`.
- Sets `PORT=7860` and runs `uvicorn app:app`.

#### `.dockerignore`

Avoids sending unnecessary/secret files in Docker build context:

- Ignores `__pycache__`, `.git`, `.env`, virtualenvs, and large `.xlsx` files.

#### `.gitignore`

Standard Python/git hygiene and secret protection:

- Ignores `.env`, virtualenvs, `__pycache__`, editor/OS junk, Excel data files.

#### `README.md`

Space / repo metadata and human‑oriented overview.

- YAML frontmatter recognized by Hugging Face:

```yaml
---
title: sqlbot
emoji: 🧠
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---
```

---

### 4.2. `ai` package — LLM reasoning

#### `ai/signatures.py`

Defines DSPy Signatures that describe the inputs/outputs of each LLM stage.

Main signatures:

- `AnalyzeAndPlan`
  - Inputs: `question`, `schema_info`, `relationships`, `data_profile`.
  - Outputs: `intent`, `relevant_tables`, `relevant_columns`, `join_conditions`, `where_conditions`, `aggregations`, `group_by`, `order_by`, `limit_val`.
  - Prompt includes business rules (e.g., how to treat status columns, transaction vs catalog queries).

- `SQLGeneration`
  - Inputs: `question`, `schema_info`, `query_plan`.
  - Output: `sql_query` as raw PostgreSQL `SELECT` (no markdown, no explanation).

- `SQLCritiqueAndFix`
  - Evaluates SQL vs schema and can generate corrected SQL.

- `InterpretAndInsight`
  - Inputs: `question`, `sql_query`, `query_results` (JSON).
  - Outputs: `answer` (plain-language explanation) and `insights` (3–5 analytic bullet points).

- `SQLRepair`
  - Given failing SQL + error message + schema + question, outputs corrected raw SQL.

#### `ai/pipeline.py`

Orchestrates the full reasoning flow via `SQLAnalystPipeline`.

Key steps in `run(question)`:

1. Build context:
   - `schema_str = format_schema()` from `db.schema`.
   - `rels_str = format_relationships()` from `db.relationships`.
   - `profile_str = get_data_profile()` from `db.profiler`.
2. Analyze & Plan:
   - Calls `self.analyze(...)` to create a structured plan.
3. SQL Generation:
   - Calls `self.generate_sql(...)`, cleans the raw text to pure SQL.
4. Schema validation:
   - Uses `check_sql_against_schema` to detect non-existing tables/columns and optionally regenerates SQL with feedback.
5. Safety validation:
   - `validate_sql(sql)` ensures a safe `SELECT` query only.
6. Execution + repair loop:
   - Uses `execute_sql(sql)`.
   - On DB error, calls `self.repair(...)` and retries up to `MAX_REPAIR_RETRIES`.
7. Interpretation & insights:
   - Serializes up to 50 result rows.
   - Calls `self.interpret(question=..., sql_query=..., query_results=...)`.
8. Returns a dict: `{ "sql": sql, "data": rows, "answer": answer, "insights": insights }`.

Also exposes `generate_sql_only(question)` and helper `_clean_sql`.

---

### 4.3. `db` package — database utilities

#### `db/connection.py`

Singleton SQLAlchemy engine and connection helpers.

- `get_engine()` uses `config.DATABASE_URL`.
- `get_connection()` returns a new connection context manager.

#### `db/schema.py`

Schema introspection via `information_schema.columns`.

- `get_schema(force_refresh=False)` returns `{table_name: [{column_name, data_type, is_nullable}, ...]}`.
- `format_schema()` returns a prompt‑friendly string view of the schema.
- `get_table_names()` returns a list of all public tables.

#### `db/relationships.py`

Relationship discovery between tables.

- Reads explicit foreign keys from `information_schema.table_constraints`.
- Adds implicit relationships:
  - Exact column name matches
  - ID-pattern matches (`*_id`, `*_key`)
  - Fuzzy name similarity
- `format_relationships()` renders them as readable text.

#### `db/profiler.py`

Profiles actual database content to give the LLM richer context:

- Row counts.
- Distinct values and counts for categorical columns.
- Min/max/avg for numeric columns.
- Date ranges for date columns.
- Adds business-rule text to the profile for the LLM to follow.

Results are cached to reduce DB load.

#### `db/executor.py`

Safe SQL execution against PostgreSQL.

- Validates SQL with `validate_sql` (only `SELECT`/`WITH`).
- Executes using SQLAlchemy and returns:
  - Success flag
  - Data rows (as list of dicts)
  - Column names
  - Error string (on failure)

#### `db/memory.py`

Conversation memory stored in Neon (`chat_history` table).

- Ensures table exists:

  ```sql
  chat_history (
    id BIGSERIAL PRIMARY KEY,
    conversation_id TEXT,
    question TEXT,
    answer TEXT,
    sql_query TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
  )
  ```

- `add_turn(conversation_id, question, answer, sql_query)` inserts a Q/A turn.
- `get_recent_history(conversation_id, limit=5)` returns the last `limit` turns (oldest first).

Used by `/chat` to give the LLM context for follow-up questions.

---

### 4.4. Frontend (`frontend` folder)

#### `frontend/index.html`

Main UI markup.

- Header with logo, title, and model switcher (Groq / OpenAI).
- Input section with textarea and submit button.
- Loading indicator with step animation.
- Results section:
  - Generated SQL
  - Query results table
  - Explanation
  - Insights
- Error section for displaying API errors.

#### `frontend/style.css`

Visual styling for a modern, dark-themed UI:

- Glassmorphism cards, gradient background, responsive layout.
- Styled table, tags, buttons, and loading indicators.

#### `frontend/script.js`

Frontend logic and API integration.

- Tracks:
  - Selected model provider.
  - A persistent `conversationId` stored in `localStorage`.
- Handles:
  - Submitting questions (button or Enter).
  - Calling `POST /chat` with `{ question, provider, conversation_id }`.
  - Rendering SQL, tabular data, answer text, and insights.
  - Showing loading state and errors.
  - Copy-to-clipboard for generated SQL.

---

## 5. End-to-end flow summary

1. **Data ingestion**
   - You add/update Excel files.
   - Run `python data_sync.py <file or folder>` to replace tables in Neon.

2. **User interaction**
   - User opens the web UI (locally or on Hugging Face).
   - Types a question and clicks submit.

3. **API request**
   - Frontend sends JSON `{ question, provider, conversation_id }` to `/chat`.

4. **Context building & memory**
   - Backend loads recent chat history from `db.memory`.
   - Builds an augmented question including recent Q&A.

5. **Reasoning pipeline**
   - `SQLAnalystPipeline` uses live schema, relationships, and data profile from Neon.
   - Generates, validates, and (if needed) repairs SQL.
   - Executes SQL and interprets results into explanations and insights.

6. **Response**
   - API returns `{ sql, data, answer, insights }`.
   - Frontend renders the results and the turn is saved to `chat_history` for future context.

---

## 6. Design principles

- **Schema-driven, not hardcoded**
  - Schema and relationships are discovered dynamically from Neon.
- **Separation of concerns**
  - Clear layers: data sync, schema/relationships, LLM reasoning, API, frontend.
- **Safe by default**
  - Only `SELECT` queries are executed; destructive SQL is rejected.
- **Deployable & portable**
  - Dockerfile + `sdk: docker` make it simple to run on Hugging Face or other container platforms.
- **Extensible**
  - Business rules live in prompt text and can evolve.
  - Memory is a simple table and can be extended with more metadata as needed.

