"""Central configuration — reads .env and exposes all settings."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Database ────────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:universe@localhost:5432/postgres").strip()

# ── Groq ────────────────────────────────────────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── OpenAI ──────────────────────────────────────────────────────────────────
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

# ── Anthropic (Claude) ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
# Haiku: used for lightweight agents (Context, Data Analyst, QA) — ~8x cheaper & faster
CLAUDE_HAIKU_MODEL: str = os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-6")
