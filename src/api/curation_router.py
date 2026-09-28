import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.curation_manager import SCORE_BANDS, CurationRepository
from src.db.session import get_db_async_session
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.schemas.common import Page
from src.schemas.curation_schema import (
    CalibrationBand,
    CurationBulkReview,
    CurationCalibration,
    CurationRead,
    CurationReview,
    CurationStats,
)

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

    result = await _repo.paginate(
        db, page=page, per_page=per_page, order_by="-importance_score,-created_at", filters=filters
    )

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


# NOTE: before /{curation_id}, same reason as /stats.
@router.get("/calibration", response_model=CurationCalibration)
async def curation_calibration(db: AsyncSession = Depends(get_db_async_session)) -> CurationCalibration:
    """
    How well the ranker's importance score predicts your review decisions.

    Only reviewed, scored rows count. ``separation`` (mean score of approved minus
    mean score of rejected) is the number to watch: it should widen as review
    verdicts accumulate into the ranker's reference set. Near zero means the score
    is not discriminating and the feed is effectively unranked for you.
    """
    means, counts = await _repo.calibration(db)

    bands = []
    for label, _ in SCORE_BANDS:
        approved = counts.get((label, CurationStatus.APPROVED), 0)
        rejected = counts.get((label, CurationStatus.REJECTED), 0)
        total = approved + rejected
        bands.append(
            CalibrationBand(
                band=label,
                approved=approved,
                rejected=rejected,
                approval_rate=(approved / total) if total else None,
            )
        )

    approved_mean = means.get(CurationStatus.APPROVED)
    rejected_mean = means.get(CurationStatus.REJECTED)
    return CurationCalibration(
        reviewed=sum(counts.values()),
        approved_mean_score=approved_mean,
        rejected_mean_score=rejected_mean,
        # Only meaningful with both sides present — one-sided means have nothing to compare.
        separation=(approved_mean - rejected_mean) if approved_mean is not None and rejected_mean is not None else None,
        bands=bands,
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
