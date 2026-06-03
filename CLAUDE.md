# HunterJobs ATS — Project Context

## What this is

Candidate-side ATS. Inverts the usual dynamic: instead of helping employers filter candidates, it helps a candidate filter the job market. Local Python app, no cloud accounts needed.

Three-stage AI pipeline per job listing → NiceGUI dashboard at `http://localhost:8080`.

## Run

```bash
# One-time: install as an editable package (puts core/pipeline/ui on the path)
pip install -e .

# Dashboard (main entry point)
python dashboard.py

# Or via launcher scripts
./_start.sh        # macOS / Linux
_start.bat         # Windows
```

Tests:
```bash
.venv/bin/python3 -m pytest -v
```

## Architecture

Packaged via `pyproject.toml` (PEP 621, setuptools). `core/`, `pipeline/`, `ui/`
are real packages; `dashboard.py` is the top-level entry module (`py-modules`).
Detached brains are launched as `python -m pipeline.run_brain1` with cwd=root.

```
dashboard.py              Entry point: bootstraps config/DB/heartbeat, defines the @ui.page("/"), runs the server

core/                     Leaf layer — no deps on pipeline/ui
  config.py               Single source of truth: DEFAULT_CONFIG, CONFIG_PATH, load/save_config, load_keys
  database.py             SQLite schema init (WAL + FTS5), DB_PATH = db/hunterjobs_ats.db
  schemas.py              Pydantic models for Gemma structured outputs
  runner_status.py        File-based IPC: pipeline status, PID, heartbeat (runner_status.json)
  embeddings.py           RAG: Gemini embeddings + sqlite-vec for "similar past applications"

pipeline/                 Brains + process control
  brain1.py               Three-stage pipeline: scrape → filter → research → outreach
  brain2.py               Market analyst: aggregates 7-day data → Gemini snapshot
  brain2_chat.py          Persistent Brain 2 chat with read-only DB tool access
  process_control.py      spawn_detached / kill_pid / _is_pid_alive / heartbeat thread
  run_brain1.py           Detached Brain 1 entry (python -m pipeline.run_brain1)
  run_brain2.py           Detached Brain 2 entry (python -m pipeline.run_brain2)

ui/                       NiceGUI frontend (FastAPI + Vue under the hood)
  theme.py                Logo + /static mount, COLOR_SWATCHES, PALETTE_CSS
  helpers.py              Pills, fmt_ts, status_dot_class, safe_notify, run_in_thread
  db_queries.py           Dashboard-side DB helpers (fetch_jobs, mark_applied, ...)
  jobs.py                 Job-row rendering + expandable sections
  tabs.py                 Applied / Market Analyzer / Logs / Setup tabs

config.json               User config (persisted by Setup tab)
keys.py                   API keys — gitignored, copy from keys_dummy.py
```

## Brain 1 pipeline

| Stage | Input | Model | Output |
|-------|-------|-------|--------|
| 1 | All scraped listings | Gemma 4 (free) | GOOD / MAYBE / BAD verdict |
| 2 | GOOD jobs only | Gemma 4 | Company OSINT: size, real stack, hiring signal, culture flags. Auto-demotes to BAD if staffing/labeling agency detected. |
| 3 | Stage 2 survivors | Gemma 4 | GitHub OSINT → email permutation → outreach draft |

Status heartbeat written to `runner_status.json` after every job so the dashboard can poll progress. A watchdog thread hard-kills the process if the dashboard heartbeat dies >90s.

## Brain 2 (Market Analyzer + Chat)

- **Snapshot**: aggregates last 7 days of jobs, calls chosen backend, writes to `market_snapshots` table.
- **Chat**: persistent conversation across sessions stored in `brain2_messages` table. Read-only SQL tool (`query_jobs`) gives the model live access to the jobs table.

## Supported LLM backends

| Key | Used by | Notes |
|-----|---------|-------|
| `gemma` | Brain 1 + Brain 2 | Free tier on Google AI Studio. No web grounding. |
| `gemini` | Brain 2 | Paid. Has Google Search grounding for snapshots. |
| `anthropic` | Brain 2 | Paid. Claude Sonnet 4.6 recommended. |
| `openai` | Brain 2 | Paid. GPT-5.5 default. |
| `lmstudio` | Brain 1 + Brain 2 | Local OpenAI-compatible endpoint. |

Brain 1 backends: `gemma` or `lmstudio` (per-stage config: `brain1_stage1_backend`, `brain1_stage23_backend`).

