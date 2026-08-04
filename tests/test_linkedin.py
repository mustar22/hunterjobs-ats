"""LinkedIn source — the tests are all about not losing listings silently.

jobspy's version of this had four defects: it advanced the offset by the
cumulative result count (skipping pages), stopped on the first empty page,
used relevance ordering instead of date, and passed filters the endpoint
ignores. Each one under-reported without complaining.
"""

from datetime import datetime, timezone

import pipeline.sources.linkedin as li

# must be relative to now: a hardcoded date silently falls outside the window
# once the calendar moves on, and the whole suite starts failing a day later
TODAY = datetime.now(timezone.utc).date().isoformat()


class _Card:
    """Minimal stand-in for a BeautifulSoup search-result card."""
    def __init__(self, jid, date=TODAY):
        self.jid, self.date = jid, date

    def find(self, tag, class_=None, **kw):
        if tag == "time":
            return {"datetime": self.date} if self.date else None
        if class_ == "base-card__full-link":
            return {"href": f"https://www.linkedin.com/jobs/view/x-{self.jid}"}
        return None


def _pages(seq, monkeypatch):
    """seq: list of (status, cards) returned in order."""
    calls = {"n": 0, "starts": []}

    def fake(session, start, hours, location, keywords="", timeout=15):
        calls["starts"].append(start)
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(li, "_get_page", fake)
    monkeypatch.setattr(li.time, "sleep", lambda *_: None)
    return calls


class TestRateLimitBackoff:
    def test_429_retries_the_same_offset(self, monkeypatch):
        # skipping ahead after a 429 is how you silently lose a page
        calls = _pages([("ratelimited", []), ("ok", []), ("ok", []), ("ok", [])],
                       monkeypatch)
        li.scrape_linkedin_jobs(hours=24, with_descriptions=False)
        assert calls["starts"][0] == calls["starts"][1] == 0

    def test_persistent_429_stops_and_says_so(self, monkeypatch):
        _pages([("ratelimited", [])], monkeypatch)
        stats = {}
        li.scrape_linkedin_jobs(hours=24, with_descriptions=False, stats=stats)
        assert "rate limited" in stats["reason"]
        assert stats["complete"] is False

    def test_a_good_page_clears_the_throttle(self, monkeypatch):
        seq = [("ratelimited", []), ("ok", [_Card(1)]), ("ratelimited", []),
               ("ok", [_Card(2)])] + [("ok", [])] * 5
        _pages(seq, monkeypatch)
        stats = {}
        rows = li.scrape_linkedin_jobs(hours=24, with_descriptions=False,
                                       stats=stats)
        assert len(rows) == 2          # survived two separate throttles


class TestPagination:
    def test_offset_advances_by_page_size(self, monkeypatch):
        calls = _pages([("ok", [_Card(i) for i in range(10)])] * 3
                       + [("ok", [])] * 4, monkeypatch)
        li.scrape_linkedin_jobs(hours=24, with_descriptions=False)
        assert calls["starts"][:3] == [0, li.PAGE, li.PAGE * 2]

    def test_empty_page_is_not_the_end(self, monkeypatch):
        # start=500 returned 200-with-no-cards while start=900 returned ten
        seq = [("ok", []), ("ok", [_Card(1)])] + [("ok", [])] * 5
        _pages(seq, monkeypatch)
        rows = li.scrape_linkedin_jobs(hours=24, with_descriptions=False)
        assert len(rows) == 1

    def test_stops_at_the_window_edge(self, monkeypatch):
        _pages([("ok", [_Card(1, TODAY), _Card(2, "1999-01-01")])],
               monkeypatch)
        stats = {}
        rows = li.scrape_linkedin_jobs(hours=24, with_descriptions=False,
                                       stats=stats)
        assert len(rows) == 1 and stats["complete"] is True


class TestParsing:
    def test_id_matches_the_existing_pool_format(self):
        row = li.parse_card(_Card(4441389991))
        assert row["id"] == "li-4441389991"
        assert row["job_url"].endswith("/4441389991")

    def test_card_without_a_numeric_id_is_dropped(self):
        class Bad(_Card):
            def find(self, tag, class_=None, **kw):
                if class_ == "base-card__full-link":
                    return {"href": "https://www.linkedin.com/jobs/view/nope"}
                return None
        assert li.parse_card(Bad(1)) is None


class TestCompanyPage:
    """A listing carries a company name and a slug, never a domain — which is
    why research kept returning 'No information available'. The company page
    has the real website plus fields LinkedIn already classified."""

    def test_slug_survives_every_url_shape(self):
        for raw, want in [
            ("https://www.linkedin.com/company/joinhyra", "joinhyra"),
            ("https://se.linkedin.com/company/spotify?trk=x", "spotify"),
            ("https://www.linkedin.com/company/luxoft-serbia/", "luxoft-serbia"),
            ("joinhyra", "joinhyra"),
            ("", ""),
        ]:
            assert li.company_slug(raw) == want

    def test_linkedins_own_industry_flags_an_agency(self):
        # no model involved: the company filed itself under this
        assert li.is_agency_industry({"industry": "Staffing and Recruiting"})
        assert li.is_agency_industry({"industry": "staffing and recruiting"})

    def test_a_normal_industry_is_not_an_agency(self):
        assert not li.is_agency_industry({"industry": "Software Development"})
        assert not li.is_agency_industry({"industry": "IT Services and IT Consulting"})
        assert not li.is_agency_industry({})
        assert not li.is_agency_industry(None)

    def test_http_999_returns_none_rather_than_junk(self, monkeypatch):
        # 999 is what LinkedIn sends when a session has already spent its one
        # free company page — it must not look like an empty company
        class R:
            status_code, text, url = 999, "", ""
        monkeypatch.setattr(li.requests, "Session",
                            lambda: type("S", (), {
                                "headers": {}, "get": lambda s, *a, **k: R(),
                                "close": lambda s: None,
                                "__init__": lambda s: None})())
        assert li.fetch_company("anything") is None

    def test_blank_slug_is_not_fetched(self):
        assert li.fetch_company("") is None


class TestSizeBucket:
    """The schema takes tiny/mid/enterprise. Passing LinkedIn's raw
    '201-500 employees' through failed validation and killed enrichment for
    every company whose page resolved — 37 of 37 in one run."""

    def test_linkedin_ranges_map_to_the_schema(self):
        for raw, want in [
            ("2-10 employees", "tiny"), ("11-50 employees", "tiny"),
            ("51-200 employees", "mid"), ("201-500 employees", "mid"),
            ("501-1,000 employees", "enterprise"),
            ("10,001+ employees", "enterprise"),
        ]:
            assert li.size_bucket(raw) == want, raw

    def test_every_bucket_is_schema_valid(self):
        from core.schemas import CompanyResearch
        for raw in ("2-10 employees", "201-500 employees", "10,001+ employees"):
            CompanyResearch(company_summary="x", hiring_signal="uncertain",
                            real_stack=[], culture_flags=[],
                            company_size=li.size_bucket(raw))

    def test_unparseable_returns_empty_so_the_model_keeps_its_answer(self):
        assert li.size_bucket("") == ""
        assert li.size_bucket("unknown") == ""
        assert li.size_bucket(None) == ""
