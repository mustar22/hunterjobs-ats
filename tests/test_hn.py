"""HN thread scraping — mostly about not losing listings quietly.

The cap trimmed a 311-comment thread to 200, failed fetches returned None, and
unparseable comments vanished. Three silent drops behind one number: a run
reported 92 when the thread held 187, and nothing complained.
"""

import logging

import pytest

from pipeline.sources import hn


# parse_comment drops anything under 100 chars, so a stub has to be realistic
_BODY = ("Acme | Backend Engineer | Remote | acme.com | We are hiring a backend "
         "engineer to work on distributed systems in Python and Go. Full time, "
         "remote within Europe. Email jobs@acme.com to apply.")


def _comment(cid, body=_BODY):
    return {"id": cid, "type": "comment", "text": body, "by": "someone",
            "time": 1750000000}


@pytest.fixture
def thread(monkeypatch):
    monkeypatch.setattr(hn, "find_latest_hiring_thread",
                        lambda **kw: {"id": 1, "title": "Ask HN: Who is hiring?",
                                      "date": "2026-07-01"})


class TestNoSilentTruncation:
    def test_warns_and_says_how_many_it_dropped(self, thread, monkeypatch, caplog):
        monkeypatch.setattr(hn, "fetch_thread_comments", lambda *a, **k: list(range(311)))
        monkeypatch.setattr(hn, "_fetch_many",
                            lambda ids, s: [_comment(i) for i in ids])
        with caplog.at_level(logging.WARNING):
            hn.scrape_hn_jobs({"hn_max_jobs": 200})
        assert any("DROPPING 111" in r.message for r in caplog.records)

    def test_a_big_cap_reads_the_whole_thread(self, thread, monkeypatch):
        monkeypatch.setattr(hn, "fetch_thread_comments", lambda *a, **k: list(range(311)))
        monkeypatch.setattr(hn, "_fetch_many",
                            lambda ids, s: [_comment(i) for i in ids])
        assert len(hn.scrape_hn_jobs({"hn_max_jobs": 1000})) == 311


class TestFetchFailures:
    def test_a_failed_fetch_is_retried_once(self, thread, monkeypatch):
        monkeypatch.setattr(hn, "fetch_thread_comments", lambda *a, **k: [1, 2, 3])
        calls = {"n": 0}

        def flaky(ids, session):
            calls["n"] += 1
            if calls["n"] == 1:               # first pass drops one
                return [_comment(1), None, _comment(3)]
            return [_comment(i) for i in ids]  # retry succeeds

        monkeypatch.setattr(hn, "_fetch_many", flaky)
        assert len(hn.scrape_hn_jobs({"hn_max_jobs": 1000})) == 3
        assert calls["n"] == 2

    def test_heavy_loss_is_an_error_not_a_shrug(self, thread, monkeypatch, caplog):
        monkeypatch.setattr(hn, "fetch_thread_comments", lambda *a, **k: list(range(10)))
        monkeypatch.setattr(hn, "_fetch_many",
                            lambda ids, s: [None] * len(ids))
        with caplog.at_level(logging.ERROR):
            hn.scrape_hn_jobs({"hn_max_jobs": 1000})
        assert any("don't trust the count" in r.message for r in caplog.records)

    def test_the_funnel_is_logged(self, thread, monkeypatch, caplog):
        monkeypatch.setattr(hn, "fetch_thread_comments", lambda *a, **k: [1, 2])
        monkeypatch.setattr(hn, "_fetch_many",
                            lambda ids, s: [_comment(1), {"id": 2, "text": ""}])
        with caplog.at_level(logging.INFO):
            hn.scrape_hn_jobs({"hn_max_jobs": 1000})
        assert any("2 ids ->" in r.message and "unparseable" in r.message
                   for r in caplog.records)
