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
import time

import pytest

# brain1 / brain2_chat import LLM SDKs at module load. If those aren't
# installed in the test environment, skip rather than error — CI installs
# them via requirements.txt so they'll be present there.
brain1 = pytest.importorskip("brain1")
brain2_chat = pytest.importorskip("brain2_chat")
embeddings = pytest.importorskip("embeddings")


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
