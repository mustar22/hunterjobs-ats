"""
Core unit tests for HunterJobs ATS.

These cover the pure-logic functions where a silent bug would quietly
corrupt results: the keyword blacklist, domain cleaning, the rate-limiter
capacity cap, JSON fence stripping, and the read-only SQL guard.

They deliberately do NOT test LLM calls, scraping, or the UI — those need
external services and aren't deterministic.

Run from the repo root:
    pytest -v
"""

import json
from datetime import datetime, timezone
import time

import pytest

# brain1 / brain2_chat import LLM SDKs at module load. If those aren't
# installed in the test environment, skip rather than error — CI installs
# them via requirements.txt so they'll be present there.
brain1 = pytest.importorskip("pipeline.brain1")
brain2_chat = pytest.importorskip("pipeline.brain2_chat")
embeddings = pytest.importorskip("core.embeddings")


# ── hard_reject_check ─────────────────────────────────────────────────────────
class TestHardRejectCheck:
    def test_matches_keyword(self):
        assert brain1.hard_reject_check(
            "Senior Engineer, US citizenship required", ["US citizenship"]
        ) == "US citizenship"

    def test_no_match_returns_none(self):
        assert brain1.hard_reject_check(
            "Remote ML role", ["US citizenship", "W2 only"]
        ) is None

    def test_case_insensitive(self):
        assert brain1.hard_reject_check(
            "W2 ONLY position", ["w2 only"]
        ) == "w2 only"

    def test_empty_reject_list(self):
        assert brain1.hard_reject_check("anything goes here", []) is None

    def test_first_match_wins(self):
        # returns the first keyword in the list that matches
        result = brain1.hard_reject_check(
            "needs security clearance and US citizenship",
            ["US citizenship", "security clearance"],
        )
        assert result == "US citizenship"


# ── clean_domain ──────────────────────────────────────────────────────────────
class TestCleanDomain:
    def test_strips_linkedin(self):
        assert brain1.clean_domain("https://www.linkedin.com/company/foo") == ""

    def test_strips_linkedin_subdomain(self):
        assert brain1.clean_domain("https://uk.linkedin.com/jobs/123") == ""

    def test_keeps_real_domain(self):
        assert brain1.clean_domain("https://evernote.com/jobs") == "evernote.com"

    def test_strips_www(self):
        assert brain1.clean_domain("https://www.tesla.com/careers") == "tesla.com"

    def test_junk_string_nan(self):
        assert brain1.clean_domain("nan") == ""

    def test_empty_string(self):
        assert brain1.clean_domain("") == ""

    def test_none(self):
        assert brain1.clean_domain(None) == ""

    def test_no_dot_rejected(self):
        assert brain1.clean_domain("notadomain") == ""

    def test_other_job_boards_rejected(self):
        for board in ("indeed.com", "glassdoor.com", "ziprecruiter.com",
                      "wellfound.com", "ycombinator.com"):
            assert brain1.clean_domain(f"https://{board}/x") == "", board


# ── TokenBucket ───────────────────────────────────────────────────────────────
class TestTokenBucket:
    def test_capacity_cap_prevents_infinite_loop(self):
        # Requesting more than capacity must be capped, not loop forever.
        b = brain1.TokenBucket(tokens_per_minute=14_000)
        start = time.monotonic()
        b.consume(50_000)  # way over capacity
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, "over-capacity consume should return ~instantly"

    def test_consume_within_capacity_is_instant(self):
        b = brain1.TokenBucket(tokens_per_minute=14_000)
        start = time.monotonic()
        b.consume(1_000)  # bucket starts full
        assert time.monotonic() - start < 0.2

    def test_draining_reduces_tokens(self):
        b = brain1.TokenBucket(tokens_per_minute=14_000)
        b.consume(10_000)
        assert b.tokens < 4_001  # ~4000 left (minus a sliver of refill)


# ── _strip_json_fence ─────────────────────────────────────────────────────────
class TestStripJsonFence:
    def test_strips_json_fence(self):
        assert brain1._strip_json_fence('```json\n{"a":1}\n```') == '{"a":1}'

    def test_strips_bare_fence(self):
        assert brain1._strip_json_fence('```\n{"a":1}\n```') == '{"a":1}'

    def test_passthrough_clean_json(self):
        assert brain1._strip_json_fence('{"a":1}') == '{"a":1}'

    def test_strips_trailing_fence_only(self):
        # the specific Gemma failure mode we hit in production
        assert brain1._strip_json_fence('{"name":"x"}\n```') == '{"name":"x"}'

    def test_empty(self):
        assert brain1._strip_json_fence('') == ''


