"""
tests/test_enrich.py

Company enrichment: posting-email extraction, cache keys/TTL, and the
enrich_company orchestration with all network + LLM stubbed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import core.companies as companies
from core.database import COMPANIES_TABLE
from core.schemas import CompanyEnrichment, WebContact
import pipeline.enrich as enrich
import pipeline.brain1 as b1


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(COMPANIES_TABLE)
    yield c
    c.close()


class TestPostingEmails:
    def test_plain_email_extracted_verified(self):
        out = enrich.posting_emails("Apply: send CV to jobs@acme.dev today")
        assert out == [{"name": "", "title": "listed in posting",
                        "email": "jobs@acme.dev", "source": "listing",
                        "confidence": "verified"}]

    def test_hn_style_obfuscation(self):
        out = enrich.posting_emails("reach me at jane (at) acme (dot) com or jim at acme dot com")
        emails = {c["email"] for c in out}
        assert emails == {"jane@acme.com", "jim@acme.com"}

    def test_junk_filtered(self):
        text = "noreply@acme.com hero@2x.png foo@example.com"
        assert enrich.posting_emails(text) == []

    def test_dedup_case_insensitive(self):
        out = enrich.posting_emails("Jobs@Acme.dev and jobs@acme.dev")
        assert len(out) == 1

    def test_empty(self):
        assert enrich.posting_emails("") == []


class TestCompanyKey:
    def test_domain_wins(self):
        assert companies.company_key("Acme Inc", "acme.dev") == "acme.dev"

    def test_name_fallback_normalized(self):
        assert companies.company_key("Enveritas (YC S18, non-profit) Inc.", "") == "enveritas"

    def test_empty(self):
        assert companies.company_key("", "") == ""


class TestCache:
    DATA = {"name": "Acme", "domain": "acme.dev", "company_summary": "s",
            "hiring_signal": "looks_real", "real_stack": ["python"],
            "culture_flags": [], "company_size": "tiny",
            "contacts": [{"name": "J", "email": "j@acme.dev"}], "hunted": True}

    def test_roundtrip(self, conn):
        companies.save(conn, "acme.dev", self.DATA)
        got = companies.get_cached(conn, "acme.dev", ttl_days=30)
        assert got["real_stack"] == ["python"]
        assert got["contacts"][0]["email"] == "j@acme.dev"
        assert got["hunted"] == 1

    def test_ttl_expiry(self, conn):
        old = dict(self.DATA)
        old["researched_at"] = (datetime.now(timezone.utc)
                                - timedelta(days=40)).isoformat()
        companies.save(conn, "acme.dev", old)
        assert companies.get_cached(conn, "acme.dev", ttl_days=30) is None
        assert companies.get_cached(conn, "acme.dev", ttl_days=0) is not None

    def test_miss(self, conn):
        assert companies.get_cached(conn, "ghost.io") is None


class TestCleanStr:
    def test_nullish_killed(self):
        for junk in ("null", "None", "N/A", "undefined", "-"):
            assert enrich._clean_str(junk) == ""

    def test_normal_passthrough_and_cap(self):
        assert enrich._clean_str("CEO") == "CEO"
        assert len(enrich._clean_str("x" * 500)) == 100


@pytest.fixture
def stubbed(monkeypatch):
    """Stub every network/LLM edge of enrich_company; record call counts."""
    calls = {"llm": 0, "site": 0, "team": 0, "github": 0, "yc": 0}

    def fake_llm(client, model, backend, system, prompt, schema, **kw):
        calls["llm"] += 1
        return CompanyEnrichment(
            company_summary="Builds rockets.", hiring_signal="looks_real",
            real_stack=["python"], culture_flags=[], company_size="tiny",
            people=[WebContact(name="Ada Lovelace", title="null")])

    monkeypatch.setattr(b1, "call_gemma", fake_llm)
    monkeypatch.setattr(enrich, "gather_site_content",
                        lambda *a, **k: (calls.__setitem__("site", calls["site"] + 1)
                                         or ("site text " * 50, "website",
                                             ["https://acme.dev"])))
    monkeypatch.setattr(enrich, "crawl_team_contacts",
                        lambda *a, **k: (calls.__setitem__("team", calls["team"] + 1) or []))
    monkeypatch.setattr(b1, "github_contacts",
                        lambda *a, **k: (calls.__setitem__("github", calls["github"] + 1) or []))
    monkeypatch.setattr(b1, "load_keys", lambda: {"github": "tok"})
    monkeypatch.setattr(enrich, "fetch_yc_company",
                        lambda slug, **k: (calls.__setitem__("yc", calls["yc"] + 1) or {
                            "founders": [{"name": "Luis P.", "title": "Founder",
                                          "email": "", "source": "yc",
                                          "confidence": "verified", "bio": "b"}],
                            "one_liner": "Rockets."}) if slug else None)
    return calls


class TestEnrichCompany:
    CFG = {"company_ttl_days": 30}

    def test_full_pass_merges_and_caches(self, conn, stubbed):
        e = enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev",
                                  yc_slug="acme")
        # team crawl deliberately skipped when YC founders are known
        assert stubbed == {"llm": 1, "site": 1, "team": 0, "github": 1, "yc": 1}
        names = [c["name"] for c in e["contacts"] if c["name"]]
        assert "Luis P." in names and "Ada Lovelace" in names    # yc + llm people
        luis = next(c for c in e["contacts"] if c["name"] == "Luis P.")
        assert luis["confidence"] == "verified" or "+permutation" in luis["source"]
        ada = next(c for c in e["contacts"] if c["name"] == "Ada Lovelace")
        assert ada["title"] == ""                                 # 'null' sanitized
        assert e["hunted"] and not e["from_cache"]
        # second call: pure cache, zero extra work
        e2 = enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev",
                                   yc_slug="acme")
        assert e2["from_cache"] is True
        assert stubbed["llm"] == 1

    def test_skip_hunt_saves_the_network_work(self, conn, stubbed):
        e = enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev",
                                  yc_slug="acme", skip_hunt=True)
        assert stubbed["team"] == 0 and stubbed["github"] == 0
        assert stubbed["yc"] == 1                # founders still ride along
        assert e["hunted"] is False
        # no pure-permutation noise added on a skip-hunt pass
        assert not any(c["source"] == "permutation" for c in e["contacts"])

    def test_hunt_upgrade_after_skip_hunt_cache(self, conn, stubbed):
        enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev",
                              skip_hunt=True)
        e = enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev")
        assert e["hunted"] is True
        assert stubbed["team"] == 1
        assert stubbed["llm"] == 1               # research reused from cache

    def test_force_refreshes(self, conn, stubbed):
        enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev")
        enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev", force=True)
        assert stubbed["llm"] == 2

    def test_llm_failure_still_returns_honest_row(self, conn, stubbed, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("429")
        monkeypatch.setattr(b1, "call_gemma", boom)
        e = enrich.enrich_company(conn, self.CFG, "Acme", "https://acme.dev")
        assert e["company_summary"] == "Info unavailable."
        assert e["hiring_signal"] == "uncertain"


class TestPrecisionGuards:
    def test_llm_placeholder_names_rejected(self, conn, stubbed, monkeypatch):
        def fake_llm(client, model, backend, system, prompt, schema, **kw):
            return CompanyEnrichment(
                company_summary="s", people=[
                    WebContact(name="name_not_found", title="Founder"),
                    WebContact(name="Technical Expertise", title="engineer"),
                    WebContact(name="Jane Doe", title="CEO"),
                ])
        monkeypatch.setattr(b1, "fake", None, raising=False)
        monkeypatch.setattr(b1, "call_gemma", fake_llm)
        e = enrich.enrich_company(conn, {"company_ttl_days": 30},
                                  "Acme", "https://acme.dev")
        names = [c["name"] for c in e["contacts"] if c["name"]]
        assert names == ["Jane Doe"]

    def test_team_crawl_skipped_when_yc_founders_known(self, conn, stubbed):
        enrich.enrich_company(conn, {"company_ttl_days": 30},
                              "Acme", "https://acme.dev", yc_slug="acme")
        assert stubbed["team"] == 0
        enrich.enrich_company(conn, {"company_ttl_days": 30},
                              "NoYc", "https://noyc.dev")
        assert stubbed["team"] == 1

    def test_marketing_heading_not_a_person(self):
        assert not b1._is_real_person_name("Technical Expertise")
        assert not b1._is_real_person_name("Customer Success")
        assert b1._is_real_person_name("Ann Chan")


class TestSources:
    def test_sources_recorded_and_cached(self, conn, stubbed):
        e = enrich.enrich_company(conn, {"company_ttl_days": 30},
                                  "Acme", "https://acme.dev", yc_slug="acme")
        labels = {s["label"] for s in e["sources"]}
        assert labels == {"YC profile", "Company site"}
        assert any("ycombinator.com/companies/acme" in s["url"] for s in e["sources"])
        # cache round-trip keeps them
        e2 = enrich.enrich_company(conn, {"company_ttl_days": 30},
                                   "Acme", "https://acme.dev", yc_slug="acme")
        assert e2["from_cache"] and e2["sources"] == e["sources"]
