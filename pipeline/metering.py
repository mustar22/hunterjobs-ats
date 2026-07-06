"""
pipeline/metering.py

Per-scan Stage 1 budget + usage accounting (scan_usage table). Metered unit =
Stage 1 verdict; hard-rejects are free, Stage 2/3 recorded but never capped.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_FIELDS = ("scraped", "hard_rejected", "judged", "queued",
           "stage2_runs", "stage3_runs")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanMeter:
    """One per scan. Flushes eagerly so a crashed scan leaves an honest row."""

    def __init__(self, conn: sqlite3.Connection, cap: int,
                 close_zombies: bool = True):
        self.conn = conn
        self.cap = int(cap)  # <= 0 → unlimited
        self.counts = {f: 0 for f in _FIELDS}
        # a hard-killed scan (Stop button) never reaches finish(); close its
        # row. Single-process assumption — multi-user callers pass False.
        if close_zombies:
            conn.execute(
                "UPDATE scan_usage SET finished_at = ?, error = 'interrupted' "
                "WHERE finished_at IS NULL",
                (_now_iso(),),
            )
        cur = conn.execute(
            "INSERT INTO scan_usage (started_at, cap) VALUES (?, ?)",
            (_now_iso(), self.cap),
        )
        conn.commit()
        self.row_id = cur.lastrowid

    def can_judge(self) -> bool:
        return self.cap <= 0 or self.counts["judged"] < self.cap

    def count(self, field: str, n: int = 1) -> None:
        if field not in self.counts:
            raise ValueError(f"unknown usage field: {field}")
        self.counts[field] += n
        self._flush()

    def finish(self, error: str | None = None) -> None:
        self._flush()
        self.conn.execute(
            "UPDATE scan_usage SET finished_at = ?, error = ? WHERE id = ?",
            (_now_iso(), error, self.row_id),
        )
        self.conn.commit()

    def _flush(self) -> None:
        sets = ", ".join(f"{f} = ?" for f in _FIELDS)
        self.conn.execute(
            f"UPDATE scan_usage SET {sets} WHERE id = ?",
            (*(self.counts[f] for f in _FIELDS), self.row_id),
        )
        self.conn.commit()