# ── SQL safety guard (run_query_jobs_tool) ────────────────────────────────────
# These all hit the guard and return BEFORE any DB access, so no DB needed.
class TestSqlGuard:
    def _err(self, sql):
        return json.loads(brain2_chat.run_query_jobs_tool(sql)).get("error")

    def test_blocks_update(self):
        assert self._err("UPDATE jobs SET verdict='BAD'")

    def test_blocks_drop(self):
        assert self._err("DROP TABLE jobs")

    def test_blocks_delete(self):
        assert self._err("DELETE FROM jobs")

    def test_blocks_insert(self):
        assert self._err("INSERT INTO jobs VALUES (1)")

    def test_blocks_multi_statement(self):
        assert self._err("SELECT 1; DELETE FROM jobs")

    def test_blocks_empty(self):
        assert self._err("")

    def test_blocks_whitespace_only(self):
        assert self._err("   ")

    def test_blocks_non_select_leading(self):
        assert self._err("WITH x AS (SELECT 1) DELETE FROM jobs")


# ── YC source: scrape_yc_jobs output -> JobSpy-style pipeline rows ─────────────
# The YC scraper is mocked; we only verify the field mapping that lets YC
# listings flow through the same Stage 1 path as LinkedIn/Indeed.
class TestYcJobsToRows:
    SAMPLE = {
        "title": "Founding ML Engineer",
        "company": "Acme AI",
        "location": "Remote (US)",
        "job_url": "https://jobs.example.com/acme/ml",
        "job_type": "fulltime",
        "is_remote": True,
        "description": "Build LLM pipelines. " * 20,
        "date_posted": "2026-05-30",
        "batch": "W25",
        "team_size": 8,
        "company_website": "https://acme.ai",
        "ats": "greenhouse",
    }

    def test_maps_jobspy_compatible_fields(self):
        rows = brain1.yc_jobs_to_rows([self.SAMPLE])
        assert len(rows) == 1
        r = rows[0]
        assert r["title"] == "Founding ML Engineer"
        assert r["company"] == "Acme AI"
        assert r["location"] == "Remote (US)"
        assert r["job_url"] == "https://jobs.example.com/acme/ml"
        assert r["description"].startswith("Build LLM pipelines.")
        assert r["date_posted"] == "2026-05-30"
        # YC-specific mapping: tagged source + website -> domain field, no salary.
        assert r["site"] == "yc"
        assert r["company_url_direct"] == "https://acme.ai"
        assert r["min_amount"] is None and r["max_amount"] is None
        # id left None so downstream builds a stable fallback id.
        assert r["id"] is None

    def test_missing_fields_default_to_empty(self):
        rows = brain1.yc_jobs_to_rows([{"title": "X"}])
        r = rows[0]
        assert r["company"] == "" and r["job_url"] == "" and r["description"] == ""
        assert r["site"] == "yc"

    def test_safe_scrape_yc_uses_config_params(self, monkeypatch):
        captured = {}

        def fake_scrape_yc_jobs(**kwargs):
            captured.update(kwargs)
            return [self.SAMPLE]

        # Patch the symbol where safe_scrape_yc imports it from.
        import sys, types
        mod = types.ModuleType("ycombinator_jobs_scraper")
        mod.scrape_yc_jobs = fake_scrape_yc_jobs
        monkeypatch.setitem(sys.modules, "ycombinator_jobs_scraper", mod)

        cfg = {"use_yc": True, "yc_max_companies": 42,
               "yc_max_team_size": 15, "yc_years_back": 2}
        rows = brain1.safe_scrape_yc(cfg)
        assert captured["max_companies"] == 42
        assert captured["max_team_size"] == 15
        assert captured["years_back"] == 2
        assert rows[0]["site"] == "yc"

    def test_safe_scrape_yc_swallows_errors(self, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("network down")

        import sys, types
        mod = types.ModuleType("ycombinator_jobs_scraper")
        mod.scrape_yc_jobs = boom
        monkeypatch.setitem(sys.modules, "ycombinator_jobs_scraper", mod)
        # A YC failure must be non-fatal: returns [] rather than raising.
        assert brain1.safe_scrape_yc({"use_yc": True}) == []

    def test_to_rows_preserves_tristate_is_remote(self):
        rows = brain1.yc_jobs_to_rows([
            {"title": "a", "is_remote": True},
            {"title": "b", "is_remote": False},
            {"title": "c"},  # missing -> None
        ])
        assert [r["is_remote"] for r in rows] == [True, False, None]


# ── YC remote-only filter (applied before Stage 1) ────────────────────────────
class TestYcRemoteFilter:
    ROWS = [
        {"title": "remote", "is_remote": True},
        {"title": "onsite", "is_remote": False},
        {"title": "unknown", "is_remote": None},
        {"title": "missing"},  # no is_remote key
    ]

    def test_drops_only_explicit_false(self):
        kept = brain1.apply_yc_remote_filter(self.ROWS, remote_only=True)
        titles = [r["title"] for r in kept]
        # True and None/missing kept; only explicit False dropped.
        assert titles == ["remote", "unknown", "missing"]
        assert "onsite" not in titles

    def test_toggle_off_keeps_everything(self):
        kept = brain1.apply_yc_remote_filter(self.ROWS, remote_only=False)
        assert kept == self.ROWS


# ── RAG embeddings: build_embedding_text ──────────────────────────────────────
class TestBuildEmbeddingText:
    def test_format(self):
        job = {"title": "ML Engineer", "company": "Acme", "description": "Build models."}
        assert (
            embeddings.build_embedding_text(job)
            == "ML Engineer — Acme\nBuild models."
        )

    def test_truncates_description_to_2000(self):
        job = {"title": "T", "company": "C", "description": "x" * 5000}
        text = embeddings.build_embedding_text(job)
        assert text == "T — C\n" + "x" * 2000
        assert text.count("x") == 2000

    def test_missing_fields(self):
        # No fields at all — must not raise, produces the empty template.
        assert embeddings.build_embedding_text({}) == " — \n"

    def test_strips_surrounding_whitespace(self):
        job = {"title": "  ML  ", "company": " Acme ", "description": "  hi  "}
        assert embeddings.build_embedding_text(job) == "ML — Acme\nhi"


# ── RAG embeddings: top-3 retrieval (cosine ranking) ──────────────────────────
# The embedding API call is mocked — these stay pure and deterministic.
class TestRankBySimilarity:
    def _vectors(self):
        return {
            "query": [1.0, 0.0, 0.0],
            "a": [1.0, 0.0, 0.0],   # identical to query -> similarity 1.0
            "b": [0.9, 0.1, 0.0],   # close
            "c": [0.0, 1.0, 0.0],   # orthogonal -> 0.0
            "d": [0.0, 0.0, 1.0],   # orthogonal -> 0.0
        }

    def test_returns_top_3_highest_first(self, monkeypatch):
        vectors = self._vectors()
        # Mock the embedding call so no network / SDK is touched.
        monkeypatch.setattr(embeddings, "embed_text", lambda t: vectors[t])
        query = embeddings.embed_text("query")
        candidates = [
            {
                "id": k,
                "title": k.upper(),
                "company": "Co",
                "embedding": embeddings.embed_text(k),
            }
            for k in ("a", "b", "c", "d")
        ]
        result = embeddings.rank_by_similarity(query, candidates, top_k=3)
        assert len(result) == 3
        assert [r["id"] for r in result][:2] == ["a", "b"]
        assert result[0]["score"] >= result[1]["score"] >= result[2]["score"]
        assert result[0]["score"] == pytest.approx(1.0)

    def test_skips_candidates_without_embedding(self):
        query = [1.0, 0.0]
        candidates = [
            {"id": "x", "title": "X", "company": "Co", "embedding": [1.0, 0.0]},
            {"id": "y", "title": "Y", "company": "Co", "embedding": None},
            {"id": "z", "title": "Z", "company": "Co"},
        ]
        result = embeddings.rank_by_similarity(query, candidates, top_k=3)
        assert [r["id"] for r in result] == ["x"]

    def test_empty_query_returns_empty(self):
        assert embeddings.rank_by_similarity([], [{"id": "x", "embedding": [1.0]}]) == []


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert embeddings.cosine_similarity([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert embeddings.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_zero_vector_is_zero(self):
        assert embeddings.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_mismatched_lengths_is_zero(self):
        assert embeddings.cosine_similarity([1.0], [1.0, 2.0]) == 0.0


# ── RAG embeddings: end-to-end retrieval over a vec0 in-memory DB ─────────────
# Exercises the applied-only filter + self-exclusion against a real sqlite-vec
# table. Skipped automatically if the extension can't load.
class TestFindSimilarApplications:
    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        if not embeddings.database._load_vec_extension(conn):
            pytest.skip("sqlite-vec extension not available")
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, "
            "applied INTEGER DEFAULT 0)"
        )
        # Small-dim mirror of the real job_embeddings vec0 table; store/get/rank
        # are dimension-agnostic so 3-dim vectors keep the test readable.
        conn.execute(
            "CREATE VIRTUAL TABLE job_embeddings USING vec0("
            "job_id TEXT PRIMARY KEY, embedding float[3] distance_metric=cosine)"
        )
        return conn

    def _add(self, conn, jid, applied, vec):
        conn.execute(
            "INSERT INTO jobs (id, title, company, applied) VALUES (?,?,?,?)",
            (jid, jid.upper(), "Co", applied),
        )
        embeddings.store_embedding(conn, jid, vec)

    def test_top3_applied_only_and_self_excluded(self):
        conn = self._conn()
        self._add(conn, "cur", 0, [1.0, 0.0, 0.0])  # current job (not applied)
        self._add(conn, "a", 1, [1.0, 0.0, 0.0])    # applied, identical
        self._add(conn, "b", 1, [0.8, 0.2, 0.0])    # applied, close
        self._add(conn, "c", 1, [0.0, 1.0, 0.0])    # applied, orthogonal
        self._add(conn, "d", 1, [0.0, 0.0, 1.0])    # applied, orthogonal
        self._add(conn, "e", 0, [1.0, 0.0, 0.0])    # NOT applied -> excluded

        result = embeddings.find_similar_applications(conn, "cur", top_k=3)
        ids = [r["id"] for r in result]
        assert len(result) == 3
        assert ids[0] == "a"
        assert "cur" not in ids   # self excluded
        assert "e" not in ids     # non-applied excluded
        assert result[0]["score"] >= result[1]["score"] >= result[2]["score"]

    def test_current_job_without_embedding_returns_empty(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO jobs (id, title, company, applied) VALUES ('x','X','Co',0)"
        )
        # 'x' has no stored embedding -> quiet empty, no error.
        assert embeddings.find_similar_applications(conn, "x") == []

    def test_store_embedding_reembed_is_idempotent(self):
        # vec0 does not honor INSERT OR REPLACE: a naive re-insert of an existing
        # job_id raises "UNIQUE constraint failed on job_embeddings primary key".
        # store_embedding must instead replace cleanly (DELETE-then-INSERT), so a
        # re-embed updates the vector without error and leaves exactly one row.
        conn = self._conn()
        embeddings.store_embedding(conn, "j1", [1.0, 0.0, 0.0])
        embeddings.store_embedding(conn, "j1", [0.0, 1.0, 0.0])  # must not raise
        count = conn.execute(
            "SELECT count(*) FROM job_embeddings WHERE job_id = 'j1'"
        ).fetchone()[0]
        assert count == 1
        # The second vector wins.
        assert embeddings.get_embedding(conn, "j1") == [0.0, 1.0, 0.0]


# ── YC fallback id: distinct postings must not collide, ids must not drift ────
# YC listings have no native id, so brain1 derives one from company+title+url
# hash. date_posted is deliberately EXCLUDED: WaaS dates are scrape-time
# estimates that shift daily, and an id embedding them re-judges the same job
# on every scan.
class TestFallbackJobId:
    def test_distinct_postings_same_company_title_dont_collide(self):
        a = {"company": "Acme", "title": "Engineer", "date_posted": "2026-05-30",
             "job_url": "https://jobs.example.com/acme/eng-sf"}
        b = {"company": "Acme", "title": "Engineer", "date_posted": "2026-05-30",
             "job_url": "https://jobs.example.com/acme/eng-nyc"}
        assert brain1.fallback_job_id(a) != brain1.fallback_job_id(b)

    def test_same_posting_yields_same_id_across_runs(self):
        row = {"company": "Acme", "title": "Engineer", "date_posted": "2026-05-30",
               "job_url": "https://jobs.example.com/acme/eng-sf"}
        assert brain1.fallback_job_id(row) == brain1.fallback_job_id(dict(row))

    def test_shifting_estimated_date_does_not_change_id(self):
        # The WaaS re-judge leak: "5 months" → now()-based date that moves every
        # day. Same posting scraped on consecutive days must keep one id.
        day1 = {"company": "Acme", "title": "Engineer", "date_posted": "2026-01-01",
                "job_url": "https://www.ycombinator.com/companies/acme/jobs/x1"}
        day2 = {**day1, "date_posted": "2026-01-02"}
        assert brain1.fallback_job_id(day1) == brain1.fallback_job_id(day2)

    def test_no_url_falls_back_to_company_title_date(self):
        row = {"company": "Acme", "title": "Engineer", "date_posted": "2026-05-30",
               "job_url": ""}
        assert brain1.fallback_job_id(row) == "Acme_Engineer_2026-05-30"


# ── source selection: JobSpy sites vs. YC-only ────────────────────────────────
class TestSourceSelection:
    def test_empty_sources_with_yc_runs_yc_only_skips_jobspy(self):
        # YC-only run: nothing for JobSpy, but there IS something to scrape (YC).
        assert brain1.linkedin_enabled([]) is False
        assert brain1.has_scrape_source([], True) is True

    def test_empty_sources_without_yc_does_nothing(self):
        # No JobSpy sites and YC off: genuinely nothing to scrape (warning + exit).
        assert brain1.linkedin_enabled([]) is False
        assert brain1.has_scrape_source([], False) is False

    def test_linkedin_only_still_scrapes(self):
        assert brain1.linkedin_enabled(["linkedin"]) is True
        assert brain1.has_scrape_source(["linkedin"], False) is True

    def test_linkedin_and_yc_both_run(self):
        assert brain1.linkedin_enabled(["linkedin"]) is True
        assert brain1.has_scrape_source(["linkedin"], True) is True


# ── freshness window: real dates filter, estimated dates pass through ─────────
class TestDateFilter:
    NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)

    def _run(self, rows, hours):
        return brain1.apply_yc_date_filter(rows, hours, now=self.NOW,
                                           return_stats=True)

    def test_estimated_dates_bypass_the_window(self):
        # WaaS dates are fabricated — the ledger/cap govern them, not the window.
        rows = [{"date_posted": "2024-01-01", "date_posted_estimated": 1}]
        kept, stale, undated = self._run(rows, 72)
        assert kept == rows and stale == 0

    def test_real_stale_date_dropped(self):
        rows = [{"date_posted": "2026-01-01"}]
        kept, stale, undated = self._run(rows, 72)
        assert kept == [] and stale == 1

    def test_day_granular_inclusive_at_boundary(self):
        # cutoff is 2026-07-02 12:00; a bare date on the cutoff day stays.
        rows = [{"date_posted": "2026-07-02"}]
        kept, _, _ = self._run(rows, 72)
        assert kept == rows

    def test_hn_full_timestamp_compares_at_hour_precision(self):
        just_in = {"date_posted": "2026-07-02T13:00:00+00:00"}   # 71h old
        too_old = {"date_posted": "2026-07-02T11:00:00+00:00"}   # 73h old
        kept, stale, _ = self._run([just_in, too_old], 72)
        assert kept == [just_in] and stale == 1

    def test_undated_rows_dropped(self):
        kept, _, undated = self._run([{"date_posted": ""}], 72)
        assert kept == [] and undated == 1

    def test_zero_hours_disables(self):
        rows = [{"date_posted": "2020-01-01"}]
        assert brain1.apply_yc_date_filter(rows, 0) == rows


class TestYcEstimatedFlag:
    def test_waas_rows_flagged_ats_rows_not(self):
        rows = brain1.yc_jobs_to_rows([
            {"title": "A", "ats": "waas", "date_posted": "2026-06-01"},
            {"title": "B", "ats": "greenhouse", "date_posted": "2026-06-01"},
        ])
        assert rows[0]["date_posted_estimated"] == 1
        assert rows[1]["date_posted_estimated"] == 0


# ── fetch_jobs sort: newest found vs newest posted ─────────────────────────────
class TestFetchJobsSort:
    def _conn(self, monkeypatch, tmp_path):
        import core.database as database
        import ui.db_queries as q
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "s.db")
        database.init_db()
        conn = database.get_db_connection()
        rows = [("a", "2026-01-01", "2026-07-01T00:00:00+00:00"),
                ("b", "2026-06-01", "2026-07-03T00:00:00+00:00"),
                ("c", "",           "2026-07-02T00:00:00+00:00")]
        for jid, posted, seen in rows:
            conn.execute("INSERT INTO jobs (id, title, verdict, date_posted, "
                         "date_scraped) VALUES (?, ?, 'GOOD', ?, ?)",
                         (jid, jid, posted, seen))
            conn.execute("INSERT INTO seen_jobs (job_key, first_seen_at, "
                         "last_seen_at) VALUES (?, ?, ?)", (jid, seen, seen))
        conn.commit()
        conn.close()
        return q

    def test_newest_found_default(self, monkeypatch, tmp_path):
        q = self._conn(monkeypatch, tmp_path)
        assert [r["id"] for r in q.fetch_jobs(["GOOD"])] == ["b", "c", "a"]

    def test_newest_posted_undated_last(self, monkeypatch, tmp_path):
        q = self._conn(monkeypatch, tmp_path)
        got = [r["id"] for r in q.fetch_jobs(["GOOD"], sort="posted")]
        assert got == ["b", "a", "c"]


