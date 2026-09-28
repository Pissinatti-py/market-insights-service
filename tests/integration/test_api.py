"""End-to-end API checks over the in-process ASGI app + real Postgres."""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.article import Article, ArticleSource
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.models.library import Library, LibraryRelease, PackageEcosystem
from src.models.repository import Repository, RepositorySnapshot


async def _seed_repo(db: AsyncSession, dedup_key="octo/widget", relevance=9.0) -> Repository:
    repo = Repository(
        dedup_key=dedup_key,
        url=f"https://github.com/{dedup_key}",
        owner=dedup_key.split("/")[0],
        name=dedup_key.split("/")[1],
        stars=500,
        forks=10,
        stars_per_week=12.0,
        relevance_score=relevance,
        languages=["rust"],
        topics=["cli"],
        collected_at=datetime.now(timezone.utc),
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


@pytest.mark.asyncio
async def test_health_and_status(client: AsyncClient):
    assert (await client.get("/health")).status_code == 200
    body = (await client.get("/status")).json()
    assert body["database"] is True
    assert body["profile_empty"] is True  # fresh DB — nothing configured yet


@pytest.mark.asyncio
async def test_repositories_list_and_get(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    listed = await client.get("/api/repositories")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    one = await client.get(f"/api/repositories/{repo.id}")
    assert one.status_code == 200
    assert one.json()["dedup_key"] == "octo/widget"

    missing = await client.get("/api/repositories/00000000-0000-0000-0000-0000000000ff")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_repositories_ordered_by_relevance(client: AsyncClient, db_session: AsyncSession):
    await _seed_repo(db_session, "low/score", relevance=1.0)
    await _seed_repo(db_session, "high/score", relevance=50.0)
    items = (await client.get("/api/repositories")).json()["items"]
    assert items[0]["dedup_key"] == "high/score"


async def _seed_snapshot(db: AsyncSession, repo: Repository, days_ago: int, stars: int, spw: float) -> None:
    db.add(
        RepositorySnapshot(
            repository_id=repo.id,
            stars=stars,
            stars_per_week=spw,
            relevance_score=float(stars) / 100,
            captured_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_repository_momentum_computes_deltas(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    await _seed_snapshot(db_session, repo, days_ago=20, stars=100, spw=10.0)
    await _seed_snapshot(db_session, repo, days_ago=10, stars=150, spw=12.0)
    await _seed_snapshot(db_session, repo, days_ago=1, stars=300, spw=20.0)

    body = (await client.get(f"/api/repositories/{repo.id}/momentum")).json()
    assert len(body["snapshots"]) == 3
    # Oldest first, deltas = newest - oldest within the window.
    assert body["snapshots"][0]["stars"] == 100
    assert body["stars_delta"] == 200
    assert body["velocity_delta"] == 10.0

    # Narrower window drops the 20-day-old snapshot.
    narrow = (await client.get(f"/api/repositories/{repo.id}/momentum?days=15")).json()
    assert len(narrow["snapshots"]) == 2
    assert narrow["stars_delta"] == 150


@pytest.mark.asyncio
async def test_repository_momentum_missing_repo_404(client: AsyncClient):
    resp = await client.get("/api/repositories/00000000-0000-0000-0000-0000000000ff/momentum")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_libraries_add_and_remove(client: AsyncClient):
    created = await client.post("/api/libraries", json={"ecosystem": "pypi", "name": "httpx"})
    assert created.status_code == 201
    lib_id = created.json()["id"]

    # Idempotent add returns the same row, not a duplicate.
    again = await client.post("/api/libraries", json={"ecosystem": "pypi", "name": "httpx"})
    assert again.json()["id"] == lib_id

    assert (await client.get("/api/libraries")).json()["total"] == 1
    assert (await client.delete(f"/api/libraries/{lib_id}")).status_code == 204
    assert (await client.get("/api/libraries")).json()["total"] == 0


@pytest.mark.asyncio
async def test_curation_review_and_stats(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    cur = Curation(
        item_type=CurationItemType.REPOSITORY,
        item_id=repo.id,
        summary="A neat cli",
        tags=["cli"],
        importance_score=0.7,
        status=CurationStatus.PENDING,
    )
    db_session.add(cur)
    await db_session.commit()
    await db_session.refresh(cur)

    stats = (await client.get("/api/curation/stats")).json()
    assert stats["total"] == 1 and stats["pending"] == 1

    reviewed = await client.put(f"/api/curation/{cur.id}/review", json={"status": "approved", "reviewed_by": "me"})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["reviewed_at"] is not None  # the approval moment, stamped on decision

    stats2 = (await client.get("/api/curation/stats")).json()
    assert stats2["approved"] == 1 and stats2["pending"] == 0


@pytest.mark.asyncio
async def test_curation_bulk_review(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    article = await _seed_article(db_session)
    a = await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.7, status=CurationStatus.PENDING)
    b = await _seed_curation(db_session, CurationItemType.ARTICLE, article.id, 0.5, status=CurationStatus.PENDING)

    unknown_id = str(uuid.uuid4())  # ignored, not an error
    resp = await client.put(
        "/api/curation/review",
        json={"ids": [str(a.id), str(b.id), unknown_id], "status": "approved", "reviewed_by": "me"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2}

    stats = (await client.get("/api/curation/stats")).json()
    assert stats["approved"] == 2 and stats["pending"] == 0

    listed = (await client.get("/api/curation?status=approved")).json()
    assert all(c["reviewed_by"] == "me" for c in listed["items"])
    assert all(isinstance(c["importance_score"], float) for c in listed["items"])
    # The bulk path is a raw Core UPDATE — it has to stamp reviewed_at itself.
    assert all(c["reviewed_at"] is not None for c in listed["items"])


async def _seed_article(db: AsyncSession, title="Async Rust", url="https://blog.example.com/rust") -> Article:
    article = Article(
        dedup_key=f"devto:{title}",
        source=ArticleSource.DEVTO,
        title=title,
        url=url,
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)
    return article


async def _seed_curation(
    db: AsyncSession, item_type, item_id, score, status=CurationStatus.APPROVED, tags=None
) -> Curation:
    cur = Curation(
        item_type=item_type,
        item_id=item_id,
        summary=f"summary {score}",
        tags=["x"] if tags is None else tags,
        importance_score=score,
        status=status,
    )
    db.add(cur)
    await db.commit()
    await db.refresh(cur)
    return cur


@pytest.mark.asyncio
async def test_feed_defaults_to_pending_plus_approved_ranked(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    article = await _seed_article(db_session)
    fresh = await _seed_article(db_session, title="Pending one", url="https://blog.example.com/pending")
    noise = await _seed_article(db_session, title="Rejected one", url="https://blog.example.com/rejected")
    broken = await _seed_article(db_session, title="Failed one", url="https://blog.example.com/failed")

    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9)
    await _seed_curation(db_session, CurationItemType.ARTICLE, article.id, 0.4)
    await _seed_curation(db_session, CurationItemType.ARTICLE, fresh.id, 0.99, status=CurationStatus.PENDING)
    await _seed_curation(db_session, CurationItemType.ARTICLE, noise.id, 0.95, status=CurationStatus.REJECTED)
    await _seed_curation(db_session, CurationItemType.ARTICLE, broken.id, 0.95, status=CurationStatus.FAILED)

    # Default: pending + approved, ranked by score — rejected/failed never surface.
    body = (await client.get("/api/feed")).json()
    assert body["total"] == 3
    first, second, third = body["items"]
    assert first["title"] == "Pending one"
    assert second["item_type"] == "repository"
    assert second["title"] == "octo/widget"
    assert second["url"] == "https://github.com/octo/widget"
    assert second["importance_score"] == 0.9  # JSON number, not Decimal's "0.900" string
    assert third["title"] == "Async Rust"

    approved_only = (await client.get("/api/feed?status=approved")).json()
    assert approved_only["total"] == 2

    pending = (await client.get("/api/feed?status=pending")).json()
    assert pending["total"] == 1
    assert pending["items"][0]["title"] == "Pending one"


@pytest.mark.asyncio
async def test_feed_orders_by_reviewed_at(client: AsyncClient, db_session: AsyncSession):
    """The 'recently approved' list: newest decision first, regardless of score."""
    low = await _seed_article(db_session, title="Approved first", url="https://blog.example.com/first")
    high = await _seed_article(db_session, title="Approved second", url="https://blog.example.com/second")
    a = await _seed_curation(db_session, CurationItemType.ARTICLE, low.id, 0.1, status=CurationStatus.PENDING)
    b = await _seed_curation(db_session, CurationItemType.ARTICLE, high.id, 0.99, status=CurationStatus.PENDING)

    for cur in (a, b):  # a approved before b
        assert (await client.put(f"/api/curation/{cur.id}/review", json={"status": "approved"})).status_code == 200

    by_score = (await client.get("/api/feed?status=approved")).json()["items"]
    assert [i["title"] for i in by_score] == ["Approved second", "Approved first"]

    by_review = (await client.get("/api/feed?status=approved&order_by=-reviewed_at")).json()["items"]
    assert [i["title"] for i in by_review] == ["Approved second", "Approved first"]

    # Same ranking either way above (0.99 approved last), so flip one decision to prove
    # the ordering really follows reviewed_at and not the score.
    await client.put(f"/api/curation/{a.id}/review", json={"status": "approved"})
    flipped = (await client.get("/api/feed?status=approved&order_by=-reviewed_at")).json()["items"]
    assert [i["title"] for i in flipped] == ["Approved first", "Approved second"]

    assert (await client.get("/api/feed?order_by=-bogus")).status_code == 422


@pytest.mark.asyncio
async def test_feed_puts_unscored_last_and_breaks_ties_newest_first(client: AsyncClient, db_session: AsyncSession):
    """A revived dead letter has no score until rerank_all — it must not jump the whole feed."""
    now = datetime.now(timezone.utc)
    seeded = [("Unscored", None, 0), ("Tie older", 1.0, 2), ("Tie newer", 1.0, 1), ("Low", 0.2, 3)]
    for title, score, days_old in seeded:
        article = await _seed_article(db_session, title=title, url=f"https://blog.example.com/{days_old}")
        cur = await _seed_curation(
            db_session, CurationItemType.ARTICLE, article.id, score, status=CurationStatus.PENDING
        )
        cur.created_at = now - timedelta(days=days_old)
    await db_session.commit()

    items = (await client.get("/api/feed")).json()["items"]
    assert [i["title"] for i in items] == ["Tie newer", "Tie older", "Low", "Unscored"]


@pytest.mark.asyncio
async def test_preferences_roundtrip(client: AsyncClient):
    empty = (await client.get("/api/config/preferences")).json()
    assert empty["stacks"] == []
    assert empty["profile_empty"] is True

    updated = await client.put("/api/config/preferences", json={"stacks": ["rust"], "keywords": ["llm"]})
    assert updated.json()["stacks"] == ["rust"]
    assert updated.json()["profile_empty"] is False

    sources = await client.post("/api/config/sources", json={"enabled_sources": {"github": False}})
    assert sources.json()["enabled_sources"] == {"github": False}


@pytest.mark.asyncio
async def test_feed_filters(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    article = await _seed_article(db_session)
    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9)
    low = await _seed_curation(db_session, CurationItemType.ARTICLE, article.id, 0.3)
    low.tags = ["python", "web"]
    await db_session.commit()

    by_type = (await client.get("/api/feed?item_type=repository")).json()
    assert by_type["total"] == 1 and by_type["items"][0]["item_type"] == "repository"

    by_score = (await client.get("/api/feed?min_score=0.5")).json()
    assert by_score["total"] == 1 and by_score["items"][0]["importance_score"] == 0.9

    by_tag = (await client.get("/api/feed?tag=python")).json()
    assert by_tag["total"] == 1 and by_tag["items"][0]["item_type"] == "article"

    by_since = (await client.get("/api/feed?since=2099-01-01T00:00:00Z")).json()
    assert by_since["total"] == 0


@pytest.mark.asyncio
async def test_sources_config_rejects_unknown_key(client: AsyncClient):
    resp = await client.post("/api/config/sources", json={"enabled_sources": {"article": False}})
    assert resp.status_code == 422
    resp2 = await client.put("/api/config/preferences", json={"enabled_sources": {"linkedin": True}})
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_root_serves_review_ui(client: AsyncClient):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Radar" in resp.text


@pytest.mark.asyncio
async def test_digest_windows_and_renders(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    fresh = await _seed_article(db_session, title="This week", url="https://blog.example.com/week")
    stale = await _seed_article(db_session, title="Last month", url="https://blog.example.com/month")
    low = await _seed_article(db_session, title="Marginal", url="https://blog.example.com/low")

    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9)
    await _seed_curation(db_session, CurationItemType.ARTICLE, fresh.id, 0.8)
    await _seed_curation(db_session, CurationItemType.ARTICLE, low.id, 0.2)
    old = await _seed_curation(db_session, CurationItemType.ARTICLE, stale.id, 0.95)
    old.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    await db_session.commit()

    body = (await client.get("/api/feed/digest?days=7")).json()
    assert [i["title"] for i in body["items"]] == ["octo/widget", "This week", "Marginal"]

    assert [i["title"] for i in (await client.get("/api/feed/digest?days=7&min_score=0.5")).json()["items"]] == [
        "octo/widget",
        "This week",
    ]
    assert len((await client.get("/api/feed/digest?days=90")).json()["items"]) == 4  # window opens up

    md = await client.get("/api/feed/digest?days=7&format=markdown")
    assert "text/markdown" in md.headers["content-type"]
    assert "# Insights — last 7 days" in md.text
    assert "## Repositories" in md.text and "## Articles" in md.text
    assert "[This week](https://blog.example.com/week)" in md.text
    assert "score 0.90" in md.text
    assert "Last month" not in md.text  # outside the window


@pytest.mark.asyncio
async def test_feed_renders_library_releases(client: AsyncClient, db_session: AsyncSession):
    """A release shows up as '<library> <version>' — the third render branch."""
    library = Library(dedup_key="pypi:fastapi", ecosystem=PackageEcosystem.PYPI, name="fastapi")
    db_session.add(library)
    await db_session.commit()
    await db_session.refresh(library)
    release = LibraryRelease(
        dedup_key="pypi:fastapi:2.0.0",
        library_id=library.id,
        previous_version="1.0.0",
        new_version="2.0.0",
        is_major=True,
        released_at=datetime.now(timezone.utc),
    )
    db_session.add(release)
    await db_session.commit()
    await db_session.refresh(release)
    await _seed_curation(db_session, CurationItemType.LIBRARY_RELEASE, release.id, 0.85)

    item = (await client.get("/api/feed?item_type=library_release")).json()["items"][0]
    assert item["title"] == "fastapi 2.0.0"
    assert item["url"] is None  # releases carry no canonical link

    md = (await client.get("/api/feed/digest?format=markdown")).text
    assert "## Releases" in md
    assert "- **fastapi 2.0.0** —" in md  # no link markup when there is no url


@pytest.mark.asyncio
async def test_missing_rows_are_404s(client: AsyncClient):
    ghost = uuid.uuid4()
    assert (await client.put(f"/api/curation/{ghost}/review", json={"status": "approved"})).status_code == 404
    assert (await client.delete(f"/api/libraries/{ghost}")).status_code == 404
    assert (await client.get(f"/api/libraries/{ghost}")).status_code == 404


@pytest.mark.asyncio
async def test_curation_list_filters_by_item_type(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    article = await _seed_article(db_session)
    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9)
    await _seed_curation(db_session, CurationItemType.ARTICLE, article.id, 0.4)

    listed = (await client.get("/api/curation?item_type=article")).json()
    assert listed["total"] == 1 and listed["items"][0]["item_type"] == "article"


@pytest.mark.asyncio
async def test_tasks_are_discoverable(client: AsyncClient):
    listed = (await client.get("/api/tasks")).json()
    assert "collect_trending" in listed
    assert listed["collect_trending"] == "src.tasks.github_tasks.collect_trending"


@pytest.mark.asyncio
async def test_digest_empty_window_still_renders(client: AsyncClient):
    assert (await client.get("/api/feed/digest")).json()["items"] == []
    assert "Nothing curated in this window" in (await client.get("/api/feed/digest?format=markdown")).text


@pytest.mark.asyncio
async def test_repositories_reject_unknown_order_field(client: AsyncClient, db_session: AsyncSession):
    """An unknown sort field is a 422 — never a silently unordered list."""
    await _seed_repo(db_session)
    assert (await client.get("/api/repositories?order_by=bogus")).status_code == 422
    assert (await client.get("/api/repositories?order_by=-stars")).status_code == 200


@pytest.mark.asyncio
async def test_repository_live_search(client: AsyncClient):
    """The live GitHub probe returns ranked, unpersisted rows."""
    payload = {
        "items": [
            {
                "full_name": "acme/tool",
                "owner": {"login": "acme"},
                "name": "tool",
                "description": "an llm agent",
                "stargazers_count": 900,
                "forks_count": 3,
                "topics": ["llm"],
                "language": "Python",
                "html_url": "https://github.com/acme/tool",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    with respx.mock:
        respx.get("https://api.github.com/search/repositories").mock(return_value=httpx.Response(200, json=payload))
        resp = await client.post("/api/repositories/search", json={"languages": ["python"], "keywords": ["llm"]})

    assert resp.status_code == 200
    assert [r["dedup_key"] for r in resp.json()] == ["acme/tool"]
    # Nothing was persisted — this endpoint only probes.
    assert (await client.get("/api/repositories")).json()["total"] == 0


@pytest.mark.asyncio
async def test_articles_search_and_get(client: AsyncClient, db_session: AsyncSession):
    article = await _seed_article(db_session, title="Async Rust", url="https://blog.example.com/rust")
    article.author = "Jane Roe"
    article.content = "a deep dive into tokio"
    other = await _seed_article(db_session, title="Postgres tips", url="https://blog.example.com/pg")
    await db_session.commit()

    by_title = (await client.get("/api/articles/search?q=rust")).json()
    assert [a["title"] for a in by_title] == ["Async Rust"]
    assert [a["title"] for a in (await client.get("/api/articles/search?q=jane")).json()] == ["Async Rust"]
    assert [a["title"] for a in (await client.get("/api/articles/search?q=tokio")).json()] == ["Async Rust"]
    assert (await client.get("/api/articles/search?q=nothing-matches")).json() == []

    assert (await client.get(f"/api/articles/{other.id}")).json()["title"] == "Postgres tips"
    assert (await client.get(f"/api/articles/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/api/articles")).json()["total"] == 2


@pytest.mark.asyncio
async def test_library_history_and_remonitoring(client: AsyncClient, db_session: AsyncSession):
    created = (await client.post("/api/libraries", json={"ecosystem": "pypi", "name": "fastapi"})).json()
    lib_id = created["id"]

    now = datetime.now(timezone.utc)
    for version, released in (("1.0.0", now - timedelta(days=30)), ("2.0.0", now)):
        db_session.add(
            LibraryRelease(
                dedup_key=f"pypi:fastapi:{version}",
                library_id=uuid.UUID(lib_id),
                new_version=version,
                is_major=version.startswith("2"),
                released_at=released,
            )
        )
    await db_session.commit()

    history = (await client.get(f"/api/libraries/{lib_id}")).json()
    assert [r["new_version"] for r in history] == ["2.0.0", "1.0.0"]  # newest release first

    # Un-monitoring is a soft delete: re-adding the same library revives that row
    # (same id) rather than creating a second one, so the history survives.
    assert (await client.delete(f"/api/libraries/{lib_id}")).status_code == 204
    assert (await client.get(f"/api/libraries/{lib_id}")).status_code == 404

    revived = (await client.post("/api/libraries", json={"ecosystem": "pypi", "name": "fastapi"})).json()
    assert revived["id"] == lib_id
    assert revived["is_monitored"] is True
    assert len((await client.get(f"/api/libraries/{lib_id}")).json()) == 2


@pytest.mark.asyncio
async def test_curation_calibration_separates_approved_from_rejected(client: AsyncClient, db_session: AsyncSession):
    """The headline metric: did the model score what you kept above what you dropped?"""
    repo = await _seed_repo(db_session)
    kept = await _seed_article(db_session, title="Kept", url="https://blog.example.com/kept")
    dropped = await _seed_article(db_session, title="Dropped", url="https://blog.example.com/dropped")
    unreviewed = await _seed_article(db_session, title="Pending", url="https://blog.example.com/pending")

    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9, CurationStatus.APPROVED)
    await _seed_curation(db_session, CurationItemType.ARTICLE, kept.id, 0.7, CurationStatus.APPROVED)
    await _seed_curation(db_session, CurationItemType.ARTICLE, dropped.id, 0.3, CurationStatus.REJECTED)
    # Pending has no verdict to compare a score against — it must not skew the means.
    await _seed_curation(db_session, CurationItemType.ARTICLE, unreviewed.id, 0.1, CurationStatus.PENDING)

    body = (await client.get("/api/curation/calibration")).json()
    assert body["reviewed"] == 3
    assert body["approved_mean_score"] == pytest.approx(0.8)
    assert body["rejected_mean_score"] == pytest.approx(0.3)
    assert body["separation"] == pytest.approx(0.5)

    bands = {b["band"]: b for b in body["bands"]}
    assert bands["0.8-1.0"] == {"band": "0.8-1.0", "approved": 1, "rejected": 0, "approval_rate": 1.0}
    assert bands["0.0-0.4"] == {"band": "0.0-0.4", "approved": 0, "rejected": 1, "approval_rate": 0.0}
    assert bands["0.4-0.6"]["approval_rate"] is None  # empty band, not a 0% one


@pytest.mark.asyncio
async def test_curation_calibration_without_both_sides_has_no_separation(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    await _seed_curation(db_session, CurationItemType.REPOSITORY, repo.id, 0.9, CurationStatus.APPROVED)

    body = (await client.get("/api/curation/calibration")).json()
    assert body["approved_mean_score"] == pytest.approx(0.9)
    assert body["rejected_mean_score"] is None
    assert body["separation"] is None


@pytest.mark.asyncio
async def test_keyword_suggestions_rank_by_net_and_skip_known_terms(client: AsyncClient, db_session: AsyncSession):
    repo = await _seed_repo(db_session)
    kept = await _seed_article(db_session, title="Kept", url="https://blog.example.com/kept")
    dropped = await _seed_article(db_session, title="Dropped", url="https://blog.example.com/dropped")

    await client.put("/api/config/preferences", json={"keywords": ["Rust"], "stacks": ["python"]})

    # "rust"/"python" are already in the profile; "async" nets 0 (approved once, rejected once).
    await _seed_curation(
        db_session, CurationItemType.REPOSITORY, repo.id, 0.9, CurationStatus.APPROVED, tags=["wasm", "rust", "async"]
    )
    await _seed_curation(
        db_session, CurationItemType.ARTICLE, kept.id, 0.8, CurationStatus.APPROVED, tags=["wasm", "python"]
    )
    await _seed_curation(
        db_session, CurationItemType.ARTICLE, dropped.id, 0.2, CurationStatus.REJECTED, tags=["async", "seo"]
    )

    body = (await client.get("/api/config/keyword-suggestions")).json()
    assert [s["keyword"] for s in body] == ["wasm"]
    assert body[0] == {"keyword": "wasm", "approved_count": 2, "rejected_count": 0, "net": 2}
