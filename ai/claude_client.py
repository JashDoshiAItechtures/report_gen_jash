"""Thin wrapper around the Anthropic SDK for multi-agent report generation.

Provides a reusable `call_agent()` function that handles:
- Sending messages to Claude with a system prompt and tools
- Executing tool calls in a loop until Claude produces a final text response
- Extracting and repairing JSON from LLM output
- Rich ANSI terminal logging showing every step of the agent's thinking
"""

import json
import logging
import re
import time
from typing import Any, Callable

import anthropic

import config

logger = logging.getLogger(__name__)

# ── ANSI Color Codes ─────────────────────────────────────────────────────────
_R       = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"
_CYAN    = "\033[96m"
_GREEN   = "\033[92m"
_YELLOW  = "\033[93m"
_RED     = "\033[91m"
_BLUE    = "\033[94m"
_MAGENTA = "\033[95m"
_WHITE   = "\033[97m"
_GREY    = "\033[90m"


def _c(text: Any, *codes: str) -> str:
    """Wrap text with ANSI codes."""
    return "".join(codes) + str(text) + _R


def _tee(msg: str) -> None:
    """Print to terminal (visible alongside uvicorn logs)."""
    print(msg, flush=True)


# ── Tool icon map ─────────────────────────────────────────────────────────────
_TOOL_ICONS = {
    "get_db_schema":      "🗄️  ",
    "get_relationships":  "🔗  ",
    "get_data_profile":   "📊  ",
    "execute_sql_query":  "⚡  ",
    "validate_sql_query": "✅  ",
}