# ── v0.7: judged listing facts + rewritable evaluation brief ──────────────────
class TestJudgeV07:
    def test_jobfilter_new_fields_default(self):
        from core.schemas import JobFilter
        f = JobFilter(verdict="GOOD")
        assert f.work_mode == "unknown" and f.us_auth_required == "unclear"

    def test_jobfilter_rejects_offgrid_values(self):
        import pytest
        from pydantic import ValidationError
        from core.schemas import JobFilter
        with pytest.raises(ValidationError):
            JobFilter(verdict="GOOD", work_mode="maybe-remote")

    def test_migration_adds_v07_columns(self, tmp_path, monkeypatch):
        import core.database as database
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
        database.init_db()
        import sqlite3
        conn = sqlite3.connect(tmp_path / "t.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert {"work_mode", "us_auth_required"} <= cols
        conn.close()

    def _capture_system(self, monkeypatch):
        import pipeline.brain1 as b1
        captured = {}
        def fake_call(client, model, backend, system, prompt, schema, **kw):
            captured["system"] = system
            captured["prompt"] = prompt
            from core.schemas import JobFilter
            return JobFilter(verdict="MAYBE")
        monkeypatch.setattr(b1, "call_gemma", fake_call)
        return b1, captured

    def test_default_brief_and_contract_present(self, monkeypatch):
        b1, cap = self._capture_system(monkeypatch)
        b1.gemma1_filter(None, "m", "stub", "desc " * 30, "ml engineer")
        assert b1.DEFAULT_JUDGE_PROMPT in cap["system"]
        assert "OUTPUT CONTRACT" in cap["system"]
        assert "CANDIDATE PROFILE" in cap["system"]

    def test_custom_brief_replaces_default_contract_stays(self, monkeypatch):
        b1, cap = self._capture_system(monkeypatch)
        b1.gemma1_filter(None, "m", "stub", "desc " * 30, "",
                         judge_prompt="GOOD if the listing states a salary.")
        assert "GOOD if the listing states a salary." in cap["system"]
        assert b1.DEFAULT_JUDGE_PROMPT not in cap["system"]
        assert "OUTPUT CONTRACT" in cap["system"]
        # no profile given → no profile block in the prompt at all
        assert "CANDIDATE PROFILE" not in cap["system"]

    def test_anthropic_stage1_client_selection(self):
        import pipeline.brain1 as b1
        client, model, backend = b1.get_gemma_client_for_stage(
            {"brain1_stage1_backend": "anthropic",
             "brain1_anthropic_model": "claude-haiku-4-5"},
            {"google": "", "anthropic": "k", "openrouter": ""},
            "stage1")
        assert backend == "anthropic" and model == "claude-haiku-4-5"
        assert type(client).__name__ == "Anthropic"

    def test_reject_reason_truncated_on_insert(self, tmp_path, monkeypatch):
        import core.database as database
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
        database.init_db()
        import sqlite3
        import pipeline.brain1 as b1
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.row_factory = sqlite3.Row
        job = {c: "" for c in b1.JOB_INSERT_COLS}
        job.update(id="j1", title="t", company="c", description="d" * 200)
        b1.insert_job_with_verdict(conn, job, "BAD", "word " * 100)
        r = conn.execute("SELECT reject_reason FROM jobs WHERE id='j1'").fetchone()
        assert len(r["reject_reason"]) <= 161
        conn.close()


def test_yc_per_company_cap_stops_slug_collisions():
    from pipeline.brain1 import YC_MAX_JOBS_PER_COMPANY, yc_jobs_to_rows
    flood = [{"company": "Pulse", "title": f"Nurse {i}",
              "job_url": f"https://g.io/pulse/{i}", "description": "x" * 50}
             for i in range(YC_MAX_JOBS_PER_COMPANY + 40)]
    flood += [{"company": "CleanCo", "title": "Eng",
               "job_url": "https://g.io/clean/1", "description": "x" * 50}]
    rows = yc_jobs_to_rows(flood)
    pulse_rows = [r for r in rows if r["company"] == "Pulse"]
    assert len(pulse_rows) == YC_MAX_JOBS_PER_COMPANY
    assert any(r["company"] == "CleanCo" for r in rows)


def test_degenerate_catches_one_stuttered_word_in_normal_prose():
    from pipeline.brain1 import _is_degenerate
    # the whole-text ratios get diluted by the surrounding normal words, so a
    # single looped token has to be scored on its own (real MAI UK case)
    assert _is_degenerate("No information available for the company MAI UK. "
                          "Domain and specific business activities are "
                          "unidentifiableifiable from the")
    assert _is_degenerate("summary: recommendationsrecommendations for teams")
    # long legitimate words must not trip it
    for good in ("A telecommunications company focused on "
                 "internationalization and interoperability.",
                 "Provides personalization and recommendation systems.",
                 "Indistinguishable from professional implementation work."):
        assert not _is_degenerate(good), good


class TestPerStageModelPicking:
    """Stage 1 reads thousands of listings, enrichment runs rarely - they need
    different models. They used to share one key per backend, so the Setup
    advice ('light models for Stage 1') was impossible to follow off Google."""

    KEYS = {"google": "x", "anthropic": "y", "openrouter": "z"}

    def _model(self, cfg, stage):
        from pipeline.brain1 import get_gemma_client_for_stage
        return get_gemma_client_for_stage(cfg, self.KEYS, stage)[1]

    def test_each_stage_gets_its_own_claude(self):
        cfg = {"brain1_stage1_backend": "anthropic",
               "brain1_stage23_backend": "anthropic",
               "brain1_stage1_anthropic_model": "claude-haiku-4-5",
               "brain1_stage23_anthropic_model": "claude-sonnet-4-6"}
        assert self._model(cfg, "stage1") == "claude-haiku-4-5"
        assert self._model(cfg, "stage2") == "claude-sonnet-4-6"

    def test_old_shared_key_still_works(self):
        # existing installs must not silently lose their configured model
        cfg = {"brain1_stage1_backend": "anthropic",
               "brain1_stage23_backend": "anthropic",
               "brain1_anthropic_model": "claude-opus-4-1"}
        assert self._model(cfg, "stage1") == "claude-opus-4-1"
        assert self._model(cfg, "stage2") == "claude-opus-4-1"

    def test_per_stage_beats_the_shared_key(self):
        cfg = {"brain1_stage1_backend": "anthropic",
               "brain1_anthropic_model": "claude-opus-4-1",
               "brain1_stage1_anthropic_model": "claude-haiku-4-5"}
        assert self._model(cfg, "stage1") == "claude-haiku-4-5"

    def test_openrouter_is_per_stage_too(self):
        cfg = {"brain1_stage1_backend": "openrouter",
               "brain1_stage23_backend": "openrouter",
               "brain1_stage1_openrouter_model": "meta-llama/llama-3-8b",
               "brain1_stage23_openrouter_model": "anthropic/claude-3.5-sonnet"}
        assert self._model(cfg, "stage1") == "meta-llama/llama-3-8b"
        assert self._model(cfg, "stage2") == "anthropic/claude-3.5-sonnet"

    def test_falls_back_to_a_default_when_nothing_is_set(self):
        assert self._model({"brain1_stage1_backend": "openrouter"},
                           "stage1") == "openrouter/free"
        assert self._model({"brain1_stage1_backend": "anthropic"},
                           "stage1") == "claude-haiku-4-5"


class TestListingPulse:
    """Nothing gets buried on one bad answer, and never because a server was
    rude - only a repeated, explicit 'not found'."""

    def _conn(self, tmp_path, monkeypatch):
        import core.database as cdb
        monkeypatch.setattr(cdb, "DB_PATH", tmp_path / "p.db")
        cdb.init_db()
        return cdb.get_db_connection()

    def test_one_404_is_not_death(self, tmp_path, monkeypatch):
        from core import ledger
        import pipeline.listing_pulse as lp
        conn = self._conn(tmp_path, monkeypatch)
        ledger.upsert_seen(conn, "li-1", "linkedin")
        monkeypatch.setattr(lp, "_check_linkedin", lambda s, k: False)
        monkeypatch.setattr(lp.time, "sleep", lambda *_: None)
        lp.run_pulse(conn, sources=("linkedin",), limit=10)
        row = conn.execute("SELECT miss_count, expired_at FROM seen_jobs").fetchone()
        assert row["miss_count"] == 1 and row["expired_at"] is None

    def test_two_404s_bury_it(self, tmp_path, monkeypatch):
        from core import ledger
        import pipeline.listing_pulse as lp
        conn = self._conn(tmp_path, monkeypatch)
        ledger.upsert_seen(conn, "li-1", "linkedin")
        monkeypatch.setattr(lp, "_check_linkedin", lambda s, k: False)
        monkeypatch.setattr(lp.time, "sleep", lambda *_: None)
        lp.run_pulse(conn, sources=("linkedin",), limit=10)
        lp.run_pulse(conn, sources=("linkedin",), limit=10)
        assert conn.execute("SELECT expired_at FROM seen_jobs"
                            ).fetchone()["expired_at"] is not None

    def test_being_alive_clears_a_previous_miss(self, tmp_path, monkeypatch):
        from core import ledger
        import pipeline.listing_pulse as lp
        conn = self._conn(tmp_path, monkeypatch)
        ledger.upsert_seen(conn, "li-1", "linkedin")
        monkeypatch.setattr(lp.time, "sleep", lambda *_: None)
        monkeypatch.setattr(lp, "_check_linkedin", lambda s, k: False)
        lp.run_pulse(conn, sources=("linkedin",), limit=10)
        monkeypatch.setattr(lp, "_check_linkedin", lambda s, k: True)
        lp.run_pulse(conn, sources=("linkedin",), limit=10)
        assert conn.execute("SELECT miss_count FROM seen_jobs"
                            ).fetchone()["miss_count"] == 0

    def test_rate_limit_never_counts_as_death(self, tmp_path, monkeypatch):
        # a 429 or a timeout is 'ask again later', not 'deleted'
        from core import ledger
        import pipeline.listing_pulse as lp
        conn = self._conn(tmp_path, monkeypatch)
        ledger.upsert_seen(conn, "li-1", "linkedin")
        monkeypatch.setattr(lp, "_check_linkedin", lambda s, k: None)
        monkeypatch.setattr(lp.time, "sleep", lambda *_: None)
        for _ in range(5):
            lp.run_pulse(conn, sources=("linkedin",), limit=10)
        row = conn.execute("SELECT miss_count, expired_at, checked_at "
                           "FROM seen_jobs").fetchone()
        assert row["miss_count"] == 0 and row["expired_at"] is None
        assert row["checked_at"]        # still recorded that we looked

    def test_yc_is_refused_rather_than_faked(self, tmp_path, monkeypatch):
        from core import ledger
        import pipeline.listing_pulse as lp
        conn = self._conn(tmp_path, monkeypatch)
        ledger.upsert_seen(conn, "yc-1", "yc")
        assert lp.run_pulse(conn, sources=("yc",), limit=10) == {}
