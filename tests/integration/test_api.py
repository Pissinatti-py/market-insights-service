"""End-to-end API checks over the in-process ASGI app + real Postgres."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.article import Article, ArticleSource
from src.models.curation import Curation, CurationItemType, CurationStatus
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


async def _seed_curation(db: AsyncSession, item_type, item_id, score, status=CurationStatus.APPROVED) -> Curation:
    cur = Curation(
        item_type=item_type,
        item_id=item_id,
        summary=f"summary {score}",
        tags=["x"],
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
async def test_preferences_roundtrip(client: AsyncClient):
    empty = (await client.get("/api/config/preferences")).json()
    assert empty["stacks"] == []
    assert empty["profile_empty"] is True

    updated = await client.put("/api/config/preferences", json={"stacks": ["rust"], "keywords": ["llm"]})
    assert updated.json()["stacks"] == ["rust"]
    assert updated.json()["profile_empty"] is False

    sources = await client.post("/api/config/sources", json={"enabled_sources": {"github": False}})
    assert sources.json()["enabled_sources"] == {"github": False}