class ClaudeClient:
    """Manages Anthropic API interactions for the multi-agent pipeline."""

    def __init__(self):
        if not config.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file or environment variables."
            )
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._model = config.CLAUDE_MODEL

    # ── Core agent call ─────────────────────────────────────────────────

    def call_agent(
        self,
        *,
        system_prompt: str,
        user_message: str,
        tools: list[dict] | None = None,
        tool_handlers: dict[str, Callable] | None = None,
        max_tokens: int = 8192,
        max_tool_rounds: int = 15,
        agent_name: str = "Agent",
        model: str | None = None,
        use_cache: bool = True,
    ) -> str:
        """Call Claude with a system prompt, user message, and optional tools.

        Executes tool calls in a loop until Claude returns a final text response.
        Prints rich ANSI-colored logging at every step.

        Returns the final text response from Claude.
        """
        messages: list[dict] = [{"role": "user", "content": user_message}]
        agent_start = time.time()
        active_model = model or self._model

        _tee(
            f"  {_c('MODEL', _DIM, _GREY)}: {_c(active_model, _DIM)}  "
            f"{_c('max_tokens', _DIM, _GREY)}: {_c(max_tokens, _DIM)}  "
            f"{_c('tools', _DIM, _GREY)}: {_c(len(tools or []), _DIM)}  "
            f"{_c('cache', _DIM, _GREY)}: {_c('ON' if use_cache else 'OFF', _GREEN if use_cache else _GREY, _DIM)}"
        )
        _tee(
            f"  {_c('Prompt', _DIM, _GREY)}: "
            f"{_c(user_message[:130].replace(chr(10), ' ') + '...', _DIM)}"
        )

        for round_num in range(max_tool_rounds):
            round_start = time.time()

            # Build system prompt — wrap in list with cache_control when caching is on.
            # Claude will serve cached tokens at ~10% of normal cost after first call.
            if use_cache:
                system_block = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_block = system_prompt

            kwargs: dict[str, Any] = {
                "model": active_model,
                "max_tokens": max_tokens,
                "system": system_block,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = self._client.messages.create(**kwargs)

            # Log cache usage if available
            usage = getattr(response, "usage", None)
            if usage:
                cache_read   = getattr(usage, "cache_read_input_tokens",   0) or 0
                cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
                if cache_read or cache_create:
                    _tee(
                        f"  {_c('  cache', _DIM, _GREY)}: "
                        f"{_c(f'hit={cache_read:,} create={cache_create:,}', _GREEN, _DIM)}"
                    )
            round_elapsed = time.time() - round_start

            # Check if response contains tool_use blocks
            tool_use_blocks = [
                block for block in response.content if block.type == "tool_use"
            ]
            text_blocks = [
                block for block in response.content if block.type == "text"
            ]

            if not tool_use_blocks:
                # ── Final text response ──────────────────────────────────
                final_text = "\n".join(b.text for b in text_blocks)
                total_elapsed = time.time() - agent_start

                _tee(
                    f"  {_c('◉ DONE', _GREEN, _BOLD)}  "
                    f"{_c(f'{len(final_text):,} chars', _CYAN)}  "
                    f"{_c(f'{round_num + 1} round(s)', _DIM)}  "
                    f"{_c(f'{total_elapsed:.1f}s', _YELLOW)}"
                )
                # Short preview of final text
                preview = final_text[:250].replace("\n", " ").strip()
                if len(final_text) > 250:
                    preview += "..."
                _tee(f"  {_c('   ' + preview, _DIM)}")

                logger.info(
                    "%s responded — %d chars, %d tool rounds, %.1fs",
                    agent_name, len(final_text), round_num + 1, total_elapsed,
                )
                return final_text

            # ── Tool call round ──────────────────────────────────────────
            _tee(
                f"\n  {_c(f'  Round {round_num + 1}', _YELLOW, _BOLD)}  "
                f"{_c(f'{len(tool_use_blocks)} tool call(s)', _YELLOW)}  "
                f"{_c(f'{round_elapsed:.1f}s', _GREY)}"
            )

            # Add assistant response to messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool and collect results
            tool_results = []
            for i, tool_block in enumerate(tool_use_blocks):
                tool_name  = tool_block.name
                tool_input = tool_block.input
                tool_id    = tool_block.id
                icon = _TOOL_ICONS.get(tool_name, "🔧  ")
                is_last = (i == len(tool_use_blocks) - 1)
                branch = "└─" if is_last else "├─"

                # Pretty-print tool input
                input_preview = ""
                if tool_input:
                    raw = json.dumps(tool_input)
                    input_preview = raw[:100] + ("..." if len(raw) > 100 else "")

                _tee(
                    f"  {_c(f'  {branch}', _GREY)} {icon}"
                    f"{_c(tool_name, _CYAN, _BOLD)}"
                    + (f"  {_c(input_preview, _DIM)}" if input_preview else "")
                )

                handler = (tool_handlers or {}).get(tool_name)
                t0 = time.time()

                if handler is None:
                    result_str = json.dumps({"error": f"Unknown tool: {tool_name}"})
                    indent = "     " if is_last else "  |  "
                    _tee(f"  {_c(indent, _GREY)}{_c('No handler registered', _RED)}")
                    logger.warning("No handler for tool '%s'", tool_name)
                else:
                    try:
                        result = handler(**tool_input)
                        result_str = (
                            result if isinstance(result, str)
                            else json.dumps(result, default=str)
                        )
                        tool_elapsed = time.time() - t0

                        # Detect success/error in result
                        is_error = False
                        result_preview = result_str[:180].replace("\n", " ")
                        if len(result_str) > 180:
                            result_preview += "..."
                        try:
                            parsed = json.loads(result_str)
                            if isinstance(parsed, dict):
                                if "error" in parsed:
                                    is_error = True
                                    result_preview = str(parsed["error"])[:150]
                                elif parsed.get("success") is False:
                                    is_error = True
                                    result_preview = str(parsed.get("error", "failed"))[:150]
                                elif parsed.get("success") is True:
                                    rows = parsed.get("row_count", len(parsed.get("data", [])))
                                    cols = parsed.get("columns", [])
                                    col_str = ", ".join(str(c) for c in cols[:5])
                                    if len(cols) > 5:
                                        col_str += "..."
                                    result_preview = f"{rows} rows  cols: [{col_str}]"
                        except Exception:
                            pass

                        indent = "     " if is_last else "  |  "
                        if is_error:
                            _tee(f"  {_c(indent, _GREY)}{_c('✗ ERROR', _RED, _BOLD)}: {_c(result_preview, _RED)}  {_c(f'{tool_elapsed:.2f}s', _GREY)}")
                        else:
                            _tee(f"  {_c(indent, _GREY)}{_c('✓', _GREEN, _BOLD)} {_c(result_preview, _DIM)}  {_c(f'{tool_elapsed:.2f}s', _GREY)}")

                    except Exception as exc:
                        result_str = json.dumps({"error": f"Tool execution failed: {str(exc)}"})
                        indent = "     " if is_last else "  |  "
                        _tee(f"  {_c(indent, _GREY)}{_c(f'✗ Exception: {exc}', _RED)}")
                        logger.error("Tool '%s' error: %s", tool_name, exc)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_str,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        # Exceeded max rounds
        total_elapsed = time.time() - agent_start
        _tee(
            f"  {_c('WARNING: Max tool rounds hit', _RED, _BOLD)} "
            f"({max_tool_rounds} rounds, {total_elapsed:.1f}s)"
        )
        logger.warning(
            "%s hit max tool rounds (%d) — %.1fs elapsed",
            agent_name, max_tool_rounds, total_elapsed,
        )
        return ""

    # ── JSON helpers ────────────────────────────────────────────────────

    @staticmethod
    def extract_json(raw: str) -> dict:
        """Extract and parse JSON from Claude's text output."""
        text = raw.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # Find JSON object boundaries
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        # Stage 1: direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Stage 2: repair then parse
        repaired = ClaudeClient._repair_json(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        # Stage 3: re-extract boundaries after repair
        start = repaired.find("{")
        end = repaired.rfind("}")
        if start != -1 and end != -1 and end > start:
            repaired = repaired[start : end + 1]

        return json.loads(repaired)

    @staticmethod
    def _repair_json(text: str) -> str:
        """Best-effort repair of common LLM JSON generation errors."""
        # Pass 0: single-quoted to double-quoted
        if "'" in text and '"' not in text:
            text = re.sub(r"(?<=[{,\[]\s*)'", '"', text)
            text = re.sub(r"'(?=\s*[:\}\],])", '"', text)

        # Pass 1: escape control chars inside strings
        result: list[str] = []
        in_string = False
        escape_next = False
        for ch in text:
            if escape_next:
                result.append(ch)
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                result.append(ch)
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                if ch == "\n":
                    result.append("\\n")
                elif ch == "\r":
                    result.append("\\r")
                elif ch == "\t":
                    result.append("\\t")
                else:
                    result.append(ch)
            else:
                result.append(ch)
        text = "".join(result)

        # Pass 2: trailing commas
        text = re.sub(r",(\s*[}\]])", r"\1", text)

        # Pass 3: close truncated JSON
        depth_brace = 0
        depth_bracket = 0
        in_str = False
        esc = False
        for ch in text:
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "{":
                    depth_brace += 1
                elif ch == "}":
                    depth_brace -= 1
                elif ch == "[":
                    depth_bracket += 1
                elif ch == "]":
                    depth_bracket -= 1

        if in_str:
            text += '"'
        if depth_bracket > 0:
            text += "]" * depth_bracket
        if depth_brace > 0:
            text += "}" * depth_brace

        return text
