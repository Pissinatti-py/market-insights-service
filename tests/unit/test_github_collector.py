from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.services.collectors import github

_URL = "https://api.github.com/search/repositories"


def _item(full_name="octo/widget", stars=520, created_days_ago=10, topics=None, desc="a rust cli widget"):
    created = (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).isoformat().replace("+00:00", "Z")
    return {
        "full_name": full_name,
        "owner": {"login": full_name.split("/")[0]},
        "name": full_name.split("/")[1],
        "stargazers_count": stars,
        "forks_count": 12,
        "html_url": f"https://github.com/{full_name}",
        "description": desc,
        "language": "Rust",
        "topics": topics or ["cli", "rust"],
        "created_at": created,
    }


def test_build_query_combines_filters():
    q = github.build_query(["python", "rust"], ["llm", "agents"], "2026-01-01", min_stars=50)
    assert "language:python" in q and "language:rust" in q
    assert "llm" in q and "agents" in q
    assert "pushed:>=2026-01-01" in q and "stars:>=50" in q


def test_stars_per_week_floors_age_at_one_week():
    now = datetime.now(timezone.utc)
    # 3 days old → treated as 1 week → spw == stars
    spw = github.compute_stars_per_week(70, now - timedelta(days=3), now)
    assert spw == 70.0
    # ~10 weeks old
    spw2 = github.compute_stars_per_week(700, now - timedelta(weeks=10), now)
    assert 60 < spw2 < 80


def test_relevance_rewards_keyword_match():
    base = github.compute_relevance(10.0, ["cli"], "a tool", [])
    matched = github.compute_relevance(10.0, ["cli", "llm"], "an llm tool", ["llm"])
    assert matched > base


def test_parse_repo_maps_fields():
    now = datetime.now(timezone.utc)
    row = github.parse_repo(_item(), ["rust"], now)
    assert row.dedup_key == "octo/widget"
    assert row.owner == "octo" and row.name == "widget"
    assert row.stars == 520
    assert row.languages == ["Rust"]
    assert row.collected_at == now


def test_parse_repo_rejects_malformed():
    with pytest.raises(CollectorTerminal):
        github.parse_repo({"name": "x"}, [], datetime.now(timezone.utc))


@respx.mock
def test_fetch_trending_ranks_by_relevance():
    payload = {
        "items": [
            _item("a/low", stars=10, topics=["misc"], desc="misc"),
            _item("b/high", stars=900, topics=["llm"], desc="an llm agent"),
        ]
    }
    respx.get(_URL).mock(return_value=httpx.Response(200, json=payload))
    rows = github.fetch_trending(["python"], ["llm"], pushed_since="2026-01-01")
    assert [r.dedup_key for r in rows][0] == "b/high"  # highest relevance first


@respx.mock
def test_fetch_trending_rate_limit_is_retriable():
    respx.get(_URL).mock(return_value=httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, json={}))
    with pytest.raises(CollectorRetriable):
        github.fetch_trending([], [], pushed_since="2026-01-01")


@respx.mock
def test_fetch_trending_server_error_is_retriable():
    respx.get(_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(CollectorRetriable):
        github.fetch_trending([], [], pushed_since="2026-01-01")
