"""HeadHunter source.

hh covers the CIS, which nothing else here reaches. Two things measured the
hard way and encoded below: its result order is not chronological, and its
pagination stops dead at page 40.
"""

from datetime import datetime, timedelta, timezone

import pipeline.sources.hh as hh

NOW = datetime.now(timezone.utc)


def _vac(vid, days_old=0, name="Python Dev", company="Acme",
         lo=None, hi=None, cur="RUR"):
    when = (NOW - timedelta(days=days_old)).isoformat()
    return {"vacancyId": vid, "name": name,
            "company": {"visibleName": company},
            "compensation": {"from": lo, "to": hi, "currencyCode": cur},
            "area": {"name": "Москва"}, "creationTime": when,
            "links": {"desktop": f"https://hh.uz/vacancy/{vid}"}}


def _pages(seq, monkeypatch):
    calls = {"n": 0, "params": []}

    def fake(session, url, params, timeout=25):
        calls["params"].append(dict(params))
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        status, items = seq[i]
        if status != "ok":
            return status, None
        return "ok", {"vacancySearchResult": {"vacancies": items}}

    monkeypatch.setattr(hh, "_state", fake)
    monkeypatch.setattr(hh.time, "sleep", lambda *_: None)
    return calls


class TestOutOfOrderResults:
    """hh's ordering is not chronological — order_by=publication_time returned
    an April listing ahead of July ones. Stopping at the first stale row would
    silently discard everything behind it."""

    def test_an_old_row_does_not_end_the_run(self, monkeypatch):
        _pages([("ok", [_vac(1, 400), _vac(2, 0), _vac(3, 0)]),
                ("ok", [])], monkeypatch)
        rows = hh.scrape_hh_jobs(hours=48, with_descriptions=False)
        assert {r["id"] for r in rows} == {"hh-2", "hh-3"}

    def test_stale_rows_are_counted_not_hidden(self, monkeypatch):
        _pages([("ok", [_vac(1, 400), _vac(2, 0)]), ("ok", [])], monkeypatch)
        stats = {}
        hh.scrape_hh_jobs(hours=48, with_descriptions=False, stats=stats)
        assert stats["stale"] == 1


class TestPagination:
    def test_server_side_window_is_sent(self, monkeypatch):
        calls = _pages([("ok", [])], monkeypatch)
        hh.scrape_hh_jobs(hours=72, with_descriptions=False)
        assert calls["params"][0]["search_period"] == 3

    def test_depth_cap_is_reported_as_partial(self, monkeypatch):
        _pages([("ok", [_vac(i) for i in range(50)])] * 45, monkeypatch)
        stats = {}
        hh.scrape_hh_jobs(hours=48, with_descriptions=False, stats=stats)
        assert "depth cap" in stats["reason"] and stats["complete"] is False

    def test_a_timeout_is_retried_before_giving_up(self, monkeypatch):
        calls = _pages([("error", None), ("ok", [_vac(1)]), ("ok", [])],
                       monkeypatch)
        rows = hh.scrape_hh_jobs(hours=48, with_descriptions=False)
        assert len(rows) == 1 and calls["n"] >= 2


class TestParsing:
    def test_salary_survives(self):
        row = hh.parse_vacancy(_vac(9, lo=150000, hi=250000))
        assert row["min_amount"] == 150000 and row["currency"] == "RUR"

    def test_id_is_namespaced(self):
        assert hh.parse_vacancy(_vac(135864896))["id"] == "hh-135864896"

    def test_area_name_maps_to_an_id(self, monkeypatch):
        calls = _pages([("ok", [])], monkeypatch)
        hh.scrape_hh_jobs(hours=24, area="russia", with_descriptions=False)
        assert calls["params"][0]["area"] == 113

    def test_no_id_is_dropped(self):
        assert hh.parse_vacancy({"name": "x"}) is None


class TestNoInventedDefaults:
    """Empty settings must mean "don't run", not "substitute something I made
    up". brain1 used to fall back to a hardcoded 'machine learning engineer
    remote' whenever search terms were blank."""

    def test_no_hardcoded_search_term_survives(self):
        import inspect
        import pipeline.brain1 as b1
        assert "machine learning engineer remote" not in inspect.getsource(b1)

    def test_hh_has_no_default_region(self):
        from core.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["hh_area"] == ""

    def test_hh_area_accepts_an_id_or_a_name(self):
        assert hh.AREAS["russia"] == 113
        assert hh.AREAS["uzbekistan"] == 97


class TestRemoteFlag:
    """hh names the work format explicitly. Reading the singular `workFormat`
    silently returned None on every row, so 32 of 51 genuinely remote listings
    came back as not-remote with no error anywhere."""

    def _row(self, elements):
        v = {"vacancyId": 1, "name": "Разработчик", "area": {"name": "Москва"}}
        if elements is not None:
            v["workFormats"] = [{"workFormatsElement": elements}]
        return hh.parse_vacancy(v)

    def test_the_plural_field_is_the_one_that_exists(self):
        assert hh.work_formats({"workFormats": [{"workFormatsElement": ["REMOTE"]}]}) == {"REMOTE"}
        assert hh.work_formats({"workFormat": "REMOTE"}) == set()

    def test_remote_is_read_from_the_real_field(self):
        assert self._row(["REMOTE"])["is_remote"] is True

    def test_remote_among_several_still_counts(self):
        assert self._row(["ON_SITE", "REMOTE", "HYBRID"])["is_remote"] is True

    def test_stated_onsite_is_false_not_unknown(self):
        assert self._row(["ON_SITE"])["is_remote"] is False

    def test_hybrid_alone_is_not_remote(self):
        assert self._row(["HYBRID"])["is_remote"] is False

    def test_missing_field_falls_back_to_the_title(self):
        assert self._row(None)["is_remote"] is None
        v = {"vacancyId": 1, "name": "Удалённый разработчик", "area": {}}
        assert hh.parse_vacancy(v)["is_remote"] is True
