import httpx
import pytest
import respx

from src.core.exceptions import CollectorRetriable
from src.models.article import ArticleSource
from src.services.collectors import articles

_HN_URL = "https://hn.algolia.com/api/v1/search"


def _hn_hit(title="Show HN: Widget", url="https://widget.dev", points=120, comments=34, object_id="401"):
    return {
        "title": title,
        "url": url,
        "author": "pg",
        "points": points,
        "num_comments": comments,
        "created_at": "2026-06-01T00:00:00Z",
        "objectID": object_id,
    }


_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Async Rust in 2026</title>
    <link>https://blog.example.com/async-rust</link>
    <author>jane@example.com</author>
    <description>A deep dive.</description>
    <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_dedup_key_is_stable_and_source_scoped():
    a = articles.dedup_key(ArticleSource.DEVTO, "https://dev.to/x")
    b = articles.dedup_key(ArticleSource.DEVTO, "https://dev.to/x")
    c = articles.dedup_key(ArticleSource.MEDIUM, "https://dev.to/x")
    assert a == b
    assert a != c  # same url, different source → different key
    assert a.startswith("devto:")


@respx.mock
def test_fetch_devto_maps_items():
    respx.get("https://dev.to/api/articles").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "title": "Hello",
                    "url": "https://dev.to/u/hello",
                    "user": {"name": "Ada"},
                    "description": "hi",
                    "public_reactions_count": 5,
                    "comments_count": 2,
                    "published_at": "2026-06-01T00:00:00Z",
                }
            ],
        )
    )
    rows = articles.fetch_devto(["python"])
    assert len(rows) == 1
    assert rows[0].source == ArticleSource.DEVTO
    assert rows[0].author == "Ada"
    assert rows[0].likes == 5


@respx.mock
def test_fetch_hackernews_maps_items():
    respx.get(_HN_URL).mock(return_value=httpx.Response(200, json={"hits": [_hn_hit()]}))
    rows = articles.fetch_hackernews(["python"])
    assert len(rows) == 1
    row = rows[0]
    assert row.source == ArticleSource.HACKERNEWS
    assert row.title == "Show HN: Widget"
    assert row.url == "https://widget.dev"
    assert row.author == "pg"
    assert row.likes == 120
    assert row.comments == 34
    assert row.published_at is not None


@respx.mock
def test_fetch_hackernews_falls_back_to_hn_link():
    # Ask HN / text posts have no external url — link to the HN item itself.
    respx.get(_HN_URL).mock(return_value=httpx.Response(200, json={"hits": [_hn_hit(url=None, object_id="42")]}))
    rows = articles.fetch_hackernews(["python"])
    assert rows[0].url == "https://news.ycombinator.com/item?id=42"


@respx.mock
def test_fetch_hackernews_rate_limit_is_retriable():
    respx.get(_HN_URL).mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(CollectorRetriable):
        articles.fetch_hackernews(["python"])


def test_fetch_rss_parses_content_string():
    # feedparser accepts a raw feed string directly — no network needed.
    rows = articles.fetch_rss(_RSS, source=ArticleSource.MEDIUM)
    assert len(rows) == 1
    row = rows[0]
    assert row.title == "Async Rust in 2026"
    assert row.source == ArticleSource.MEDIUM
    assert row.url == "https://blog.example.com/async-rust"
    assert row.published_at is not None


def test_title_fingerprint_normalizes():
    fp = articles.title_fingerprint
    assert fp("Show HN: FastAPI 1.0!") == fp("fastapi 1.0")
    assert fp("FastAPI 1.0 — released") == fp("FastAPI  1.0 released")
    assert fp("FastAPI 1.0") != fp("Django 6.0")


def test_parsers_stamp_title_fingerprint():
    row = articles._parse_hn_hit(_hn_hit())
    assert row.title_fingerprint == articles.title_fingerprint("Show HN: Widget")


@respx.mock
def test_fetch_hackernews_applies_recency_filter():
    from datetime import datetime, timezone

    route = respx.get(_HN_URL).mock(return_value=httpx.Response(200, json={"hits": []}))
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    articles.fetch_hackernews(["python"], since=since)
    assert route.calls.last.request.url.params["numericFilters"] == f"created_at_i>{int(since.timestamp())}"
