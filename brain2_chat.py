"""
brain2_chat.py

Conversational Brain 2 with persistent history and read-only DB function calling.

Backends supported:
  - gemini       (Google Gemini 3.1 Pro, default, has Google Search grounding)
  - gemma        (Google Gemma 4 26B, free tier on Google AI Studio, no grounding)
  - anthropic    (Claude Sonnet 4.6 / Opus 4.7 / Haiku 4.5)
  - lmstudio     (local OpenAI-compatible)

Each backend has its own quirk for function calling:
  - Gemini: types.Tool(function_declarations=[...])
  - Anthropic: tools=[{name, description, input_schema}]
  - LM Studio: OpenAI-style tools=[{type:"function", function:{...}}]

This module normalizes the chat loop so the dashboard doesn't care which backend
is active.

The single tool exposed to the model is `query_jobs(sql)` which runs a SAFE
read-only SQL query against the jobs table and returns up to 100 rows as JSON.
The SQL is restricted to SELECT statements only (no INSERT/UPDATE/DELETE/DROP).

Messages are persisted in the brain2_messages table; the chat is one long
conversation across sessions.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database import get_db_connection

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


# ── config + keys ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def load_keys() -> dict:
    try:
        import keys
        return {
            "google": getattr(keys, "GOOGLE_API_KEY", ""),
            "anthropic": getattr(keys, "ANTHROPIC_API_KEY", ""),
            "openai": getattr(keys, "OPENAI_API_KEY", ""),
        }
    except ImportError:
        return {"google": "", "anthropic": "", "openai": ""}


# ── system prompt: Brain 2 self-awareness ─────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are Brain 2 inside HunterJobs ATS — a candidate-side
applicant tracking system that inverts the usual ATS dynamic: instead of helping
employers filter candidates, it helps a candidate filter the job market.

SYSTEM ARCHITECTURE (so you understand what data you have):
  Brain 1 runs a sequential three-stage pipeline against scraped job listings:
    Stage 1 (Gemma 4): hard-reject keywords, then GOOD/MAYBE/BAD verdict
    Stage 2 (Gemma 4): for GOOD jobs, company OSINT — summary, size, real
                       tech stack, culture flags, hiring legitimacy signal.
                       Auto-demotes to BAD if staffing/labeling agency detected.
    Stage 3 (Gemma 4): for surviving GOODs, GitHub OSINT for contact email +
                       outreach draft.
  You (Brain 2): strategic advisor. Read-only access to the jobs table via
                 the query_jobs(sql) tool. Periodically produce market
                 snapshots; chat with the candidate for follow-ups.

CURRENT CONFIG:
  Profile: {profile}
  Search terms: {search_terms}
  Hard rejects: {n_rejects} keyword(s) configured
  Salary floor: ${salary_floor}/month
  Sources: {sources}

LATEST SNAPSHOT SUMMARY:
{snapshot_summary}

YOUR JOB:
  - Be brutal, dense, actionable. No diplomatic softening.
  - When the candidate asks about their data, USE the query_jobs tool to
    answer with real numbers. Don't guess.
  - **Use the FEWEST queries possible.** Prefer one comprehensive query
    that returns everything you need over multiple narrow queries.
    Example: instead of 4 separate COUNT(*) queries for each verdict,
    write `SELECT verdict, COUNT(*) FROM jobs GROUP BY verdict`.
  - After 1-2 queries, you have enough data — **write the answer in text**.
    Do not keep querying indefinitely. The user is waiting.
  - The candidate is technical, treat them like a peer.
  - Maintain context across the conversation; you remember everything in
    this thread.
  - Do NOT 'think out loud' about your process. Don't write 'let me search'
    or 'I'll run several queries' or list out steps you're about to take.
    Just execute the tool calls silently and answer directly. The user only
    wants the final answer, not a play-by-play.
  - You do NOT have web search. Don't pretend to run web searches or list
    'search queries' you're about to run. Use ONLY query_jobs and your
    existing knowledge.
  - Refuse to:
      * Help write spammy mass-outreach (drafts must be personal).
      * Fabricate company information you don't have.
      * Pretend listings exist that aren't in the DB.

DB SCHEMA (for query_jobs):
  jobs(id TEXT PK, title, company, domain, location, salary_min, salary_max,
       currency, source, url, description, date_posted, date_scraped,
       verdict TEXT,           -- 'GOOD' | 'MAYBE' | 'BAD'
       reject_reason TEXT,     -- includes 'hard_reject:...', 'stage2_demoted_from_X:...'
       gemma2_done INTEGER, company_summary, hiring_signal, real_stack JSON,
       culture_flags JSON, company_size,
       gemma3_done INTEGER, contact_name, contact_title, contact_email,
       outreach_draft,
       applied INTEGER, applied_date,
       notes TEXT,             -- user's per-job notes
       row_color TEXT)         -- user-set color label

  market_snapshots(id, date, total_jobs, good_count, maybe_count, bad_count,
                   hard_reject_count, top_stacks JSON, salary_avg_min,
                   salary_avg_max, analysis, targeting_feedback)
"""


