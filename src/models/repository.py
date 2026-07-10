import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base
from src.db.mixins import SoftDeleteMixin, TimestampMixin


class Repository(TimestampMixin, SoftDeleteMixin, Base):
    """
    A trending GitHub repository captured by the github collector.

    ``dedup_key`` is the ``owner/name`` slug — a repo is stored once and its
    changing metrics are tracked in :class:`RepositorySnapshot`. The collector
    upserts on ``dedup_key`` so re-runs never duplicate the row.
    """

    __tablename__ = "mi__repositories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    url: Mapped[str] = mapped_column(String(512), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stars_per_week: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)

    languages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    topics: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    repo_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    snapshots: Mapped[list["RepositorySnapshot"]] = relationship(
        back_populates="repository",
        cascade="all, delete-orphan",
    )


class RepositorySnapshot(TimestampMixin, Base):
    """
    A point-in-time stars/score reading for a repository — the change history.

    The collector appends one snapshot per run so star-growth and relevance
    drift over time can be charted. No ``dedup_key``: snapshots are append-only.
    """

    __tablename__ = "mi__repository_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mi__repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    stars_per_week: Mapped[float] = mapped_column(Float, nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    repository: Mapped["Repository"] = relationship(back_populates="snapshots")
