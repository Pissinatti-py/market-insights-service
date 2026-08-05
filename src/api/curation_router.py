import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.curation_manager import CurationRepository
from src.db.session import get_db_async_session
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.schemas.common import Page
from src.schemas.curation_schema import CurationBulkReview, CurationRead, CurationReview, CurationStats

router = APIRouter(prefix="/curation", tags=["Curation"])
_repo = CurationRepository()


@router.get("", response_model=Page[CurationRead])
async def list_curation(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: CurationStatus | None = Query(None),
    item_type: CurationItemType | None = Query(None),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[CurationRead]:
    """List curation results, most important first, optionally filtered by status and item type."""
    filters = {}
    if status is not None:
        filters["status"] = status
    if item_type is not None:
        filters["item_type"] = item_type

    result = await _repo.paginate(db, page=page, per_page=per_page, order_by="-importance_score", filters=filters)

    return Page.from_result(result, items=[CurationRead.model_validate(c) for c in result.items])


# NOTE: before /{curation_id} so "stats" is not parsed as an id.
@router.get("/stats", response_model=CurationStats)
async def curation_stats(db: AsyncSession = Depends(get_db_async_session)) -> CurationStats:
    """Aggregate counts of curation rows by review status."""
    rows = await db.execute(select(Curation.status, func.count()).group_by(Curation.status))
    counts = {status: count for status, count in rows.all()}
    return CurationStats(
        total=sum(counts.values()),
        pending=counts.get(CurationStatus.PENDING, 0),
        approved=counts.get(CurationStatus.APPROVED, 0),
        rejected=counts.get(CurationStatus.REJECTED, 0),
        failed=counts.get(CurationStatus.FAILED, 0),
    )


@router.put("/review")
async def bulk_review_curation(
    body: CurationBulkReview,
    db: AsyncSession = Depends(get_db_async_session),
) -> dict:
    """
    Apply one review decision to many curation rows in a single UPDATE.

    Unknown ids are ignored — the response says how many rows actually changed.

    :return: ``{"updated": int}``.
    :rtype: dict
    """
    result = await db.execute(
        update(Curation)
        .where(Curation.id.in_(body.ids))
        .values(status=body.status, reviewed_by=body.reviewed_by, reviewed_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"updated": result.rowcount}


@router.put("/{curation_id}/review", response_model=CurationRead)
async def review_curation(
    curation_id: uuid.UUID,
    body: CurationReview,
    db: AsyncSession = Depends(get_db_async_session),
) -> CurationRead:
    """Manually approve/reject a curation result."""
    updated = await _repo.update(
        db,
        curation_id,
        {"status": body.status, "reviewed_by": body.reviewed_by, "reviewed_at": datetime.now(timezone.utc)},
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="curation not found")
    return CurationRead.model_validate(updated)
