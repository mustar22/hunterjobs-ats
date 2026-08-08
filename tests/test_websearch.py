"""
tests/test_websearch.py

Backend dispatch: keyed provider wins, failures fall through to ddgs,
total failure returns [] without raising.
"""

from __future__ import annotations

import pytest

import core.websearch as ws


@pytest.fixture
def no_ddgs(monkeypatch):
    monkeypatch.setattr(ws, "_ddgs", lambda q, n, t, src="": [{"title": "d", "body": "dd", "url": "u"}])


class TestDispatch:
    def test_no_keys_uses_ddgs(self, monkeypatch, no_ddgs):
        monkeypatch.setattr(ws, "_load_search_keys", lambda: {"tavily": "", "serper": ""})
        assert ws.search("q")[0]["title"] == "d"

    def test_tavily_key_wins(self, monkeypatch, no_ddgs):
        monkeypatch.setattr(ws, "_load_search_keys", lambda: {"tavily": "k", "serper": "k2"})
        monkeypatch.setattr(ws, "_tavily", lambda q, k, n, t: [{"title": "t", "body": "b", "url": "u"}])
        assert ws.search("q")[0]["title"] == "t"

    def test_tavily_failure_falls_to_serper_then_ddgs(self, monkeypatch, no_ddgs):
        monkeypatch.setattr(ws, "_load_search_keys", lambda: {"tavily": "k", "serper": "k2"})
        def boom(*a): raise RuntimeError("down")
        monkeypatch.setattr(ws, "_tavily", boom)
        monkeypatch.setattr(ws, "_serper", lambda q, k, n, t: [{"title": "s", "body": "b", "url": "u"}])
        assert ws.search("q")[0]["title"] == "s"
        monkeypatch.setattr(ws, "_serper", boom)
        assert ws.search("q")[0]["title"] == "d"

    def test_everything_down_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ws, "_load_search_keys", lambda: {"tavily": "", "serper": ""})
        def boom(*a): raise RuntimeError("down")
        monkeypatch.setattr(ws, "_ddgs", boom)
        assert ws.search("q") == []

    def test_snippets_text(self):
        assert ws.snippets_text([{"title": "A", "body": "B", "url": ""}]) == "A: B"


class TestBackendChoice:
    """Yandex is always on for hh and opt-in elsewhere, so by default a Russian
    engine only ever sees Russian queries."""

    def _cfg(self, monkeypatch, cfg):
        import core.config as c
        monkeypatch.setattr(c, "load_config", lambda: cfg)

    def test_western_sources_get_no_yandex_by_default(self, monkeypatch):
        self._cfg(monkeypatch, {})
        for src in ("", "linkedin", "yc", "hn"):
            assert "yandex" not in ws._backends(src)

    def test_hh_always_gets_yandex_first(self, monkeypatch):
        self._cfg(monkeypatch, {})
        assert ws._backends("hh").startswith("yandex")

    def test_hh_keeps_yandex_even_with_a_custom_chain(self, monkeypatch):
        self._cfg(monkeypatch, {"search_backends": "mojeek"})
        assert ws._backends("hh").startswith("yandex")

    def test_the_opt_in_adds_yandex_everywhere(self, monkeypatch):
        self._cfg(monkeypatch, {"search_yandex": True})
        assert "yandex" in ws._backends("linkedin")

    def test_a_custom_chain_is_respected(self, monkeypatch):
        self._cfg(monkeypatch, {"search_backends": "yahoo"})
        assert ws._backends("linkedin") == "yahoo"

    def test_the_dead_engines_are_not_the_default(self):
        # measured 2026-08-08: all three returned nothing, every query
        for dead in ("duckduckgo", "startpage", "mojeek"):
            assert dead not in ws.DDGS_BACKEND

    def test_a_broken_config_still_yields_a_chain(self, monkeypatch):
        import core.config as c
        monkeypatch.setattr(c, "load_config", lambda: (_ for _ in ()).throw(OSError))
        assert ws._backends("linkedin")
