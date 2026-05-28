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