def build_system_prompt() -> str:
    cfg = load_config()
    conn = get_db_connection()
    try:
        snap = conn.execute(
            "SELECT date, total_jobs, good_count, maybe_count, bad_count, "
            "hard_reject_count, salary_avg_min, salary_avg_max "
            "FROM market_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        snap = None
    finally:
        conn.close()

    if snap:
        snapshot_summary = (
            f"Generated {snap['date'][:19]}: "
            f"{snap['total_jobs']} jobs, "
            f"{snap['good_count']} GOOD, {snap['maybe_count']} MAYBE, "
            f"{snap['bad_count']} BAD, {snap['hard_reject_count']} hard-rejected. "
            f"Avg salary range seen: ${snap['salary_avg_min']}-${snap['salary_avg_max']}/mo."
        )
    else:
        snapshot_summary = "No market snapshot yet. Suggest the user click 'Wake Brain 2'."

    return SYSTEM_PROMPT_TEMPLATE.format(
        profile=(cfg.get("profile") or "(none)")[:600],
        search_terms=", ".join(
            t.strip() for t in cfg.get("search_terms", "").splitlines() if t.strip()
        )[:300],
        n_rejects=sum(
            1 for t in cfg.get("hard_rejects", "").splitlines() if t.strip()
        ),
        salary_floor=cfg.get("salary_floor", "?"),
        sources=", ".join(cfg.get("sources", ["linkedin"])),
        snapshot_summary=snapshot_summary,
    )


# ── tool definition ──────────────────────────────────────────────────────────
QUERY_JOBS_DESCRIPTION = (
    "Run a read-only SQL SELECT query against the jobs and market_snapshots "
    "tables. Returns up to 100 rows as a JSON array of objects. "
    "Only SELECT statements are allowed (no INSERT/UPDATE/DELETE/DROP/ALTER). "
    "Use this whenever the user asks about their data — counts, filters, "
    "comparisons, lookups. Always run the query yourself rather than guessing."
)

QUERY_JOBS_PARAMETERS = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "A single SQL SELECT query. Example: "
                "SELECT title, company FROM jobs WHERE verdict='GOOD' "
                "AND applied=0 ORDER BY date_scraped DESC LIMIT 20"
            ),
        }
    },
    "required": ["sql"],
}


_BAD_SQL_PATTERNS = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|truncate|vacuum|reindex)\b",
    re.IGNORECASE,
)


def run_query_jobs_tool(sql: str) -> str:
    """Execute a read-only SQL query. Returns JSON result string."""
    if not isinstance(sql, str) or not sql.strip():
        return json.dumps({"error": "Empty SQL"})
    s = sql.strip().rstrip(";")
    # Single-statement only
    if ";" in s:
        return json.dumps({"error": "Multiple statements not allowed"})
    # SELECT only
    if not re.match(r"^\s*select\b", s, re.IGNORECASE):
        return json.dumps({"error": "Only SELECT statements are allowed"})
    if _BAD_SQL_PATTERNS.search(s):
        return json.dumps({"error": "Query contains forbidden keyword"})
    # Enforce limit
    if not re.search(r"\blimit\b", s, re.IGNORECASE):
        s = f"{s} LIMIT 100"

    conn = get_db_connection()
    try:
        rows = conn.execute(s).fetchall()
        result = []
        for row in rows[:100]:
            d = dict(row)
            # truncate long descriptions to keep tokens reasonable
            if "description" in d and isinstance(d["description"], str):
                d["description"] = d["description"][:300]
            if "analysis" in d and isinstance(d["analysis"], str):
                d["analysis"] = d["analysis"][:500]
            if "outreach_draft" in d and isinstance(d["outreach_draft"], str):
                d["outreach_draft"] = d["outreach_draft"][:300]
            result.append(d)
        return json.dumps({"rows": result, "count": len(result)})
    except sqlite3.Error as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


