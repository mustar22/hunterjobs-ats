"""
tests/test_metering.py

ScanMeter (pipeline/metering.py) against an in-memory scan_usage table.
Acceptance #5 lives here: one row per scan with correct counts.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.database import SCAN_USAGE_TABLE
from pipeline.metering import ScanMeter


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(SCAN_USAGE_TABLE)
    yield c
    c.close()


def _row(conn, meter):
    return conn.execute(
        "SELECT * FROM scan_usage WHERE id = ?", (meter.row_id,)
    ).fetchone()


class TestScanMeter:
    def test_creates_row_immediately(self, conn):
        m = ScanMeter(conn, cap=100)
        row = _row(conn, m)
        assert row["cap"] == 100 and row["started_at"] and row["finished_at"] is None

    def test_cap_gates_judging_only(self, conn):
        m = ScanMeter(conn, cap=2)
        for _ in range(2):
            assert m.can_judge()
            m.count("judged")
        assert not m.can_judge()
        # hard-rejects stay free after the cap — never gated.
        m.count("hard_rejected", 50)
        assert not m.can_judge()
        assert _row(conn, m)["hard_rejected"] == 50

    def test_zero_cap_means_unlimited(self, conn):
        m = ScanMeter(conn, cap=0)
        m.count("judged", 10_000)
        assert m.can_judge()

    def test_counts_flushed_eagerly_survive_crash(self, conn):
        # No finish() call — the row must already hold the counts.
        m = ScanMeter(conn, cap=100)
        m.count("scraped", 412)
        m.count("judged", 100)
        m.count("queued", 132)
        row = _row(conn, m)
        assert (row["scraped"], row["judged"], row["queued"]) == (412, 100, 132)
        assert row["finished_at"] is None

    def test_finish_records_end_and_error(self, conn):
        m = ScanMeter(conn, cap=100)
        m.count("stage2_runs", 7)
        m.count("stage3_runs", 5)
        m.finish(error="aborted: dashboard closed")
        row = _row(conn, m)
        assert row["finished_at"] is not None
        assert row["error"] == "aborted: dashboard closed"
        assert (row["stage2_runs"], row["stage3_runs"]) == (7, 5)

    def test_two_scans_two_rows(self, conn):
        ScanMeter(conn, cap=100).finish()
        ScanMeter(conn, cap=50).finish()
        assert conn.execute("SELECT COUNT(*) FROM scan_usage").fetchone()[0] == 2

    def test_unknown_field_rejected(self, conn):
        with pytest.raises(ValueError):
            ScanMeter(conn, cap=1).count("stage1_llm_calls")

    def test_new_scan_closes_zombie_rows_from_killed_scans(self, conn):
        m1 = ScanMeter(conn, cap=100)   # never finished (hard kill)
        m2 = ScanMeter(conn, cap=100)
        zombie = _row(conn, m1)
        assert zombie["finished_at"] is not None
        assert zombie["error"] == "interrupted"
        assert _row(conn, m2)["error"] is None
