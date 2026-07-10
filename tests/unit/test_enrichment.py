"""Unit tests for the curation enrichment tools and orchestrator."""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import respx

from src.models.curation import CurationItemType
from src.schemas.repository_schema import RepositoryCreate
from src.services.agents import enrichment
from src.services.tools import article_reader, trending, web_search

_DDG = "https://html.duckduckgo.com/html/"

_DDG_HTML = """
<div class="result">
  <a class="result__a" href="/l/?uddg=x">FastAPI 1.0 released</a>
  <a class="result__snippet" href="#">A major new version with <b>big</b> changes.</a>
</div>
<div class="result">
  <a class="result__a" href="/l/?uddg=y">Why FastAPI is trending</a>
  <a class="result__snippet" href="#">Adoption keeps climbing.</a>
</div>
"""


@respx.mock
def test_web_search_parses_titles_and_snippets():
    respx.get(_DDG).mock(return_value=httpx.Response(200, text=_DDG_HTML))
    results = web_search.search("fastapi", max_results=5)
    assert results[0] == {"title": "FastAPI 1.0 released", "snippet": "A major new version with big changes."}
    assert len(results) == 2


@respx.mock
def test_web_search_returns_empty_on_error():
    respx.get(_DDG).mock(side_effect=httpx.ConnectError("refused"))
    assert web_search.search("fastapi") == []


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


def test_trending_repository_composite(monkeypatch):
    monkeypatch.setattr(trending, "_recent_growth", lambda session, rid: 100)
    item = SimpleNamespace(id="r1", stars_per_week=50.0)
    block = trending.signal(CurationItemType.REPOSITORY, item, session=None)
    # velocity 50/100=0.5, growth 100/200=0.5 -> composite 0.50
    assert "TRENDING SIGNAL (composite 0.50 / 1.00)" in block
    assert "50.0 stars/week" in block
    assert "+100 stars" in block


def test_trending_library_uses_github(monkeypatch):
    top = RepositoryCreate(
        dedup_key="tiangolo/fastapi",
        url="https://github.com/tiangolo/fastapi",
        owner="tiangolo",
        name="fastapi",
        description=None,
        stars=70000,
        forks=6000,
        stars_per_week=200.0,
        relevance_score=1.0,
        languages=["Python"],
        topics=[],
        repo_created_at=None,
        collected_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(trending, "_github_top", lambda name: top)
    item = SimpleNamespace(library=SimpleNamespace(name="fastapi"))
    block = trending.signal(CurationItemType.LIBRARY_RELEASE, item, session=None)
    assert "GitHub repo tiangolo/fastapi" in block
    assert "composite 1.00" in block  # 200/100 clamps to 1.0


def test_build_context_article(monkeypatch):
    monkeypatch.setattr(enrichment.article_reader, "read_main_content", lambda url: "full body here")
    item = SimpleNamespace(url="https://example.com/post")
    ctx = enrichment.build_context(CurationItemType.ARTICLE, item, session=None)
    assert ctx == "FULL ARTICLE CONTENT:\nfull body here"


def test_build_context_repo(monkeypatch):
    monkeypatch.setattr(enrichment.web_search, "search", lambda q, n: [{"title": "T", "snippet": "S"}])
    monkeypatch.setattr(
        enrichment.trending, "signal", lambda it, item, s: "TRENDING SIGNAL (composite 0.30 / 1.00):\n- x"
    )
    item = SimpleNamespace(owner="tiangolo", name="fastapi")
    ctx = enrichment.build_context(CurationItemType.REPOSITORY, item, session=None)
    assert "RELATED WEB COVERAGE (recent news):\n- T: S" in ctx
    assert "TRENDING SIGNAL" in ctx
