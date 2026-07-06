"""
tests/test_ledger.py

First-seen ledger (core/ledger.py) against an in-memory DB. The invariant under
test is the money-saver: a job identity is judged at most once, regardless of
how many scans re-sight it or how its upstream date wobbles.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import core.ledger as ledger
from core.database import JOBS_TABLE, SEEN_JOBS_TABLE, SEEN_JOBS_INDEX


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(JOBS_TABLE)
    c.execute(SEEN_JOBS_TABLE)
    c.execute(SEEN_JOBS_INDEX)
    yield c
    c.close()


class TestUpsertSeen:
    def test_first_sighting_is_new(self, conn):
        assert ledger.upsert_seen(conn, "j1", "yc") is True

    def test_resighting_is_not_new_and_keeps_first_seen(self, conn):
        ledger.upsert_seen(conn, "j1", "yc", "2026-07-01T00:00:00+00:00")
        assert ledger.upsert_seen(conn, "j1", "yc",
                                  "2026-07-02T00:00:00+00:00") is False
        row = conn.execute("SELECT * FROM seen_jobs WHERE job_key='j1'").fetchone()
        assert row["first_seen_at"] == "2026-07-01T00:00:00+00:00"
        assert row["last_seen_at"] == "2026-07-02T00:00:00+00:00"

    def test_resighting_revives_expired_row(self, conn):
        ledger.upsert_seen(conn, "j1", "yc", "2026-01-01T00:00:00+00:00")
        ledger.prune_expired(conn, 60, now=datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert conn.execute(
            "SELECT expired_at FROM seen_jobs WHERE job_key='j1'"
        ).fetchone()["expired_at"] is not None
        ledger.upsert_seen(conn, "j1", "yc", "2026-07-02T00:00:00+00:00")
        assert conn.execute(
            "SELECT expired_at FROM seen_jobs WHERE job_key='j1'"
        ).fetchone()["expired_at"] is None


class TestJudged:
    def test_mark_and_query(self, conn):
        ledger.upsert_seen(conn, "j1", "hn")
        assert ledger.is_judged(conn, "j1") is False
        ledger.mark_judged(conn, "j1")
        assert ledger.is_judged(conn, "j1") is True

    def test_judged_at_not_overwritten(self, conn):
        ledger.upsert_seen(conn, "j1", "hn")
        ledger.mark_judged(conn, "j1", "2026-07-01T00:00:00+00:00")
        ledger.mark_judged(conn, "j1", "2026-07-04T00:00:00+00:00")
        assert conn.execute(
            "SELECT judged_at FROM seen_jobs WHERE job_key='j1'"
        ).fetchone()["judged_at"] == "2026-07-01T00:00:00+00:00"

    def test_unknown_key_is_not_judged(self, conn):
        assert ledger.is_judged(conn, "ghost") is False


class TestPrune:
    def test_old_rows_expire_recent_survive(self, conn):
        now = datetime(2026, 7, 5, tzinfo=timezone.utc)
        ledger.upsert_seen(conn, "old", "yc",
                           (now - timedelta(days=90)).isoformat())
        ledger.upsert_seen(conn, "fresh", "yc",
                           (now - timedelta(days=5)).isoformat())
        assert ledger.prune_expired(conn, 60, now=now) == 1
        rows = {r["job_key"]: r["expired_at"]
                for r in conn.execute("SELECT * FROM seen_jobs")}
        assert rows["old"] is not None and rows["fresh"] is None

    def test_zero_days_disables(self, conn):
        ledger.upsert_seen(conn, "old", "yc", "2020-01-01T00:00:00+00:00")
        assert ledger.prune_expired(conn, 0) == 0

    def test_idempotent(self, conn):
        now = datetime(2026, 7, 5, tzinfo=timezone.utc)
        ledger.upsert_seen(conn, "old", "yc",
                           (now - timedelta(days=90)).isoformat())
        assert ledger.prune_expired(conn, 60, now=now) == 1
        assert ledger.prune_expired(conn, 60, now=now) == 0


class TestBackfill:
    def test_backfill_seeds_from_jobs_history(self, conn):
        conn.execute(
            "INSERT INTO jobs (id, source, date_scraped, gemma1_done) "
            "VALUES ('a', 'yc', '2026-06-01T00:00:00+00:00', 1)"
        )
        conn.execute(
            "INSERT INTO jobs (id, source, date_scraped, gemma1_done) "
            "VALUES ('b', 'hn', '', 0)"
        )
        n = ledger.backfill_from_jobs(conn, "2026-07-05T00:00:00+00:00")
        assert n == 2
        a = conn.execute("SELECT * FROM seen_jobs WHERE job_key='a'").fetchone()
        assert a["first_seen_at"] == "2026-06-01T00:00:00+00:00"
        assert a["judged_at"] == "2026-06-01T00:00:00+00:00"
        b = conn.execute("SELECT * FROM seen_jobs WHERE job_key='b'").fetchone()
        assert b["first_seen_at"] == "2026-07-05T00:00:00+00:00"
        assert b["judged_at"] is None

    def test_backfill_is_idempotent_and_preserves_ledger(self, conn):
        conn.execute("INSERT INTO jobs (id, source, gemma1_done) VALUES ('a','yc',1)")
        ledger.backfill_from_jobs(conn, "2026-07-01T00:00:00+00:00")
        assert ledger.backfill_from_jobs(conn, "2026-07-05T00:00:00+00:00") == 0
        assert conn.execute(
            "SELECT first_seen_at FROM seen_jobs WHERE job_key='a'"
        ).fetchone()["first_seen_at"] == "2026-07-01T00:00:00+00:00"