# ── message persistence ──────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_message(
    role: str,
    content: str,
    backend: str | None = None,
    tool_calls: list | None = None,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    hidden: bool = False,
) -> int:
    conn = get_db_connection()
    try:
        cur = conn.execute(
            """INSERT INTO brain2_messages
               (ts, role, content, backend, tool_calls, tool_name, tool_args, hidden)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                _now_iso(),
                role,
                content,
                backend,
                json.dumps(tool_calls) if tool_calls else None,
                tool_name,
                json.dumps(tool_args) if tool_args else None,
                1 if hidden else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_messages(include_hidden: bool = True, limit: int = 500) -> list[dict]:
    conn = get_db_connection()
    try:
        if include_hidden:
            rows = conn.execute(
                "SELECT * FROM brain2_messages ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM brain2_messages WHERE hidden=0 "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear_messages() -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM brain2_messages")
        conn.commit()
    finally:
        conn.close()


# ── backend adapters ─────────────────────────────────────────────────────────
def _msgs_to_gemini(messages: list[dict]) -> list:
    """Convert our DB messages to Gemini Content format."""
    from google.genai import types
    contents = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue  # system goes via system_instruction
        if role == "user":
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=m["content"])],
            ))
        elif role == "assistant":
            parts = [types.Part(text=m["content"])] if m["content"] else []
            if m.get("tool_calls"):
                tcs = json.loads(m["tool_calls"])
                for tc in tcs:
                    # Gemini 3.x requires thought_signature to be echoed back
                    # on the function_call part in subsequent requests.
                    # We base64-encode bytes for JSON storage; decode on the way back.
                    sig_b64 = tc.get("thought_signature")
                    if sig_b64:
                        import base64
                        try:
                            sig_bytes = base64.b64decode(sig_b64)
                        except Exception:
                            sig_bytes = None
                    else:
                        sig_bytes = None
                    part_kwargs = {
                        "function_call": types.FunctionCall(
                            name=tc["name"], args=tc["args"],
                        ),
                    }
                    if sig_bytes:
                        part_kwargs["thought_signature"] = sig_bytes
                    parts.append(types.Part(**part_kwargs))
            contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            contents.append(types.Content(
                role="user",  # Gemini wants tool results as user-role function_response
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=m["tool_name"],
                    response={"result": m["content"]},
                ))],
            ))
    return contents


def _msgs_to_anthropic(messages: list[dict]) -> tuple[str, list]:
    """Convert our DB messages to Anthropic messages format.
    Returns (system_prompt, messages_list)."""
    system = ""
    out = []
    pending_tool_uses: dict[str, dict] = {}  # tool_use_id -> tool_call info

    for m in messages:
        role = m["role"]
        if role == "system":
            system = m["content"]
        elif role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list = []
            if m["content"]:
                blocks.append({"type": "text", "text": m["content"]})
            if m.get("tool_calls"):
                tcs = json.loads(m["tool_calls"])
                for tc in tcs:
                    tu_id = tc.get("id") or f"tu_{len(out)}_{tc['name']}"
                    blocks.append({
                        "type": "tool_use",
                        "id": tu_id,
                        "name": tc["name"],
                        "input": tc["args"],
                    })
                    pending_tool_uses[tc["name"]] = {"id": tu_id}
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            # Find matching tool_use_id
            tu_info = pending_tool_uses.get(m.get("tool_name", ""), {})
            tu_id = tu_info.get("id", f"tu_{m.get('tool_name','unknown')}")
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": m["content"],
                }],
            })
    return system, out


def _msgs_to_openai(messages: list[dict]) -> list:
    """Convert to OpenAI/LM Studio format."""
    out = []
    pending_tool_calls: dict[str, str] = {}  # tool_name -> tool_call_id
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append({"role": "system", "content": m["content"]})
        elif role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg = {"role": "assistant", "content": m["content"] or ""}
            if m.get("tool_calls"):
                tcs = json.loads(m["tool_calls"])
                msg["tool_calls"] = []
                for i, tc in enumerate(tcs):
                    tc_id = tc.get("id") or f"call_{len(out)}_{i}"
                    pending_tool_calls[tc["name"]] = tc_id
                    msg["tool_calls"].append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"]),
                        },
                    })
            out.append(msg)
        elif role == "tool":
            tc_id = pending_tool_calls.get(m.get("tool_name", ""), "unknown")
            out.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": m["content"],
            })
    return out


# ── timeout wrapper (reused pattern from brain1) ─────────────────────────────
_LOCAL_MODEL_NOISE_RE = re.compile(
    r"\[/?(?:TOOL_RESULT|END_TOOL_RESULT|TOOL|END_TOOL|THINK|END_THINK)\]"
    r"|<think>.*?</think>|<thinking>.*?</thinking>",
    re.IGNORECASE | re.DOTALL,
)


def _clean_local_model_output(text: str) -> str:
    """Strip noise tags that local models (DeepSeek R1, Gemma) sometimes emit:
    [TOOL_RESULT]...[END_TOOL_RESULT], <think>...</think>, etc. These are
    artifacts of the model's internal reasoning leaking into the final output."""
    if not text:
        return text
    cleaned = _LOCAL_MODEL_NOISE_RE.sub("", text)
    # Also strip any orphan JSON dump of tool results that the model echoed
    cleaned = re.sub(r'\{"rows":\s*\[.*?\],?\s*"count":\s*\d+\}', "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _run_with_timeout(fn, timeout_s: float):
    import threading
    result = {"value": None, "exc": None}
    done = threading.Event()
    def _runner():
        try: result["value"] = fn()
        except Exception as e: result["exc"] = e
        finally: done.set()
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    if not done.wait(timeout=timeout_s):
        raise TimeoutError(f"Backend call exceeded {timeout_s}s")
    if result["exc"] is not None:
        raise result["exc"]
    return result["value"]


# ── unified chat turn ────────────────────────────────────────────────────────
def chat_turn(user_message: str, backend: str | None = None) -> str:
    """Run one full chat turn: persist the user message, call the LLM (with
    tool loop if it requests tools), persist all assistant + tool messages,
    return the final assistant text for the UI."""
    cfg = load_config()
    keys = load_keys()
    backend = backend or cfg.get("brain2_backend", "gemini")

    # Save user message
    save_message("user", user_message, backend=backend)

    # Build conversation: system prompt is rebuilt fresh each turn so it
    # reflects current config + latest snapshot. We don't persist the system
    # message itself between turns (we recompute it).
    system_prompt = build_system_prompt()
    history = load_messages(include_hidden=False)

    # Prepend a transient system message for the LLM, but don't double-add if
    # it's already in history (we never persist system, so this is always
    # fresh).
    messages_for_llm = [{"role": "system", "content": system_prompt}] + history

    try:
        if backend == "anthropic":
            final_text = _chat_anthropic(messages_for_llm, cfg, keys)
        elif backend == "openai":
            final_text = _chat_openai(messages_for_llm, cfg, keys)
        elif backend == "lmstudio":
            final_text = _chat_lmstudio(messages_for_llm, cfg)
        else:
            # gemini (default) and gemma both go through google-genai SDK
            final_text = _chat_google(messages_for_llm, cfg, keys, backend)
    except Exception as e:
        err = f"[backend error: {e}]"
        log.exception("Brain 2 chat turn failed")
        save_message("assistant", err, backend=backend)
        return err

    return final_text


# ── Google (Gemini + Gemma) backend ─────────────────────────────────────────
def _chat_google(messages_for_llm: list[dict], cfg: dict, keys: dict, backend: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=keys["google"])

    if backend == "gemma":
        model = cfg.get("brain2_gemma_model", "gemma-4-26b-a4b-it")
    else:
        model = cfg.get("brain2_gemini_model", "gemini-3.5-flash")

    # System instruction is the first message
    system_text = messages_for_llm[0]["content"]
    history = messages_for_llm[1:]

    tools = [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="query_jobs",
            description=QUERY_JOBS_DESCRIPTION,
            parameters=QUERY_JOBS_PARAMETERS,
        ),
    ])]

    # Note: Gemini does NOT allow combining function_declarations with
    # google_search in the same request without server-side tool invocation
    # enabled (which has its own restrictions). For the chat we prioritize
    # function calling over grounding — the periodic snapshot in brain2.py
    # still uses grounding because it doesn't need function calling.

    # Tool-call loop (max 8 iterations to prevent runaway; Gemini 3.x sometimes
    # legitimately wants 4-6 queries for a complex question).
    MAX_ITERATIONS = 8
    for _iter in range(MAX_ITERATIONS):
        contents = _msgs_to_gemini(history)
        config = types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=0.3,
            tools=tools,
        )

        def _call():
            return client.models.generate_content(
                model=model, contents=contents, config=config,
            )
        response = _run_with_timeout(_call, 90.0)

        # Extract function calls + text
        function_calls = []
        text_parts = []
        if response.candidates:
            cand = response.candidates[0]
            if cand.content and cand.content.parts:
                for part in cand.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        # Gemini 3.x returns a thought_signature on the part
                        # which we MUST echo back on the next turn.
                        sig = getattr(part, "thought_signature", None)
                        sig_b64 = None
                        if sig:
                            import base64
                            try:
                                if isinstance(sig, (bytes, bytearray)):
                                    sig_b64 = base64.b64encode(bytes(sig)).decode("ascii")
                                else:
                                    # Some SDK versions may surface it as a base64 string
                                    sig_b64 = str(sig)
                            except Exception:
                                sig_b64 = None
                        function_calls.append({
                            "name": fc.name,
                            "args": dict(fc.args) if fc.args else {},
                            "thought_signature": sig_b64,
                        })
                    elif hasattr(part, "text") and part.text:
                        text_parts.append(part.text)

        text = "".join(text_parts).strip()

        if not function_calls:
            # Done; persist and return
            save_message("assistant", text, backend=backend)
            return text

        # Persist the assistant turn with tool calls
        save_message(
            "assistant", text,
            backend=backend, tool_calls=function_calls,
        )

        # Execute each tool call and persist result
        for fc in function_calls:
            if fc["name"] == "query_jobs":
                result = run_query_jobs_tool(fc["args"].get("sql", ""))
            else:
                result = json.dumps({"error": f"Unknown tool: {fc['name']}"})
            save_message(
                "tool", result,
                backend=backend, tool_name=fc["name"], tool_args=fc["args"],
            )

        # Refresh history with new tool turns
        history = load_messages(include_hidden=False)

    # Loop exhausted — Gemini sometimes keeps querying instead of summarizing.
    # Force one final text-only response by removing tools from the config.
    log.warning(
        f"Tool loop hit {MAX_ITERATIONS} iterations; forcing text-only finalization."
    )
    final_history = load_messages(include_hidden=False)
    # Append a nudge so the model knows it must summarize now.
    nudge_messages = final_history + [{
        "role": "user",
        "content": (
            "[system] You've gathered enough data via tool calls. Now write the "
            "final answer in plain text. No more tool calls."
        ),
    }]
    try:
        contents = _msgs_to_gemini(nudge_messages)
        config_final = types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=0.3,
            # No tools — forces text response
        )

        def _final_call():
            return client.models.generate_content(
                model=model, contents=contents, config=config_final,
            )
        response = _run_with_timeout(_final_call, 60.0)
        final_text = ""
        if response.candidates and response.candidates[0].content \
                and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    final_text += part.text
        final_text = final_text.strip() or "[Brain 2 gathered data but couldn't summarize]"
        save_message("assistant", final_text, backend=backend)
        return final_text
    except Exception as e:
        log.error(f"Finalization call failed: {e}")
        msg = (
            "[Brain 2 gathered data but couldn't summarize. Try asking again, "
            "or break your question into smaller parts.]"
        )
        save_message("assistant", msg, backend=backend)
        return msg


