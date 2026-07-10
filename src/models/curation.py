import enum
import uuid
from decimal import Decimal

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin


class CurationItemType(str, enum.Enum):
    """Which collected entity a curation row analyses."""

    REPOSITORY = "repository"
    LIBRARY_RELEASE = "library_release"
    ARTICLE = "article"


class CurationStatus(str, enum.Enum):
    """Review lifecycle of an AI curation result."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # Dead-letter: the LLM output never validated for this item — the row keeps the
    # item out of the uncurated selection; recurate_all is the retry path.
    FAILED = "failed"


class Curation(TimestampMixin, Base):
    """
    An AI-generated analysis of one collected item.

    Polymorphic by ``(item_type, item_id)`` — there is no DB-level FK because the
    target lives in one of three tables. A partial design choice mirroring
    pissync's provenance pattern: the raw LLM output is kept in ``raw_llm_output``
    (JSONB) for debugging, the validated summary/tags/score are first-class
    columns, and ``status`` drives manual review.

    The ``(item_type, item_id)`` unique constraint makes curation idempotent: an
    item is curated at most once, so re-running the curation task skips it.
    """

    __tablename__ = "mi__curation"

    __table_args__ = (UniqueConstraint("item_type", "item_id", name="uq_mi__curation__item"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    item_type: Mapped[CurationItemType] = mapped_column(
        SAEnum(CurationItemType, name="curation_item_type_enum", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    importance_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True, index=True)

    status: Mapped[CurationStatus] = mapped_column(
        SAEnum(CurationStatus, name="curation_status_enum", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=CurationStatus.PENDING,
        index=True,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_llm_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
