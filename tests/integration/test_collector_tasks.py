"""
Collector task orchestration — the seam between a collector and the database.

The collectors themselves are covered by the unit suite (mocked HTTP, no DB).
What is tested here is what the *tasks* add on top: reading the profile, honouring
the per-source toggle, upserting idempotently, writing the extra rows a task owns
(repository snapshots, library version bumps), and chaining curation **only** when
something new landed.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from src.db.managers.preference_manager import get_or_create_sync
from src.db.session import SyncSession
from src.models.article import Article
from src.models.library import Library, LibraryRelease, PackageEcosystem
from src.models.repository import Repository, RepositorySnapshot
from src.tasks import articles_tasks, github_tasks, packages_tasks

_GITHUB_URL = "https://api.github.com/search/repositories"
_DEVTO_URL = "https://dev.to/api/articles"
_HN_URL = "https://hn.algolia.com/api/v1/search"
_CURATE = "src.tasks.curation_tasks.curate_uncurated"


def _profile(**fields) -> None:
    """Set fields on the singleton preferences row the collectors read."""
    with SyncSession() as session:
        pref = get_or_create_sync(session)
        for key, value in fields.items():
            setattr(pref, key, value)
        session.commit()


@pytest.fixture
def chained(monkeypatch) -> list[str]:
    """Capture the tasks a collector chains instead of enqueueing them for real."""
    sent: list[str] = []
    for module in (github_tasks, packages_tasks, articles_tasks):
        monkeypatch.setattr(module.celery_app, "send_task", lambda name, *a, **kw: sent.append(name))
    return sent


# --------------------------------------------------------------------------- github


def _gh_item(full_name: str, stars: int = 500) -> dict:
    owner, name = full_name.split("/")
    return {
        "full_name": full_name,
        "owner": {"login": owner},
        "name": name,
        "description": "an llm agent framework",
        "stargazers_count": stars,
        "forks_count": 12,
        "topics": ["llm", "python"],
        "language": "Python",
        "html_url": f"https://github.com/{full_name}",
        "created_at": "2026-01-01T00:00:00Z",
    }


@respx.mock
def test_collect_trending_stores_repos_snapshots_and_chains_once(chained):
    _profile(stacks=["python"], keywords=["llm"])
    route = respx.get(_GITHUB_URL).mock(
        return_value=httpx.Response(200, json={"items": [_gh_item("acme/one"), _gh_item("acme/two", stars=90)]})
    )

    assert github_tasks.collect_trending.apply().get() == {"fetched": 2, "inserted": 2}

    with SyncSession() as session:
        repos = session.query(Repository).filter(Repository.dedup_key.in_(["acme/one", "acme/two"])).all()
        assert {r.dedup_key for r in repos} == {"acme/one", "acme/two"}
        # One metrics snapshot per freshly-inserted repo — the momentum history.
        snapshots = session.query(RepositorySnapshot).filter(
            RepositorySnapshot.repository_id.in_([r.id for r in repos])
        )
        assert snapshots.count() == 2
    assert chained == [_CURATE]

    # Re-running the collector is a no-op: nothing inserted, no new snapshot rows,
    # and curation is not chained over an empty batch.
    assert github_tasks.collect_trending.apply().get() == {"fetched": 2, "inserted": 0}
    with SyncSession() as session:
        repo_ids = [r.id for r in session.query(Repository).filter(Repository.dedup_key.like("acme/%")).all()]
        assert session.query(RepositorySnapshot).filter(RepositorySnapshot.repository_id.in_(repo_ids)).count() == 2
    assert chained == [_CURATE]
    assert route.call_count == 2


@respx.mock
def test_collect_trending_skips_when_source_disabled(chained):
    _profile(enabled_sources={"github": False})
    route = respx.get(_GITHUB_URL).mock(return_value=httpx.Response(200, json={"items": [_gh_item("acme/nope")]}))

    assert github_tasks.collect_trending.apply().get() == {"fetched": 0, "inserted": 0}

    assert route.call_count == 0  # disabled means no outbound request at all
    with SyncSession() as session:
        assert session.query(Repository).filter(Repository.dedup_key == "acme/nope").count() == 0
    assert chained == []


# ------------------------------------------------------------------------- packages


def _pypi(version: str) -> httpx.Response:
    return httpx.Response(200, json={"info": {"version": version, "summary": "web framework"}})


@respx.mock
def test_collect_releases_seeds_libraries_and_records_bumps(chained):
    # Only the well-formed entry becomes a monitored library; the other two are junk.
    _profile(monitored_libraries=["pypi:fastapi", "nope", "badeco:thing"])
    respx.get("https://pypi.org/pypi/fastapi/json").mock(return_value=_pypi("1.0.0"))

    assert packages_tasks.collect_releases.apply().get() == {"checked": 1, "new_releases": 1}

    with SyncSession() as session:
        libs = session.query(Library).all()
        assert [(x.ecosystem, x.name) for x in libs] == [(PackageEcosystem.PYPI, "fastapi")]
        assert libs[0].current_version == "1.0.0"
        release = session.query(LibraryRelease).one()
        assert (release.new_version, release.previous_version) == ("1.0.0", None)
        # First sighting is not a major bump — we have nothing to compare against.
        assert release.is_major is False
    assert chained == [_CURATE]


@respx.mock
def test_collect_releases_is_quiet_until_the_version_moves(chained):
    _profile(monitored_libraries=["pypi:fastapi"])
    respx.get("https://pypi.org/pypi/fastapi/json").mock(return_value=_pypi("1.0.0"))
    packages_tasks.collect_releases.apply().get()
    chained.clear()

    # Same version again → no release row, no curation chained.
    assert packages_tasks.collect_releases.apply().get() == {"checked": 1, "new_releases": 0}
    assert chained == []

    # A major bump is recorded as one, and carries the previous version.
    respx.get("https://pypi.org/pypi/fastapi/json").mock(return_value=_pypi("2.0.0"))
    assert packages_tasks.collect_releases.apply().get() == {"checked": 1, "new_releases": 1}
    with SyncSession() as session:
        latest = session.query(LibraryRelease).filter(LibraryRelease.new_version == "2.0.0").one()
        assert latest.is_major is True
        assert latest.previous_version == "1.0.0"
        assert session.query(Library).one().current_version == "2.0.0"
    assert chained == [_CURATE]


@respx.mock
def test_collect_releases_skips_one_dead_package_and_finishes_the_run(chained):
    _profile(monitored_libraries=["pypi:fastapi", "pypi:ghost"])
    respx.get("https://pypi.org/pypi/fastapi/json").mock(return_value=_pypi("1.0.0"))
    respx.get("https://pypi.org/pypi/ghost/json").mock(return_value=httpx.Response(404, json={"message": "not found"}))

    # The 404 is terminal for that package only — the run still checks both and
    # records the healthy one.
    assert packages_tasks.collect_releases.apply().get() == {"checked": 2, "new_releases": 1}
    with SyncSession() as session:
        assert session.query(LibraryRelease).count() == 1


# ------------------------------------------------------------------------- articles


# Relative so the ARTICLE_MAX_AGE_DAYS gate never turns this suite into a time bomb.
_RECENT = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _devto(title: str, likes: int = 5) -> dict:
    return {
        "url": f"https://dev.to/{title.replace(' ', '-').lower()}",
        "title": title,
        "user": {"name": "author"},
        "description": "excerpt",
        "public_reactions_count": likes,
        "comments_count": 1,
        "published_at": _RECENT,
    }


def _hn(title: str, points: int = 200, created: str = _RECENT) -> dict:
    return {
        "objectID": "42",
        "title": title,
        "url": f"https://news.example.com/{title.replace(' ', '-').lower()}",
        "author": "hn_user",
        "points": points,
        "num_comments": 30,
        "created_at": created,
    }


@respx.mock
def test_collect_articles_drops_stale_collapses_dups_and_chains(chained, monkeypatch):
    _profile(keywords=["rust"])
    # Medium RSS goes over feedparser (real network); the RSS path has its own unit test.
    monkeypatch.setattr(articles_tasks.articles, "fetch_rss", lambda url, source=None: [])

    respx.get(_DEVTO_URL).mock(
        return_value=httpx.Response(200, json=[_devto("Async Rust in production", likes=5), _devto("Old news")])
    )
    # Same story as the Dev.to one (title fingerprint matches) but more engagement,
    # plus one genuinely stale item that the age gate must drop.
    respx.get(_HN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    _hn("Show HN: Async Rust in production", points=900),
                    _hn("Ancient history", created="2020-01-01T00:00:00Z"),
                ]
            },
        )
    )

    result = articles_tasks.collect_articles.apply().get()
    assert result["fetched"] == 4

    with SyncSession() as session:
        titles = {a.title for a in session.query(Article).all()}
        # The syndicated pair collapsed to the higher-engagement HN copy; the
        # 2020 story never entered the pipeline.
        assert "Ancient history" not in titles
        assert "Async Rust in production" not in titles
        assert "Show HN: Async Rust in production" in titles
        assert "Old news" in titles
        assert session.query(Article).count() == result["inserted"]
    assert chained == [_CURATE]


@respx.mock
def test_collect_articles_skips_when_source_disabled(chained):
    _profile(keywords=["rust"], enabled_sources={"articles": False})
    devto = respx.get(_DEVTO_URL).mock(return_value=httpx.Response(200, json=[_devto("never fetched")]))

    assert articles_tasks.collect_articles.apply().get() == {"fetched": 0, "inserted": 0}

    assert devto.call_count == 0
    assert chained == []
