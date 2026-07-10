from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.preference_manager import PreferenceRepository
from src.db.session import get_db_async_session
from src.schemas.preference_schema import PreferenceRead, PreferenceUpdate, SourcesConfig

router = APIRouter(prefix="/config", tags=["Configuration"])
_repo = PreferenceRepository()


@router.get("/preferences", response_model=PreferenceRead)
async def get_preferences(db: AsyncSession = Depends(get_db_async_session)) -> PreferenceRead:
    """Get the user's technical profile (creates an empty one on first read)."""
    pref = await _repo.get_or_create(db)
    return PreferenceRead.model_validate(pref)


@router.put("/preferences", response_model=PreferenceRead)
async def update_preferences(
    body: PreferenceUpdate,
    db: AsyncSession = Depends(get_db_async_session),
) -> PreferenceRead:
    """Update the technical profile (partial — only provided fields change)."""
    pref = await _repo.get_or_create(db)
    updated = await _repo.update(db, pref.id, body.model_dump(exclude_unset=True))
    return PreferenceRead.model_validate(updated)


@router.post("/sources", response_model=PreferenceRead)
async def configure_sources(
    body: SourcesConfig,
    db: AsyncSession = Depends(get_db_async_session),
) -> PreferenceRead:
    """Enable/disable individual collectors (e.g. ``{"github": true, "articles": false}``)."""
    pref = await _repo.get_or_create(db)
    updated = await _repo.update(db, pref.id, {"enabled_sources": body.enabled_sources})
    return PreferenceRead.model_validate(updated)
