from datetime import datetime

from pytz import utc
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` timestamp columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(utc),
        onupdate=lambda: datetime.now(utc),
        nullable=False,
    )


class SoftDeleteMixin:
    """Adds ``deleted_at`` for non-destructive deletes (audit trail).

    A row with ``deleted_at IS NULL`` is active. Managers that opt in filter
    ``deleted_at IS NULL`` in every read and stamp the column instead of issuing
    SQL ``DELETE``. Indexed because every read carries the ``deleted_at IS NULL``
    predicate.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
