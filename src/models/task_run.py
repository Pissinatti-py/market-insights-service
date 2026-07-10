import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin


class TaskRun(TimestampMixin, Base):
    """
    One completed Celery task execution — the "did last night's run happen?" ledger.

    Written by the ``task_postrun`` signal (``src/core/celery/task_runs.py``) for
    every task, success or failure. ``/status`` surfaces the latest row per
    ``task_name``.
    """

    __tablename__ = "mi__task_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    task_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # ponytail: Celery's state string (SUCCESS/FAILURE/RETRY), not an enum — the
    # set is Celery's, not ours.
    state: Mapped[str] = mapped_column(String(32), nullable=False)

    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
