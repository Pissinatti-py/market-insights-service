import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base
from src.db.mixins import SoftDeleteMixin, TimestampMixin


class PackageEcosystem(str, enum.Enum):
    """Where a monitored library lives."""

    PYPI = "pypi"
    NPM = "npm"


def _ecosystem_enum(name: str) -> SAEnum:
    return SAEnum(
        PackageEcosystem,
        name=name,
        values_callable=lambda obj: [e.value for e in obj],
    )


class Library(TimestampMixin, SoftDeleteMixin, Base):
    """
    A library the user monitors for new releases.

    ``dedup_key`` is ``<ecosystem>:<name>`` so the same package can be tracked
    once per ecosystem. ``current_version`` is advanced as the collector sees
    newer releases; the diff drives :class:`LibraryRelease` rows.
    """

    __tablename__ = "mi__libraries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    ecosystem: Mapped[PackageEcosystem] = mapped_column(_ecosystem_enum("package_ecosystem_enum"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_monitored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    releases: Mapped[list["LibraryRelease"]] = relationship(
        back_populates="library",
        cascade="all, delete-orphan",
    )


class LibraryRelease(TimestampMixin, Base):
    """
    A single new release of a monitored library.

    ``dedup_key`` is ``<ecosystem>:<name>:<version>`` so each version is recorded
    once. ``is_major`` flags a major-version bump (likely breaking changes).
    """

    __tablename__ = "mi__library_releases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mi__libraries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    previous_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_version: Mapped[str] = mapped_column(String(64), nullable=False)
    is_major: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    library: Mapped["Library"] = relationship(back_populates="releases")
