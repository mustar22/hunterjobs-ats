"""Keyless name -> domain resolution.

The point of these tests is the REFUSALS. Resolving a domain saves a search
call; resolving the WRONG domain attaches another company's website to a
listing and researches it with total confidence. Only one of those is a bug
worth shipping, so most of what follows checks that we back off.
"""

import core.domains as domains


def _fetch(pages):
    """Fake scrape_markdown backed by a {domain: text} dict."""
    return lambda d: pages.get(d, "(fetch failed: 404)")


class TestLooksLike:
    def test_multiword_name_matches_on_one_identity_word(self):
        assert domains.looks_like("Emerging Travel Group",
                                  "emergingtravel.com", "Emerging Travel Group")

    def test_single_word_name_rejects_a_longer_domain(self):
        # primagames.com is a games site, not the company called "Prima"
        assert not domains.looks_like("Prima Games", "primagames.com", "Prima")
        assert not domains.looks_like("Kira Talent", "kiratalent.com", "Kira")

    def test_single_word_name_allows_a_boring_suffix(self):
        assert domains.looks_like("Ashby", "ashbyhq.com", "Ashby")
        assert domains.looks_like("MrBeast", "mrbeastjobs.com", "MrBeast")

    def test_exact_domain_always_ok(self):
        assert domains.looks_like("Inetum", "inetum.com", "Inetum")

    def test_noise_words_alone_never_match(self):
        # "Global Technology Solutions" is all filler - matching on it would
        # verify against any consultancy on earth
        assert not domains.looks_like("Acme Corp", "acme.com",
                                      "Global Technology Solutions Inc")

    def test_empty_name_is_not_a_match(self):
        assert not domains.looks_like("Whatever", "whatever.com", "")


class TestVerify:
    def test_page_that_names_the_company_verifies(self):
        ok, text = domains.verify(
            "monterail.com", "Monterail",
            _fetch({"monterail.com": "Monterail builds software " * 40}))
        assert ok and "Monterail" in text

    def test_page_that_never_says_the_name_is_refused(self):
        ok, _ = domains.verify(
            "somebodyelse.com", "Monterail",
            _fetch({"somebodyelse.com": "We sell industrial pumps " * 40}))
        assert not ok

    def test_failed_fetch_is_refused_not_trusted(self):
        ok, text = domains.verify("dead.com", "Dead", _fetch({}))
        assert not ok and text == ""


class TestResolve:
    def test_returns_empty_when_nothing_verifies(self, monkeypatch):
        monkeypatch.setattr(domains, "suggest",
                            lambda n: [{"name": "Wrong", "domain": "wrong.com"}])
        domains._cache.clear()
        assert domains.resolve("Rightco", _fetch({})) == ("", "")

    def test_returns_verified_domain_and_reuses_the_page(self, monkeypatch):
        body = "Rightco is a company that does things. " * 40
        monkeypatch.setattr(domains, "suggest",
                            lambda n: [{"name": "Rightco", "domain": "rightco.com"}])
        domains._cache.clear()
        dom, text = domains.resolve("Rightco", _fetch({"rightco.com": body}))
        assert dom == "rightco.com"
        assert "Rightco" in text          # handed back so enrich needn't refetch

    def test_a_wrong_suggestion_does_not_get_attached(self, monkeypatch):
        # clearbit fuzzy-matches; the page decides, not the suggestion
        monkeypatch.setattr(domains, "suggest",
                            lambda n: [{"name": "MrBeast Burger",
                                        "domain": "mrbeastburger.com"}])
        domains._cache.clear()
        dom, _ = domains.resolve(
            "MrBeast", _fetch({"mrbeastburger.com": "Order a burger " * 40}))
        assert dom == ""

    def test_suggest_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(domains, "suggest", lambda n: [])
        domains._cache.clear()
        assert domains.resolve("Anything", _fetch({})) == ("", "")

    def test_blank_name_short_circuits(self):
        assert domains.resolve("  ", _fetch({})) == ("", "")


class TestGuessFallback:
    def test_guesses_dotcom_when_suggest_is_dead(self, monkeypatch):
        # the whole point: HubSpot could switch autocomplete off tomorrow
        monkeypatch.setattr(domains, "suggest", lambda n: [])
        domains._cache.clear()
        body = "Monterail builds software. " * 40
        dom, text = domains.resolve("Monterail", _fetch({"monterail.com": body}))
        assert dom == "monterail.com" and "Monterail" in text

    def test_never_guesses_alternate_tlds(self, monkeypatch):
        # openai.co is a parked lookalike; guessing past .com resolved WRONG
        # companies in live testing, so only .com is allowed
        monkeypatch.setattr(domains, "suggest", lambda n: [])
        domains._cache.clear()
        squatter = "OpenAI is great, buy this domain. " * 40
        dom, _ = domains.resolve("OpenAI", _fetch({"openai.co": squatter,
                                                   "openai.ai": squatter}))
        assert dom == ""

    def test_does_not_guess_for_multiword_names(self, monkeypatch):
        monkeypatch.setattr(domains, "suggest", lambda n: [])
        domains._cache.clear()
        dom, _ = domains.resolve(
            "Emerging Travel Group",
            _fetch({"emerging.com": "Emerging Travel Group " * 40}))
        assert dom == ""

    def test_guess_still_has_to_verify(self, monkeypatch):
        monkeypatch.setattr(domains, "suggest", lambda n: [])
        domains._cache.clear()
        dom, _ = domains.resolve(
            "Acme", _fetch({"acme.com": "We sell industrial pumps " * 40}))
        assert dom == ""