## Config keys (config.json)

Key defaults live in `DEFAULT_CONFIG` in `core/config.py`. Notable ones:

```
brain2_backend          gemini | gemma | anthropic | openai | lmstudio
brain2_gemini_model     gemini-3.5-flash | gemini-3.1-pro-preview
brain2_gemma_model      gemma-4-26b-a4b-it
brain2_anthropic_model  claude-sonnet-4-6 | claude-opus-4-7 | claude-haiku-4-5-20251001
brain2_openai_model     gpt-5.5 | gpt-5.4-mini | gpt-5.4-nano
brain2_lmstudio_url     http://localhost:1234/v1
salary_floor            monthly USD floor — jobs below this get BAD
hard_rejects            newline-separated substrings; matched case-insensitively against title+description
```

## DB schema (key tables)

```sql
jobs(
  id TEXT PK, title, company, domain, location,
  salary_min, salary_max, currency, source, url, description,
  date_posted, date_scraped, description_hash,
  verdict TEXT,           -- 'GOOD' | 'MAYBE' | 'BAD'
  reject_reason TEXT,     -- 'hard_reject:...', 'stage2_demoted_from_X:...', 'manual_bad'
  gemma1_done, gemma2_done, gemma3_done INTEGER,
  company_summary, hiring_signal, real_stack JSON, culture_flags JSON, company_size,
  contact_name, contact_title, contact_email, email_confidence, email_source,
  outreach_draft,
  applied INTEGER, applied_date,
  notes TEXT, row_color TEXT
)

market_snapshots(id, date, total_jobs, good_count, maybe_count, bad_count,
                 hard_reject_count, top_stacks JSON, salary_avg_min, salary_avg_max,
                 analysis, targeting_feedback)

brain2_messages(id, ts, role, content, backend, tool_calls, tool_name, tool_args, hidden)
```

## API keys (keys.py)

```python
GOOGLE_API_KEY    = ""   # required for Gemma/Gemini backends
ANTHROPIC_API_KEY = ""   # optional, Brain 2 Anthropic backend
OPENAI_API_KEY    = ""   # optional, Brain 2 OpenAI backend
GITHUB_PAT        = ""   # optional, Stage 3 GitHub OSINT for contact emails
```

## Dashboard DB helpers (ui/db_queries.py)

```
fetch_jobs(verdicts, query, limit)   filter + FTS search
mark_applied / unmark_applied        applied flag
update_notes                         per-job notes
update_row_color                     color label (purple/green/amber/red/blue/gray)
update_verdict(job_id, verdict, reason)  manual verdict override (e.g. "manual_bad")
```

## Tests

`tests/test_core.py` — 58 pure-logic unit tests. No LLM calls, no scraping, no UI.
Covers: `hard_reject_check`, `clean_domain`, `TokenBucket`, `_strip_json_fence`, SQL guard,
YC row mapping/remote filter, embeddings (cosine/rank/build-text), fallback job IDs, source selection.
Imports are package-qualified (`pipeline.brain1`, `core.embeddings`); no `sys.path` hack — relies on
`pip install -e .`. CI: `.github/workflows/test.yml` — Python 3.10 / 3.11 / 3.12 on push to main.

## v0.2 WIP status (as of 2026-05-28)

Completed this session:
- Move to BAD button on GOOD/MAYBE job rows (`update_verdict` + red button in `_render_apply_button`)
- OpenAI backend for Brain 2 chat and snapshots (`_chat_openai` in brain2_chat.py, openai block in brain2.py, Setup UI, DEFAULT_CONFIG, keys_dummy.py)

Remaining v0.2 items (from README):
- YC "Work at a Startup" scraper
- RAG over past applications
- OpenAI backend for Brain 1 (currently Brain 2 only)

## Style notes

- NiceGUI v2 with gothic purple accent (`--accent: #9d6fff`). Geist font for UI, JetBrains Mono for code/logs.
- Dark/light theme toggle via `data-theme` attribute on `<html>`.
- All job rows rendered via `render_job_row` (`ui/jobs.py`) → expandable sections → notes + color swatch + action buttons.
- `safe_notify()` (`ui/helpers.py`) wraps `ui.notify` to not crash when the slot is destroyed during async refresh.
- Brain processes are fully detached (`spawn_detached` in `pipeline/process_control.py`) so browser refresh doesn't kill them.
