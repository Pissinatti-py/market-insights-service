import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.repository_manager import RepositoryRepository
from src.db.session import get_db_async_session
from src.models.repository import Repository
from src.schemas.common import Page
from src.schemas.repository_schema import (
    RepositoryMomentum,
    RepositoryRead,
    RepositorySearch,
    SnapshotRead,
)
from src.services.collectors import github

router = APIRouter(prefix="/repositories", tags=["Repositories"])
_repo = RepositoryRepository()


@router.get("", response_model=Page[RepositoryRead])
async def list_repositories(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    # Literal, not a free string: an unknown field would otherwise be dropped silently
    # and the list would come back in insertion order looking like a success.
    order_by: Literal["-relevance_score", "-stars", "-created_at", "name"] = Query("-relevance_score"),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[RepositoryRead]:
    """List stored trending repositories, most relevant first."""
    result = await _repo.paginate(
        db, page=page, per_page=per_page, order_by=order_by, expressions=[Repository.deleted_at.is_(None)]
    )
    return Page.from_result(result, items=[RepositoryRead.model_validate(r) for r in result.items])


@router.get("/{repository_id}", response_model=RepositoryRead)
async def get_repository(
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_async_session),
) -> RepositoryRead:
    """Get one repository by id."""
    repo = await _repo.get(db, repository_id)
    if repo is None or repo.deleted_at is not None:
        raise HTTPException(status_code=404, detail="repository not found")
    return RepositoryRead.model_validate(repo)


@router.get("/{repository_id}/momentum", response_model=RepositoryMomentum)
async def repository_momentum(
    repository_id: uuid.UUID,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db_async_session),
) -> RepositoryMomentum:
    """
    Star-growth over the snapshot history: snapshots in the window (oldest
    first) plus newest-minus-oldest stars/velocity deltas.
    """
    repo = await _repo.get(db, repository_id)
    if repo is None or repo.deleted_at is not None:
        raise HTTPException(status_code=404, detail="repository not found")

    since = datetime.now(timezone.utc) - timedelta(days=days)
    snaps = await _repo.snapshots_since(db, repository_id, since)
    stars_delta = snaps[-1].stars - snaps[0].stars if len(snaps) >= 2 else 0
    velocity_delta = snaps[-1].stars_per_week - snaps[0].stars_per_week if len(snaps) >= 2 else 0.0
    return RepositoryMomentum(
        repository_id=repository_id,
        window_days=days,
        snapshots=[SnapshotRead.model_validate(s) for s in snaps],
        stars_delta=stars_delta,
        velocity_delta=velocity_delta,
    )


@router.post("/search", response_model=list[RepositoryRead])
async def search_repositories(body: RepositorySearch) -> list[RepositoryRead]:
    """
    Run a live GitHub search with explicit filters (does not persist).

    Lets a caller probe GitHub on demand without waiting for the scheduled
    collector. Results are returned ranked but not stored.
    """
    now = datetime.now(timezone.utc)
    pushed_since = (now - timedelta(days=90)).date().isoformat()
    rows = github.fetch_trending(
        body.languages or [],
        body.keywords or [],
        pushed_since=pushed_since,
        min_stars=body.min_stars,
        max_results=body.max_results,
    )
    # The collector returns RepositoryCreate; project onto the read shape with a
    # null id (these are not persisted rows).
    return [
        RepositoryRead(
            id=uuid.UUID(int=0),
            created_at=now,
            **r.model_dump(),
        )
        for r in rows
    ]
