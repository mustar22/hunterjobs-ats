"""
tests/test_acceptance.py

End-to-end run_brain1 with LLM/scrape/status stubbed out — the metering +
ledger acceptance criteria: cap enforced, hard-rejects free, overflow QUEUED
and drained FIFO, repeat scans cost zero Stage 1 calls, usage row written.
"""

from __future__ import annotations

import sqlite3

import pytest

import core.database as database
import core.runner_status as runner_status
import pipeline.brain1 as brain1
from core.schemas import JobFilter


def _fake_rows(n_ok: int, n_reject: int):
    rows = []
    for i in range(n_ok + n_reject):
        bad = i < n_reject
        rows.append({
            "id": None,
            "title": f"Engineer {i}",
            "company": f"Co{i}",
            "location": "Remote",
            "job_type": "",
            "min_amount": None, "max_amount": None, "currency": "",
            "site": "yc",
            "job_url": f"https://jobs.example.com/co{i}/role",
            "description": ("REJECTME " if bad else "") + "Build things. " * 20,
            "date_posted": "2026-07-01",
            "date_posted_estimated": 0,
            "is_remote": True,
            "visa": "",
        })
    return rows


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + stubbed config/LLM/status. Returns a dict with knobs the
    tests mutate (cap, rows) and a judge-call recorder."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    state = {"cap": 10, "rows": [], "judged_ids": []}

    cfg = {
        "profile": "", "geo_eligibility": "",
        "search_terms": "", "hard_rejects": "REJECTME",
        "sources": [], "use_yc": True, "use_hn": False,
        "yc_remote_only": False, "yc_hours_old": 0,
        "use_rag": False,
        "results_wanted": 100, "hours_old": 72,
        "ledger_expire_days": 60,
    }
    monkeypatch.setattr(brain1, "load_config",
                        lambda: {**cfg, "max_llm_jobs_per_scan": state["cap"]})
    monkeypatch.setattr(brain1, "load_keys",
                        lambda: {"google": "", "github": "", "openrouter": ""})
    monkeypatch.setattr(brain1, "get_gemma_client_for_stage",
                        lambda *a, **k: (None, "stub", "stub"))
    monkeypatch.setattr(brain1, "safe_scrape_yc", lambda cfg: list(state["rows"]))

    def fake_filter(client, model, backend, description, profile, **kw):
        return JobFilter(verdict="MAYBE")

    def recording_filter(*a, **kw):
        # job id isn't passed to gemma1_filter; recorded in _judge via insert —
        # count calls here, ids are read back from the DB by the tests.
        state["judged_ids"].append(1)
        return fake_filter(*a, **kw)

    monkeypatch.setattr(brain1, "gemma1_filter", recording_filter)
    monkeypatch.setattr(runner_status, "dashboard_is_alive", lambda **kw: True)
    monkeypatch.setattr(runner_status, "start", lambda *a, **k: None)
    monkeypatch.setattr(runner_status, "patch", lambda *a, **k: None)
    monkeypatch.setattr(runner_status, "finish", lambda *a, **k: None)
    return state


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    return conn


def _counts(conn):
    return {
        "queued": conn.execute("SELECT COUNT(*) FROM jobs WHERE verdict='QUEUED'").fetchone()[0],
        "judged": conn.execute("SELECT COUNT(*) FROM jobs WHERE gemma1_done=1 AND reject_reason NOT LIKE 'hard_reject%'").fetchone()[0],
        "hard_rej": conn.execute("SELECT COUNT(*) FROM jobs WHERE reject_reason LIKE 'hard_reject%'").fetchone()[0],
    }


