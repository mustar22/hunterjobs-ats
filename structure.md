# HunterJobs — Code Structure

Quick map of what lives where and how a scan flows. For run/setup instructions
see README.md; for agent context see CLAUDE.md.

## Layout

```
dashboard.py            entry point — config/DB/heartbeat bootstrap, @ui.page("/"), server

core/                   leaf layer, no deps on pipeline/ui
  config.py             DEFAULT_CONFIG + config.json load/save, API key loading
  database.py           SQLite schema (WAL + FTS5), DB at core/db/hunterjobs_ats.db
  ledger.py             seen_jobs helpers — first/last sighting, judged flag, expiry
  schemas.py            Pydantic models for structured LLM outputs
  runner_status.py      file-based IPC (runner_status.json): state, PID, heartbeat
  embeddings.py         RAG — Gemini embeddings + sqlite-vec

pipeline/
  brain1.py             the scan: scrape → Stage 1 filter → Stage 2 research → Stage 3 outreach
  metering.py           ScanMeter — per-scan Stage 1 cap + scan_usage row
  sources/hn.py         HN "Who is hiring?" source (Algolia + Firebase, no auth)
  brain2.py             market analyst snapshot (7-day aggregate → LLM)
  brain2_chat.py        persistent analyst chat with read-only SQL tool
  process_control.py    detached process spawn/kill + heartbeat thread
  run_brain1.py         `python -m pipeline.run_brain1` (detached entry)
  run_brain2.py         same for brain2

ui/                     NiceGUI dashboard
  theme.py              logo, palette, CSS
  helpers.py            pills, fmt_ts, safe_notify, run_in_thread
  db_queries.py         dashboard-side queries (fetch_jobs, mark_applied, ...)
  jobs.py               job row rendering + brain1 status strip
  tabs.py               Applied / Market Analyzer / Logs / Setup tabs

scripts/
  setup.py              canonical install (editable HJ + YC scraper clone)
  migrate_v05.py        pre-v0.5 DB migration (id recompute, ledger backfill)
  wipe.py               clear scraped data
```

The YC scraper is a separate package (`ycombinator-jobs-scraper`), a stateless
fetcher — all state (dedup, freshness, metering) lives in HunterJobs' DB.

## How a scan flows (brain1)

1. **Drain**: QUEUED jobs from previous scans are judged first, FIFO by
   `date_scraped`, spending from this scan's cap.
2. **Scrape**: JobSpy terms (LinkedIn/Indeed), then YC, then HN. Every row goes
   through one choke point, `_process_row`:
   - ledger sighting (`seen_jobs`: first_seen/last_seen)
   - skip if the job id is already in the DB — known jobs are never re-judged,
     description edits just refresh the stored hash
   - hard-reject keywords → BAD, free (no LLM call, doesn't touch the cap)
   - cap reached → stored as `verdict='QUEUED'`, judged next scan
   - otherwise Stage 1 LLM verdict: GOOD / MAYBE / BAD
3. **Stage 2**: GOOD jobs get company research; staffing agencies demoted to BAD.
4. **Stage 3**: survivors get contact OSINT + outreach draft.
5. **Housekeeping**: ledger rows unseen for `ledger_expire_days` get
   `expired_at` (listing gone = probably filled); `scan_usage` row finalized.

## Identity & freshness rules

- Job id = native id where the source has one (JobSpy numeric, `hn_<comment>`),
  else `company_title_<sha1(url)[:8]>`. Dates are never part of the id — WaaS
  dates are scrape-time estimates that drift daily.
- `date_posted_estimated=1` marks WaaS dates; they display with `~` and are
  ignored by the freshness window. Real dates (Greenhouse/Lever/Ashby, HN
  comment time) still filter; HN compares at hour precision.
- "New" = never judged, per the ledger. Not the posting date.

## Metering (the SaaS primitive)

`max_llm_jobs_per_scan` (default 100, 0 = off) caps Stage 1 LLM verdicts per
scan. Hard-rejects are free; Stage 2/3 are recorded but uncapped. Every scan
writes a `scan_usage` row (scraped / hard_rejected / judged / queued /
stage2_runs / stage3_runs / cap / error) — the billing hook for the hosted
version; a `user_id` column is a later `ALTER TABLE`.

## Tests

`pytest` from repo root, 94 tests, all offline: pure logic (`test_core.py`),
ledger + meter on in-memory DBs (`test_ledger.py`, `test_metering.py`), and
full stubbed `run_brain1` acceptance runs (`test_acceptance.py`).
