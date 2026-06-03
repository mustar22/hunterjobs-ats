"""
brain2.py

Market analyst. Aggregates 7 days of Brain 1 output, sends to Gemini 3.1 Pro
with Google Search grounding for live web validation. Writes a snapshot to
market_snapshots. Heartbeat via runner_status so the dashboard can show progress.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import types
from openai import OpenAI

from core.database import get_db_connection, init_db
import core.runner_status as runner_status

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("hunterjobs.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

from core.config import CONFIG_PATH


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


def aggregate_market_data(days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT verdict, reject_reason, real_stack, salary_min, salary_max "
        "FROM jobs WHERE date_scraped >= ?",
        (since,),
    ).fetchall()
    applied = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied=1 AND date_scraped >= ?",
        (since,),
    ).fetchone()[0]
    conn.close()

    if not rows:
        return {}

    counts = {"GOOD": 0, "MAYBE": 0, "BAD": 0, "hard_reject": 0}
    stack_freq: dict[str, int] = {}
    sal_min, sal_max = [], []

    for row in rows:
        v = row["verdict"] or "BAD"
        rr = row["reject_reason"] or ""
        if "hard_reject" in rr:
            counts["hard_reject"] += 1
        elif v in counts:
            counts[v] += 1

        try:
            stacks = json.loads(row["real_stack"] or "[]")
        except json.JSONDecodeError:
            stacks = []
        for s in stacks:
            stack_freq[s] = stack_freq.get(s, 0) + 1

        if row["salary_min"]:
            sal_min.append(row["salary_min"])
        if row["salary_max"]:
            sal_max.append(row["salary_max"])

    top_stacks = sorted(stack_freq, key=stack_freq.get, reverse=True)[:15]

    return {
        "total_jobs": len(rows),
        "good_count": counts["GOOD"],
        "maybe_count": counts["MAYBE"],
        "bad_count": counts["BAD"],
        "hard_reject_count": counts["hard_reject"],
        "top_stacks": top_stacks,
        "salary_avg_min": int(sum(sal_min) / len(sal_min)) if sal_min else 0,
        "salary_avg_max": int(sum(sal_max) / len(sal_max)) if sal_max else 0,
        "applied_count": applied,
    }


def build_prompt(data: dict, profile: str, backend: str = "gemini") -> str:
    base = (
        f"A candidate with this profile is job hunting:\n{profile or '(no profile)'}\n\n"
        f"Market data from the last 7 days of their scraping:\n"
        f"- Total scraped: {data['total_jobs']}\n"
        f"- Hard rejected instantly: {data['hard_reject_count']}\n"
        f"- GOOD: {data['good_count']} | MAYBE: {data['maybe_count']} | BAD: {data['bad_count']}\n"
        f"- Applied so far: {data['applied_count']}\n"
        f"- Top stacks appearing: "
        f"{', '.join(data['top_stacks']) if data['top_stacks'] else 'none yet'}\n"
        f"- Avg salary range seen: {data['salary_avg_min']}-{data['salary_avg_max']}\n\n"
    )
    tasks = (
        "Tasks:\n"
        "1. Are the top stacks actually in demand right now relative to this candidate's "
        "niche? Are they signal or noise?\n"
        "2. Is the candidate's salary floor realistic for remote AI/ML contractors right now?\n"
        "3. What skills are surging that the candidate should flex more in outreach?\n"
        "4. Is the candidate targeting the right tier and positioning correctly?\n"
        "5. What patterns in the BAD/hard_reject pile reveal about market structure?\n\n"
        "Format: write a single, dense, brutal strategist report using numbered sections "
        "(1. SECTION TITLE in caps, then prose). No diplomatic softening. No filler. "
        "Do NOT 'think out loud' about searching, planning, or any process — produce the "
        "final report directly. Do NOT show your reasoning steps. The user wants only the "
        "polished output."
    )
    if backend == "gemini":
        # Gemini has Google Search grounding available — explicitly invite it.
        head = (
            "You are an elite tech market strategist with access to Google Search. "
            "Search to validate stack demand and salary ranges where useful, then "
            "produce the final report. Do not show intermediate search steps to the user.\n\n"
        )
    else:
        # No grounding. Tell the model so it doesn't hallucinate searches.
        head = (
            "You are an elite tech market strategist. You do not have web search; "
            "analyze ONLY the data given below and apply your own market knowledge. "
            "Do not pretend to search or 'run queries' — go straight to the report.\n\n"
        )
    return head + base + tasks


def run_brain2() -> None:
    cfg = load_config()
    keys = load_keys()
    backend = cfg.get("brain2_backend", "gemini")
    profile_text = cfg.get("profile", "")

    log.info("=" * 60)
    log.info(f"Brain 2 awakened | backend={backend}")
    log.info("=" * 60)

    init_db()
    runner_status.start("brain2")
    runner_status.patch("brain2", pid=os.getpid(), phase="aggregating")

    # Watchdog: hard-kill if dashboard heartbeat dies (covers being stuck in a
    # synchronous Gemini call).
    import threading as _t
    def _watchdog():
        import time as _time
        while True:
            _time.sleep(15)
            if not runner_status.dashboard_is_alive(max_age_seconds=90):
                log.warning("Watchdog: dashboard heartbeat dead for >90s; hard-killing brain2.")
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
                try:
                    runner_status.finish("brain2", error="killed by watchdog (dashboard gone)")
                except Exception:
                    pass
                os._exit(1)
    _t.Thread(target=_watchdog, daemon=True, name="brain2-watchdog").start()

    data = aggregate_market_data(days=7)
    if not data:
        log.warning("No job data in last 7 days. Run Brain 1 first.")
        runner_status.finish("brain2", error="no_data")
        return

    if not runner_status.dashboard_is_alive(max_age_seconds=90):
        log.warning("Dashboard gone (>90s) before Brain 2 could call Gemini. Exiting.")
        runner_status.finish("brain2", error="aborted: dashboard closed")
        return

    log.info(f"Aggregated {data['total_jobs']} jobs")
    prompt = build_prompt(data, profile_text, backend=backend)
    targeting_feedback = ""

    if backend == "lmstudio":
        runner_status.patch("brain2", phase="calling lm studio")
        lm_url = cfg.get("brain2_lmstudio_url", "http://localhost:1234/v1")
        lm_model = (cfg.get("brain2_lmstudio_model") or "").strip()
        if not lm_model:
            # Auto-detect first loaded model
            try:
                import requests
                r = requests.get(f"{lm_url.rstrip('/')}/models", timeout=5)
                r.raise_for_status()
                data_ = r.json().get("data", [])
                lm_model = data_[0]["id"] if data_ else "lmstudio-unknown"
            except Exception:
                lm_model = "lmstudio-unknown"
        log.info(f"Calling LM Studio: {lm_model}")
        client = OpenAI(base_url=lm_url, api_key="lm-studio")
        response = client.chat.completions.create(
            model=lm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=120.0,
        )
        analysis_text = response.choices[0].message.content or ""

    elif backend == "anthropic":
        runner_status.patch("brain2", phase="calling claude")
        anthropic_model = cfg.get("brain2_anthropic_model", "claude-sonnet-4-6")
        log.info(f"Calling Anthropic Claude: {anthropic_model}")
        try:
            import anthropic
        except ImportError:
            log.error("anthropic package not installed. pip install anthropic")
            runner_status.finish("brain2", error="anthropic package missing")
            return
        if not keys.get("anthropic"):
            log.error("ANTHROPIC_API_KEY not set in keys.py")
            runner_status.finish("brain2", error="ANTHROPIC_API_KEY missing")
            return
        client = anthropic.Anthropic(api_key=keys["anthropic"])
        response = client.messages.create(
            model=anthropic_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        analysis_text = "".join(
            block.text for block in response.content if block.type == "text"
        )

    elif backend == "openai":
        runner_status.patch("brain2", phase="calling openai")
        openai_model = cfg.get("brain2_openai_model", "gpt-5.5")
        log.info(f"Calling OpenAI: {openai_model}")
        if not keys.get("openai"):
            log.error("OPENAI_API_KEY not set in keys.py")
            runner_status.finish("brain2", error="OPENAI_API_KEY missing")
            return
        client = OpenAI(api_key=keys["openai"])
        response = client.chat.completions.create(
            model=openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=120.0,
        )
        analysis_text = response.choices[0].message.content or ""

    elif backend == "gemma":
        runner_status.patch("brain2", phase="calling gemma")
        gemma_model = cfg.get("brain2_gemma_model", "gemma-4-26b-a4b-it")
        log.info(f"Calling Gemma: {gemma_model}")
        client = genai.Client(api_key=keys["google"])
        config = types.GenerateContentConfig(temperature=0.2)
        response = client.models.generate_content(
            model=gemma_model,
            contents=prompt,
            config=config,
        )
        analysis_text = response.text or ""

    else:
        # backend == "gemini" (default)
        runner_status.patch("brain2", phase="calling gemini")
        gemini_model = cfg.get("brain2_gemini_model", "gemini-3.5-flash")
        log.info(f"Calling Gemini ({gemini_model}) with Google Search grounding")
        client = genai.Client(api_key=keys["google"])
        config = types.GenerateContentConfig(
            temperature=0.2,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        response = client.models.generate_content(
            model=gemini_model,
            contents=prompt,
            config=config,
        )
        analysis_text = response.text or ""

        if getattr(response, "candidates", None):
            cand = response.candidates[0]
            gm = getattr(cand, "grounding_metadata", None)
            if gm and getattr(gm, "grounding_chunks", None):
                sources = []
                for ch in gm.grounding_chunks:
                    web = getattr(ch, "web", None)
                    if web and getattr(web, "uri", None):
                        sources.append(web.uri)
                if sources:
                    targeting_feedback = "Sources used: " + ", ".join(sources[:5])

    runner_status.patch("brain2", phase="writing")
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO market_snapshots (
            date, total_jobs, good_count, maybe_count, bad_count,
            hard_reject_count, top_stacks, salary_avg_min, salary_avg_max,
            analysis, targeting_feedback
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            data["total_jobs"], data["good_count"], data["maybe_count"],
            data["bad_count"], data["hard_reject_count"],
            json.dumps(data["top_stacks"]),
            data["salary_avg_min"], data["salary_avg_max"],
            analysis_text, targeting_feedback,
        ),
    )
    conn.commit()
    conn.close()

    # Inject the snapshot into chat history as a hidden system message so that
    # subsequent chat follow-ups can reference it. The chat module rebuilds the
    # system prompt each turn with a snapshot summary, but the full text is
    # only available if the user asks.
    try:
        from pipeline import brain2_chat
        brain2_chat.save_message(
            role="system",
            content=(
                "Fresh market snapshot just generated:\n\n"
                + analysis_text
                + ("\n\nSources: " + targeting_feedback if targeting_feedback else "")
            ),
            backend=backend,
            hidden=True,
        )
    except Exception as e:
        log.warning(f"Could not save snapshot to chat history: {e}")

    runner_status.finish("brain2")
    log.info("Brain 2 analysis written.")
    log.info("=" * 60)


import atexit


def _cleanup_on_exit():
    try:
        s = runner_status.read_status()
        if s["brain2"]["state"] == "running":
            runner_status.finish("brain2", error="process exited unexpectedly")
    except Exception:
        pass


if __name__ == "__main__":
    atexit.register(_cleanup_on_exit)
    try:
        run_brain2()
    except Exception as e:
        log.exception("Brain 2 crashed")
        runner_status.finish("brain2", error=str(e))
        raise
