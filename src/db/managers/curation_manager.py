import uuid

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


def reviewed_examples_sync(session: Session) -> list[tuple[uuid.UUID, list[float], bool]]:
    """
    Every human verdict with an embedding — the training set of the importance ranker.

    Runs on the **sync** engine because ranking happens inside Celery tasks. Only
    approved/rejected rows carry a verdict; rows reviewed before they had an
    embedding join once ``rerank_all`` backfills them.

    :param session: Active sync session.
    :type session: Session
    :return: ``(item_id, embedding, approved)`` per reviewed row.
    :rtype: list[tuple[uuid.UUID, list[float], bool]]
    """
    rows = session.execute(
        select(Curation.item_id, Curation.embedding, Curation.status).where(
            Curation.status.in_([CurationStatus.APPROVED, CurationStatus.REJECTED]),
            Curation.embedding.is_not(None),
        )
    )
    return [(item_id, embedding, status == CurationStatus.APPROVED) for item_id, embedding, status in rows]


class CurationRepository(BaseManager[Curation]):
    def __init__(self) -> None:
        super().__init__(model=Curation)

    async def calibration(self, db: AsyncSession) -> Calibration:
        """
        Raw material for the calibration report: how the ranker scored what a human
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
