"""
GitHub trending-repository collector.

Hits the GitHub Search API over plain httpx (no SDK). The query is built from the
user's preferences (languages + keywords + a recency floor) and results are
ranked by a relevance score combining stars-per-week with keyword/topic match.

Failures follow the shared collector contract: rate-limit (403/429) and 5xx are
:class:`CollectorRetriable`; a malformed response or other 4xx is
:class:`CollectorTerminal`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.schemas.repository_schema import RepositoryCreate

_SEARCH_URL = "https://api.github.com/search/repositories"


def build_query(languages: list[str], keywords: list[str], pushed_since: str, min_stars: int = 10) -> str:
    """
    Build a GitHub Search ``q`` string from preferences.

    :param languages: Languages to OR together (``language:python``).
    :type languages: list[str]
    :param keywords: Free-text keywords to require in the search.
    :type keywords: list[str]
    :param pushed_since: ISO date; only repos pushed on/after are returned.
    :type pushed_since: str
    :param min_stars: Minimum star count.
    :type min_stars: int
    :return: The assembled query string.
    :rtype: str
    """
    parts: list[str] = []
    if keywords:
        parts.append(" ".join(keywords))
    for lang in languages:
        parts.append(f"language:{lang}")
    parts.append(f"pushed:>={pushed_since}")
    parts.append(f"stars:>={min_stars}")
    return " ".join(parts).strip()


def compute_stars_per_week(stars: int, created_at: datetime | None, now: datetime) -> float:
    """
    Approximate star velocity as total stars over the repo's age in weeks.

    A coarse but stable proxy — the Search API gives no historical star counts.
    A repo younger than a week is treated as one week old so brand-new repos
    don't divide by ~0 and dominate the ranking.

    :param stars: Current star count.
    :type stars: int
    :param created_at: Repo creation timestamp, if known.
    :type created_at: datetime | None
    :param now: Reference time.
    :type now: datetime
    :return: Stars per week.
    :rtype: float
    """
    if created_at is None:
        return float(stars)
    age_weeks = max((now - created_at).total_seconds() / (7 * 86400), 1.0)
    return round(stars / age_weeks, 3)


def compute_relevance(stars_per_week: float, topics: list[str], description: str | None, keywords: list[str]) -> float:
    """
    Score a repo against the profile: velocity, dampened, plus keyword match.

    Velocity is log-dampened so a viral repo doesn't swamp the keyword signal;
    each profile keyword found in the topics or description adds a flat bonus.

    :param stars_per_week: Star velocity from :func:`compute_stars_per_week`.
    :type stars_per_week: float
    :param topics: Repo topics.
    :type topics: list[str]
    :param description: Repo description.
    :type description: str | None
    :param keywords: Profile keywords to match.
    :type keywords: list[str]
    :return: A relevance score (higher = more relevant).
    :rtype: float
    """
    from math import log1p

    haystack = " ".join(topics).lower() + " " + (description or "").lower()
    matches = sum(1 for kw in keywords if kw.lower() in haystack)
    return round(log1p(stars_per_week) + matches * 1.5, 3)


def parse_repo(item: dict, keywords: list[str], now: datetime) -> RepositoryCreate:
    """
    Map one GitHub Search API item to a validated :class:`RepositoryCreate`.

    :param item: A single ``items[]`` object from the search response.
    :type item: dict
    :param keywords: Profile keywords for relevance scoring.
    :type keywords: list[str]
    :param now: Collection timestamp.
    :type now: datetime
    :return: The validated row.
    :rtype: RepositoryCreate
    :raises CollectorTerminal: If a required field is missing.
    """
    try:
        full_name = item["full_name"]
        owner = item["owner"]["login"]
        name = item["name"]
        stars = int(item.get("stargazers_count", 0))
    except (KeyError, TypeError) as exc:
        raise CollectorTerminal(f"github: malformed item: {exc}") from exc

    created_raw = item.get("created_at")
    created_at = _parse_dt(created_raw)
    topics = item.get("topics") or []
    description = item.get("description")
    language = item.get("language")
    languages = [language] if language else []

    spw = compute_stars_per_week(stars, created_at, now)
    relevance = compute_relevance(spw, topics, description, keywords)

    return RepositoryCreate(
        dedup_key=full_name,
        url=item.get("html_url", f"https://github.com/{full_name}"),
        owner=owner,
        name=name,
        description=description,
        stars=stars,
        forks=int(item.get("forks_count", 0)),
        stars_per_week=spw,
        relevance_score=relevance,
        languages=languages,
        topics=topics,
        repo_created_at=created_at,
        collected_at=now,
    )


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``...Z``) to an aware datetime."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_trending(
    languages: list[str],
    keywords: list[str],
    *,
    pushed_since: str,
    min_stars: int = 10,
    max_results: int | None = None,
) -> list[RepositoryCreate]:
    """
    Query the GitHub Search API and return validated repository rows.

    :param languages: Languages to include.
    :type languages: list[str]
    :param keywords: Profile keywords (search + relevance).
    :type keywords: list[str]
    :param pushed_since: ISO date recency floor.
    :type pushed_since: str
    :param min_stars: Minimum stars.
    :type min_stars: int
    :param max_results: Cap on rows returned (defaults to ``GITHUB_MAX_RESULTS``).
    :type max_results: int | None
    :return: Validated rows, ranked by relevance descending.
    :rtype: list[RepositoryCreate]
    :raises CollectorRetriable: Timeout, transport error, rate-limit, or 5xx.
    :raises CollectorTerminal: Other 4xx or an unparseable body.
    """
    limit = max_results or settings.GITHUB_MAX_RESULTS
    query = build_query(languages, keywords, pushed_since, min_stars)
    headers = {"Accept": "application/vnd.github+json"}
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": min(limit, 100)}

    try:
        with httpx.Client(timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(_SEARCH_URL, params=params, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise CollectorRetriable(f"github: {type(exc).__name__}: {exc}") from exc

    _raise_for_status(resp)

    try:
        items = resp.json().get("items", [])
    except ValueError as exc:
        raise CollectorTerminal(f"github: non-JSON response: {exc}") from exc

    now = datetime.now(timezone.utc)
    rows = [parse_repo(item, keywords, now) for item in items[:limit]]
    rows.sort(key=lambda r: r.relevance_score, reverse=True)
    return rows


def _raise_for_status(resp: httpx.Response) -> None:
    """Map a GitHub HTTP status onto the collector exception contract."""
    if resp.status_code == 200:
        return
    # 403 with a zero remaining-rate header (or 429) is a rate limit — retry.
    if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0"):
        raise CollectorRetriable("github: rate limited")
    if resp.status_code >= 500:
        raise CollectorRetriable(f"github: server error {resp.status_code}")
    raise CollectorTerminal(f"github: unexpected status {resp.status_code}")
