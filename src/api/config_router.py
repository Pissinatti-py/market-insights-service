from collections import Counter

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.preference_manager import PreferenceRepository
from src.db.session import get_db_async_session
from src.models.curation import Curation, CurationStatus
from src.schemas.preference_schema import KeywordSuggestion, PreferenceRead, PreferenceUpdate, SourcesConfig

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


async def _tag_counts(db: AsyncSession, status: CurationStatus) -> Counter:
    """Count tag occurrences across curations in one review status."""
    rows = await db.execute(select(Curation.tags).where(Curation.status == status))
    # Counted in Python, not with jsonb_array_elements: a single-user feed reviews
    # hundreds of rows, not millions, and this is a third of the code.
    return Counter(tag for tags in rows.scalars() if tags for tag in tags)


@router.get("/keyword-suggestions", response_model=list[KeywordSuggestion])
async def keyword_suggestions(
    limit: int = Query(15, ge=1, le=100),
    db: AsyncSession = Depends(get_db_async_session),
) -> list[KeywordSuggestion]:
    """
    Candidate search terms mined from what you approved.

    Tags the LLM assigned to approved items, minus the ones already in your profile,
    ranked by approvals-minus-rejections. Read-only on purpose: adding one is a
    deliberate ``PUT /api/config/preferences``, so the profile never narrows itself.
    """
    approved = await _tag_counts(db, CurationStatus.APPROVED)
    rejected = await _tag_counts(db, CurationStatus.REJECTED)

    pref = await _repo.get_or_create(db)
    known = {
        term.lower()
        for field in (pref.keywords, pref.stacks, pref.areas, pref.monitored_libraries)
        for term in (field or [])
    }

    suggestions = [
        KeywordSuggestion(
            keyword=tag,
            approved_count=count,
            rejected_count=rejected.get(tag, 0),
            net=count - rejected.get(tag, 0),
        )
        for tag, count in approved.items()
        if tag.lower() not in known and count > rejected.get(tag, 0)
    ]
    suggestions.sort(key=lambda s: (-s.net, -s.approved_count, s.keyword))
    return suggestions[:limit]


@router.post("/sources", response_model=PreferenceRead)
async def configure_sources(
    body: SourcesConfig,
    db: AsyncSession = Depends(get_db_async_session),
) -> PreferenceRead:
    """Enable/disable individual collectors (e.g. ``{"github": true, "articles": false}``)."""
    pref = await _repo.get_or_create(db)
    updated = await _repo.update(db, pref.id, {"enabled_sources": body.enabled_sources})
    return PreferenceRead.model_validate(updated)
