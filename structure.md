# HunterJobs — Code Structure

Quick map of what lives where and how a scan flows. For run/setup instructions
see README.md.

## Layout

```
dashboard.py            entry point — config/DB/heartbeat bootstrap, @ui.page("/"), server

core/                   leaf layer, no deps on pipeline/ui
  config.py             DEFAULT_CONFIG + config.json load/save, API key loading
  database.py           SQLite schema (WAL + FTS5), DB at core/db/hunterjobs_ats.db
  ledger.py             seen_jobs helpers — sightings, judged flag, expiry, census diff
  companies.py          company intel cache (companies table) — key/get/save, TTL
  websearch.py          pluggable web search: Tavily/Serper when keyed, ddgs fallback
  schemas.py            Pydantic models for structured LLM outputs
  runner_status.py      file-based IPC (runner_status.json): state, PID, heartbeat
  embeddings.py         RAG — Gemini embeddings + sqlite-vec

pipeline/
  brain1.py             the scan: scrape → Stage 1 filter → enrichment
  enrich.py             per-company research + contacts in ONE LLM call, cache-first
  metering.py           ScanMeter — per-scan Stage 1 cap + scan_usage row
  listing_pulse.py      opt-in liveness check — asks listings if they still exist
  sources/hn.py         HN "Who is hiring?" source (Algolia + Firebase, no auth)
  sources/linkedin.py   LinkedIn source — time-window enumeration, no keywords needed
  sources/hh.py         HeadHunter (CIS) source — no API key, region-selected
  brain2.py             market analyst snapshot (7-day aggregate → LLM)
  brain2_chat.py        persistent analyst chat with read-only SQL tool
  process_control.py    detached process spawn/kill + heartbeat thread
  run_brain1.py         `python -m pipeline.run_brain1` (detached entry)
  run_brain2.py         same for brain2
  run_scrape.py         scrape only — everything lands QUEUED, judged later
  run_enrich.py         enrich only — research companies, no scraping/judging
  run_pulse.py          `python -m pipeline.run_pulse <sources> <limit>` (detached)

ui/                     NiceGUI dashboard
  theme.py              logo, palette, CSS
  helpers.py            pills, fmt_ts, safe_notify, run_in_thread
  db_queries.py         dashboard-side queries (fetch_jobs, mark_applied, ...)
  jobs.py               job row rendering (incl. the server-vs-yours intel split)
  companies.py          Companies tab — searchable grid, grows on scroll
  tabs.py               Applied / Market Analyzer / Logs / Setup tabs

scripts/
  setup.py              canonical install (editable HJ + YC scraper clone)
  migrate_v05.py        pre-v0.5 DB migration (id recompute, ledger backfill)
  import_seed.py        pull the free company research + YC/HN listings
  wipe.py               clear scraped data
```

The YC scraper is a separate package (`ycombinator-jobs-scraper`), a stateless
fetcher — all state (dedup, freshness, metering) lives in HunterJobs' DB.

## How a scan flows (brain1)

1. **Drain**: QUEUED jobs from previous scans are judged first, FIFO by
   `date_scraped`, spending from this scan's cap.
2. **Scrape**: LinkedIn terms, then YC, then HN, then hh. Every row goes
   through one choke point, `_process_row`:
   - ledger sighting (`seen_jobs`: first_seen/last_seen)
   - skip if the job id is already in the DB — known jobs are never re-judged,
     description edits just refresh the stored hash
   - hard-reject keywords → BAD, free (no LLM call, doesn't touch the cap)
   - cap reached → stored as `verdict='QUEUED'`, judged next scan
   - otherwise Stage 1 LLM verdict: GOOD / MAYBE / BAD
3. **Enrichment** (replaces the old Stage 2/3 split): per GOOD job, one
   cache-first company pass — posting-baked emails (free, skips the hunt),
   YC profile founders + descriptions, company site (homepage → /about →
   search-snippet fallback), team-page crawl, GitHub org, then ONE LLM call
   producing research + extracted people. Cached in `companies` (TTL
   `company_ttl_days`, default 30d), so N jobs at one company cost one pass.
   If nothing is cached locally, the imported `companies_seed` table is
   checked before paying for research — the contact hunt still runs, since
   contacts are never seeded. Staffing agencies still demoted to BAD.
4. **Housekeeping**: ledger rows unseen for `ledger_expire_days` get
   `expired_at` (listing gone = probably filled); `scan_usage` row finalized.

## Identity & freshness rules

- Job id = native id where the source has one (`li-<id>`, `hh-<id>`, `hn_<comment>`),
  else `company_title_<sha1(url)[:8]>`. Dates are never part of the id — WaaS
  dates are scrape-time estimates that drift daily.
- `date_posted_estimated=1` marks WaaS dates. They are never displayed: the
  row shows "listed <first_seen>" instead, because a back-computed date from a
  rounded relative age comes out as "2021" and that is not a fact we can
  defend. They are ignored by the freshness window too. Real dates
  (Greenhouse/Lever/Ashby, HN comment time) display and filter normally.
- "New" = never judged, per the ledger. Not the posting date.

## Metering (the SaaS primitive)

`max_llm_jobs_per_scan` (default 100, 0 = off) caps Stage 1 LLM verdicts per
scan. Hard-rejects are free; enrichment is recorded but uncapped. Every scan
writes a `scan_usage` row (scraped / hard_rejected / judged / queued /
stage2_runs / stage3_runs / cap / error) — the billing hook for the hosted
version; a `user_id` column is a later `ALTER TABLE`.

## Tests

`pytest` from repo root, 112 tests, all offline: pure logic (`test_core.py`),
ledger + meter on in-memory DBs (`test_ledger.py`, `test_metering.py`), and
full stubbed `run_brain1` acceptance runs (`test_acceptance.py`) and enrichment
cache tests (`test_enrich.py`).
