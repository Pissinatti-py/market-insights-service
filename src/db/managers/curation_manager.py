from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.managers.base_manager import BaseManager
from src.models.curation import Curation, CurationStatus


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