class TestCapAndQueue:
    def test_cap_judges_exactly_cap_hard_rejects_free(self, env, tmp_path):
        # acceptance #1: 40 scraped, 18 hard-rejectable, cap 10
        env["rows"] = _fake_rows(n_ok=22, n_reject=18)
        env["cap"] = 10
        brain1.run_brain1()
        conn = _db(tmp_path)
        c = _counts(conn)
        assert len(env["judged_ids"]) == 10
        assert c == {"queued": 12, "judged": 10, "hard_rej": 18}
        usage = conn.execute("SELECT * FROM scan_usage").fetchone()
        assert (usage["scraped"], usage["hard_rejected"], usage["judged"],
                usage["queued"], usage["cap"]) == (40, 18, 10, 12, 10)
        assert usage["finished_at"] is not None and usage["error"] is None

    def test_queue_drains_across_scans_until_dry(self, env, tmp_path):
        env["rows"] = _fake_rows(n_ok=22, n_reject=18)
        env["cap"] = 10
        brain1.run_brain1()          # judged 10, queued 12
        env["judged_ids"].clear()
        brain1.run_brain1()          # drains 10 queued, rescrape all skipped
        assert len(env["judged_ids"]) == 10
        env["judged_ids"].clear()
        brain1.run_brain1()          # drains last 2
        assert len(env["judged_ids"]) == 2
        env["judged_ids"].clear()
        brain1.run_brain1()          # nothing left anywhere
        assert len(env["judged_ids"]) == 0
        conn = _db(tmp_path)
        assert _counts(conn)["queued"] == 0
        assert conn.execute("SELECT COUNT(*) FROM scan_usage").fetchone()[0] == 4

    def test_queue_drained_fifo_by_date_scraped(self, env, tmp_path):
        # seed QUEUED rows directly with known scrape times, drain with cap 2
        env["rows"] = []
        env["cap"] = 0
        brain1.run_brain1()  # just creates the schema
        conn = _db(tmp_path)
        for jid, ts in (("late", "2026-07-03"), ("early", "2026-07-01"),
                        ("mid", "2026-07-02")):
            conn.execute(
                "INSERT INTO jobs (id, title, company, source, description, "
                "date_scraped, verdict, gemma1_done) "
                "VALUES (?, ?, 'C', 'yc', ?, ?, 'QUEUED', 0)",
                (jid, jid, "desc " * 30, ts))
        conn.commit()
        conn.close()
        env["cap"] = 2
        brain1.run_brain1()
        conn = _db(tmp_path)
        still_queued = [r["id"] for r in
                        conn.execute("SELECT id FROM jobs WHERE verdict='QUEUED'")]
        assert still_queued == ["late"]  # early+mid went first


class TestLedgerSavesRepeatCost:
    def test_second_identical_scan_is_free(self, env, tmp_path):
        # acceptance #2: no upstream changes -> zero Stage 1 calls on scan 2
        env["rows"] = _fake_rows(n_ok=5, n_reject=0)
        env["cap"] = 0
        brain1.run_brain1()
        assert len(env["judged_ids"]) == 5
        env["judged_ids"].clear()
        brain1.run_brain1()
        assert len(env["judged_ids"]) == 0

    def test_edited_description_does_not_rejudge(self, env, tmp_path):
        env["rows"] = _fake_rows(n_ok=3, n_reject=0)
        env["cap"] = 0
        brain1.run_brain1()
        env["judged_ids"].clear()
        for r in env["rows"]:
            r["description"] += " edited!"
        brain1.run_brain1()
        assert len(env["judged_ids"]) == 0

    def test_shifting_estimated_date_does_not_rejudge(self, env, tmp_path):
        # the WaaS leak, end to end: same jobs, date drifts a day between scans
        env["rows"] = _fake_rows(n_ok=4, n_reject=0)
        env["cap"] = 0
        brain1.run_brain1()
        env["judged_ids"].clear()
        for r in env["rows"]:
            r["date_posted"] = "2026-07-02"
            r["date_posted_estimated"] = 1
        brain1.run_brain1()
        assert len(env["judged_ids"]) == 0

    def test_ledger_rows_written(self, env, tmp_path):
        env["rows"] = _fake_rows(n_ok=2, n_reject=1)
        env["cap"] = 0
        brain1.run_brain1()
        conn = _db(tmp_path)
        rows = conn.execute("SELECT * FROM seen_jobs").fetchall()
        assert len(rows) == 3
        # hard-rejected rows are seen but count as judged=free? no LLM call ->
        # judged_at stays NULL for them
        judged = [r for r in rows if r["judged_at"] is not None]
        assert len(judged) == 2


class TestDrainOnlyRun:
    def test_no_sources_with_queue_drains_instead_of_exiting(self, env, tmp_path, monkeypatch):
        env["rows"] = _fake_rows(n_ok=8, n_reject=0)
        env["cap"] = 3
        brain1.run_brain1()          # 3 judged, 5 queued
        env["judged_ids"].clear()
        # all sources off -> must still drain the 5 leftovers
        real_cfg = brain1.load_config()
        monkeypatch.setattr(brain1, "load_config",
                            lambda: {**real_cfg, "use_yc": False,
                                     "max_llm_jobs_per_scan": 0})
        brain1.run_brain1()
        assert len(env["judged_ids"]) == 5
        conn = _db(tmp_path)
        assert _counts(conn)["queued"] == 0

    def test_no_sources_empty_queue_still_exits(self, env, monkeypatch):
        env["rows"] = []
        real_cfg = brain1.load_config()
        monkeypatch.setattr(brain1, "load_config",
                            lambda: {**real_cfg, "use_yc": False})
        brain1.run_brain1()          # must not crash, no scan work
        assert len(env["judged_ids"]) == 0