# ── Anthropic backend ───────────────────────────────────────────────────────
def _chat_anthropic(messages_for_llm: list[dict], cfg: dict, keys: dict) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=keys["anthropic"])
    model = cfg.get("brain2_anthropic_model", "claude-sonnet-4-6")

    system, history = _msgs_to_anthropic(messages_for_llm)

    tools = [{
        "name": "query_jobs",
        "description": QUERY_JOBS_DESCRIPTION,
        "input_schema": QUERY_JOBS_PARAMETERS,
    }]

    for _iter in range(5):
        msgs = _msgs_to_anthropic(messages_for_llm)[1]
        # Refresh from history each loop
        msgs = _msgs_to_anthropic(
            [messages_for_llm[0]] + load_messages(include_hidden=False)
        )[1]

        def _call():
            return client.messages.create(
                model=model,
                max_tokens=2048,
                system=system,
                messages=msgs,
                tools=tools,
                temperature=0.3,
            )
        response = _run_with_timeout(_call, 90.0)

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append({
                    "id": block.id,
                    "name": block.name,
                    "args": block.input,
                })
        text = "".join(text_parts).strip()

        if not tool_uses:
            save_message("assistant", text, backend="anthropic")
            return text

        save_message(
            "assistant", text,
            backend="anthropic", tool_calls=tool_uses,
        )
        for tu in tool_uses:
            if tu["name"] == "query_jobs":
                result = run_query_jobs_tool(tu["args"].get("sql", ""))
            else:
                result = json.dumps({"error": f"Unknown tool: {tu['name']}"})
            save_message(
                "tool", result,
                backend="anthropic",
                tool_name=tu["name"], tool_args=tu["args"],
            )

    save_message(
        "assistant",
        "[tool loop exhausted]",
        backend="anthropic",
    )
    return "[tool loop exhausted]"


