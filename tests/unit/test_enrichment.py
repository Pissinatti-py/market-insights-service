"""Unit tests for the curation enrichment tools and orchestrator."""

from types import SimpleNamespace

import httpx
import respx

from src.models.curation import CurationItemType
from src.services.agents import enrichment
from src.services.tools import article_reader


@respx.mock
def test_article_reader_extracts_and_truncates(monkeypatch):
    respx.get("https://example.com/post").mock(return_value=httpx.Response(200, text="<html>..</html>"))
    monkeypatch.setattr(article_reader.trafilatura, "extract", lambda *a, **k: "x" * 10_000)
    monkeypatch.setattr(article_reader.settings, "ARTICLE_MAX_CHARS", 100)
    text = article_reader.read_main_content("https://example.com/post")
    assert text is not None and len(text) == 100


@respx.mock
def test_article_reader_returns_none_on_fetch_failure():
    respx.get("https://example.com/post").mock(return_value=httpx.Response(404))
    assert article_reader.read_main_content("https://example.com/post") is None


def test_build_context_article(monkeypatch):
    monkeypatch.setattr(enrichment.article_reader, "read_main_content", lambda url: "full body here")
    item = SimpleNamespace(url="https://example.com/post")
    ctx = enrichment.build_context(CurationItemType.ARTICLE, item)
    assert ctx == "FULL ARTICLE CONTENT:\nfull body here"


def test_build_context_article_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(enrichment.article_reader, "read_main_content", lambda url: None)
    item = SimpleNamespace(url="https://example.com/post")
    assert enrichment.build_context(CurationItemType.ARTICLE, item) == ""


def test_build_context_repo_is_empty():
    item = SimpleNamespace(owner="tiangolo", name="fastapi")
    assert enrichment.build_context(CurationItemType.REPOSITORY, item) == ""
