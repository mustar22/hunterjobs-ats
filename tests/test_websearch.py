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
    monkeypatch.setattr(ws, "_ddgs", lambda q, n, t: [{"title": "d", "body": "dd", "url": "u"}])


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
