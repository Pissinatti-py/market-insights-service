import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.library_manager import LibraryRepository
from src.db.session import get_db_async_session
from src.models.library import Library, LibraryRelease
from src.schemas.common import Page
from src.schemas.library_schema import LibraryCreate, LibraryRead, LibraryReleaseRead

router = APIRouter(prefix="/libraries", tags=["Libraries"])
_repo = LibraryRepository()


@router.get("", response_model=Page[LibraryRead])
async def list_libraries(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[LibraryRead]:
    """List monitored libraries."""
    result = await _repo.paginate(
        db, page=page, per_page=per_page, order_by="name", expressions=[Library.deleted_at.is_(None)]
    )
    return Page(
        total=result.total,
        items=[LibraryRead.model_validate(x) for x in result.items],
        page=result.page,
        per_page=result.per_page,
        num_pages=result.num_pages,
    )


@router.get("/{library_id}", response_model=list[LibraryReleaseRead])
async def library_release_history(
    library_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_async_session),
) -> list[LibraryReleaseRead]:
    """Get a library's release history, newest first."""
    lib = await _repo.get(db, library_id)
    if lib is None or lib.deleted_at is not None:
        raise HTTPException(status_code=404, detail="library not found")
    rows = await db.execute(
        select(LibraryRelease)
        .where(LibraryRelease.library_id == library_id)
        .order_by(LibraryRelease.released_at.desc().nullslast())
    )
    return [LibraryReleaseRead.model_validate(r) for r in rows.scalars().all()]


@router.post("", response_model=LibraryRead, status_code=201)
async def add_library(body: LibraryCreate, db: AsyncSession = Depends(get_db_async_session)) -> LibraryRead:
    """Add a library to monitoring (idempotent on ``ecosystem:name``)."""
    dedup_key = f"{body.ecosystem.value}:{body.name}"
    existing = await _repo.get_by_field(db, "dedup_key", dedup_key)
    if existing is not None:
        if existing.deleted_at is not None or not existing.is_monitored:
            existing.deleted_at = None
            existing.is_monitored = True
            await db.commit()
            await db.refresh(existing)
        return LibraryRead.model_validate(existing)
    created = await _repo.create(
        db, {"dedup_key": dedup_key, "ecosystem": body.ecosystem, "name": body.name, "is_monitored": True}
    )
    return LibraryRead.model_validate(created)


@router.delete("/{library_id}", status_code=204)
async def remove_library(library_id: uuid.UUID, db: AsyncSession = Depends(get_db_async_session)) -> None:
    """Stop monitoring a library (soft delete — release history is preserved)."""
    lib = await _repo.get(db, library_id)
    if lib is None or lib.deleted_at is not None:
        raise HTTPException(status_code=404, detail="library not found")
    lib.is_monitored = False
    await _repo.soft_delete(db, library_id)
