from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.db.managers.base_manager import BaseManager
from src.models.curation import Curation, CurationStatus

#: Score bands for the calibration report, high to low. Each entry is
#: ``(label, inclusive_lower_bound)``; the last one catches everything below.
SCORE_BANDS = (("0.8-1.0", 0.8), ("0.6-0.8", 0.6), ("0.4-0.6", 0.4), ("0.0-0.4", 0.0))

#: ``(mean score per status, row count per (band, status))`` — see :meth:`CurationRepository.calibration`.
Calibration = tuple[dict[CurationStatus, float], dict[tuple[str, CurationStatus], int]]


def recent_decisions_sync(session: Session, limit: int) -> dict[str, list[Curation]]:
    """
    The most recently reviewed approved/rejected curations — the few-shot feedback set.

    Runs on the **sync** engine because curation happens inside Celery tasks. The
    LLM's own ``summary``/``tags``/``importance_score`` on a reviewed row *is* the
    example, so no extra storage is needed; rows without a summary (dead-lettered
    ``failed`` ones) carry no usable example text and are skipped.

    :param session: Active sync session.
    :type session: Session
    :param limit: Max examples per decision.
    :type limit: int
    :return: ``{"approved": [...], "rejected": [...]}``, newest decision first.
    :rtype: dict[str, list[Curation]]
    """

    def _pick(status: CurationStatus) -> list[Curation]:
        query = (
            select(Curation)
            .where(
                Curation.status == status,
                Curation.reviewed_at.is_not(None),
                Curation.summary.is_not(None),
            )
            .order_by(Curation.reviewed_at.desc())
            .limit(limit)
        )
        return list(session.execute(query).scalars().all())

    return {"approved": _pick(CurationStatus.APPROVED), "rejected": _pick(CurationStatus.REJECTED)}


class CurationRepository(BaseManager[Curation]):
    def __init__(self) -> None:
        super().__init__(model=Curation)

    async def calibration(self, db: AsyncSession) -> Calibration:
        """
        Raw material for the calibration report: how the LLM scored what a human
        then approved vs. rejected.

        Only reviewed rows with a score participate — ``pending`` has no verdict to
        compare against and ``failed`` has no score.

        :param db: Database session.
        :type db: AsyncSession
        :return: ``(mean_score_by_status, row_count_by_(band, status))``.
        :rtype: Calibration
        """
        reviewed = (
            Curation.status.in_([CurationStatus.APPROVED, CurationStatus.REJECTED]),
            Curation.importance_score.is_not(None),
        )

        mean_rows = await db.execute(
            select(Curation.status, func.avg(Curation.importance_score)).where(*reviewed).group_by(Curation.status)
        )
        means = {status: float(avg) for status, avg in mean_rows.all()}

        band = case(
            *[(Curation.importance_score >= lower, label) for label, lower in SCORE_BANDS],
            else_=SCORE_BANDS[-1][0],
        ).label("band")
        band_rows = await db.execute(
            select(band, Curation.status, func.count()).where(*reviewed).group_by(band, Curation.status)
        )
        counts = {(label, status): count for label, status, count in band_rows.all()}

        return means, counts
