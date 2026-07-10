"""
Article collectors: Dev.to (official API) and Medium / technical blogs (RSS).

Clean, ToS-friendly sources — no scraping. Dev.to exposes a JSON API; Medium tag
feeds and most engineering blogs publish RSS/Atom, parsed with ``feedparser``.
Each row is deduped on a hash of ``source + url``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.models.article import ArticleSource
from src.schemas.article_schema import ArticleCreate

_DEVTO_URL = "https://dev.to/api/articles"
_HN_URL = "https://hn.algolia.com/api/v1/search"


def dedup_key(source: ArticleSource, url: str) -> str:
    """
    Stable dedup key for an article: ``<source>:<sha256(url)[:32]>``.

    :param source: The feed the article came from.
    :type source: ArticleSource
    :param url: The article's canonical URL.
    :type url: str
    :return: The dedup key.
    :rtype: str
    """
    digest = hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()[:32]
    return f"{source.value}:{digest}"


def fetch_devto(tags: list[str], per_page: int = 30) -> list[ArticleCreate]:
    """
    Fetch recent Dev.to articles for the given tags.

    :param tags: Topic tags (one request per tag, results merged).
    :type tags: list[str]
    :param per_page: Max articles per tag.
    :type per_page: int
    :return: Validated article rows.
    :rtype: list[ArticleCreate]
    :raises CollectorRetriable: Timeout, transport error, 429, or 5xx.
    :raises CollectorTerminal: Other 4xx or an unparseable body.
    """
    out: list[ArticleCreate] = []
    queries = tags or [""]
    with httpx.Client(timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS) as client:
        for tag in queries:
            params = {"per_page": per_page}
            if tag:
                params["tag"] = tag
            try:
                resp = client.get(_DEVTO_URL, params=params, headers={"Accept": "application/json"})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise CollectorRetriable(f"devto: {type(exc).__name__}: {exc}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                raise CollectorRetriable(f"devto: status {resp.status_code}")
            if resp.status_code != 200:
                raise CollectorTerminal(f"devto: status {resp.status_code}")

            try:
                items = resp.json()
            except ValueError as exc:
                raise CollectorTerminal(f"devto: non-JSON: {exc}") from exc

            out.extend(_parse_devto_item(item) for item in items if isinstance(item, dict))
    return out


def _parse_devto_item(item: dict) -> ArticleCreate:
    """Map one Dev.to API article to an :class:`ArticleCreate`."""
    url = item.get("url", "")
    return ArticleCreate(
        dedup_key=dedup_key(ArticleSource.DEVTO, url),
        source=ArticleSource.DEVTO,
        title=item.get("title", "(untitled)"),
        author=(item.get("user") or {}).get("name"),
        url=url,
        content=item.get("description"),
        likes=int(item.get("public_reactions_count", 0)),
        comments=int(item.get("comments_count", 0)),
        published_at=_parse_iso(item.get("published_at")),
    )


def fetch_hackernews(keywords: list[str], per_page: int = 30) -> list[ArticleCreate]:
    """
    Fetch recent Hacker News stories for the given keywords (Algolia HN API).

    Free, no auth, JSON. One request per keyword, results merged. Text posts
    (Ask HN etc.) have no external URL — those link to the HN item itself.

    :param keywords: Search terms (one request per keyword, results merged).
    :type keywords: list[str]
    :param per_page: Max stories per keyword.
    :type per_page: int
    :return: Validated article rows.
    :rtype: list[ArticleCreate]
    :raises CollectorRetriable: Timeout, transport error, 429, or 5xx.
    :raises CollectorTerminal: Other 4xx or an unparseable body.
    """
    out: list[ArticleCreate] = []
    with httpx.Client(timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS) as client:
        for keyword in keywords or [""]:
            params = {"tags": "story", "hitsPerPage": per_page}
            if keyword:
                params["query"] = keyword
            try:
                resp = client.get(_HN_URL, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise CollectorRetriable(f"hackernews: {type(exc).__name__}: {exc}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                raise CollectorRetriable(f"hackernews: status {resp.status_code}")
            if resp.status_code != 200:
                raise CollectorTerminal(f"hackernews: status {resp.status_code}")

            try:
                hits = resp.json().get("hits", [])
            except ValueError as exc:
                raise CollectorTerminal(f"hackernews: non-JSON: {exc}") from exc

            out.extend(_parse_hn_hit(hit) for hit in hits if isinstance(hit, dict))
    return out


def _parse_hn_hit(hit: dict) -> ArticleCreate:
    """Map one Algolia HN story hit to an :class:`ArticleCreate`."""
    url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
    return ArticleCreate(
        dedup_key=dedup_key(ArticleSource.HACKERNEWS, url),
        source=ArticleSource.HACKERNEWS,
        title=hit.get("title") or "(untitled)",
        author=hit.get("author"),
        url=url,
        content=hit.get("story_text"),
        likes=int(hit.get("points") or 0),
        comments=int(hit.get("num_comments") or 0),
        published_at=_parse_iso(hit.get("created_at")),
    )


def fetch_rss(feed_url: str, source: ArticleSource = ArticleSource.RSS) -> list[ArticleCreate]:
    """
    Parse an RSS/Atom feed into article rows (Medium tags, engineering blogs).

    feedparser is lenient and never raises on malformed feeds — it sets
    ``bozo``. A genuinely unreachable feed surfaces as an empty entry list, which
    we treat as a soft miss (return ``[]``) rather than failing the run.

    :param feed_url: The feed URL.
    :type feed_url: str
    :param source: Which source label to stamp (Medium feeds → MEDIUM).
    :type source: ArticleSource
    :return: Validated article rows.
    :rtype: list[ArticleCreate]
    """
    parsed = feedparser.parse(feed_url)
    out: list[ArticleCreate] = []
    for entry in parsed.entries:
        url = entry.get("link")
        if not url:
            continue
        out.append(
            ArticleCreate(
                dedup_key=dedup_key(source, url),
                source=source,
                title=entry.get("title", "(untitled)"),
                author=entry.get("author"),
                url=url,
                content=entry.get("summary"),
                published_at=_parse_struct_time(entry),
            )
        )
    return out


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (``...Z``) to an aware datetime."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_struct_time(entry) -> datetime | None:
    """Pull a published/updated ``time.struct_time`` from a feed entry, if present."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if st is None:
        return None
    return datetime.fromtimestamp(mktime(st), tz=timezone.utc)