# ── OpenAI backend ──────────────────────────────────────────────────────────
def _chat_openai(messages_for_llm: list[dict], cfg: dict, keys: dict) -> str:
    from openai import OpenAI
    api_key = keys.get("openai", "")
    model_name = cfg.get("brain2_openai_model", "gpt-5.5")

    client = OpenAI(api_key=api_key)

    tools = [{
        "type": "function",
        "function": {
            "name": "query_jobs",
            "description": QUERY_JOBS_DESCRIPTION,
            "parameters": QUERY_JOBS_PARAMETERS,
        },
    }]

    for _iter in range(5):
        msgs = _msgs_to_openai(
            [messages_for_llm[0]] + load_messages(include_hidden=False)
        )

        def _call():
            return client.chat.completions.create(
                model=model_name,
                messages=msgs,
                tools=tools,
                temperature=0.3,
                timeout=90.0,
            )
        response = _run_with_timeout(_call, 95.0)
        choice = response.choices[0].message

        text = choice.content or ""
        tool_calls = []
        if getattr(choice, "tool_calls", None):
            for tc in choice.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args,
                })

        if not tool_calls:
            save_message("assistant", text, backend="openai")
            return text

        save_message(
            "assistant", text,
            backend="openai", tool_calls=tool_calls,
        )
        for tc in tool_calls:
            if tc["name"] == "query_jobs":
                result = run_query_jobs_tool(tc["args"].get("sql", ""))
            else:
                result = json.dumps({"error": f"Unknown tool: {tc['name']}"})
            save_message(
                "tool", result,
                backend="openai",
                tool_name=tc["name"], tool_args=tc["args"],
            )

    save_message("assistant", "[tool loop exhausted]", backend="openai")
    return "[tool loop exhausted]"


# ── LM Studio backend ───────────────────────────────────────────────────────
def _chat_lmstudio(messages_for_llm: list[dict], cfg: dict) -> str:
    from openai import OpenAI
    base_url = cfg.get("brain2_lmstudio_url", "http://localhost:1234/v1")
    model_name = (cfg.get("brain2_lmstudio_model") or "").strip()

    if not model_name:
        import requests
        try:
            r = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
            r.raise_for_status()
            data = r.json().get("data", [])
            model_name = data[0]["id"] if data else "no-model-loaded"
            log.info(f"LM Studio auto-detected model: {model_name}")
        except Exception as e:
            log.error(f"LM Studio probe failed: {e}")
            model_name = "lmstudio-unavailable"

    client = OpenAI(base_url=base_url, api_key="lm-studio")

    tools = [{
        "type": "function",
        "function": {
            "name": "query_jobs",
            "description": QUERY_JOBS_DESCRIPTION,
            "parameters": QUERY_JOBS_PARAMETERS,
        },
    }]

    for _iter in range(5):
        msgs = _msgs_to_openai(
            [messages_for_llm[0]] + load_messages(include_hidden=False)
        )

        def _call():
            return client.chat.completions.create(
                model=model_name,
                messages=msgs,
                tools=tools,
                temperature=0.3,
                timeout=90.0,
            )
        response = _run_with_timeout(_call, 95.0)
        choice = response.choices[0].message

        text = choice.content or ""
        tool_calls = []
        if getattr(choice, "tool_calls", None):
            for tc in choice.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args,
                })

        if not tool_calls:
            cleaned = _clean_local_model_output(text)
            save_message("assistant", cleaned, backend="lmstudio")
            return cleaned

        save_message(
            "assistant", _clean_local_model_output(text),
            backend="lmstudio", tool_calls=tool_calls,
        )
        for tc in tool_calls:
            if tc["name"] == "query_jobs":
                result = run_query_jobs_tool(tc["args"].get("sql", ""))
            else:
                result = json.dumps({"error": f"Unknown tool: {tc['name']}"})
            save_message(
                "tool", result,
                backend="lmstudio",
                tool_name=tc["name"], tool_args=tc["args"],
            )

    save_message(
        "assistant",
        "[tool loop exhausted]",
        backend="lmstudio",
    )
    return "[tool loop exhausted]"


if __name__ == "__main__":
    # Quick smoke test
    print(json.dumps(load_messages(), indent=2, default=str)[:1000])
