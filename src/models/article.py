import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import SoftDeleteMixin, TimestampMixin


class ArticleSource(str, enum.Enum):
    """Origin feed of a collected article."""

    DEVTO = "devto"
    MEDIUM = "medium"
    RSS = "rss"
    HACKERNEWS = "hackernews"


class Article(TimestampMixin, SoftDeleteMixin, Base):
    """
    A technical article / post captured by the articles collector.

    ``dedup_key`` is a hash of ``source`` + ``url`` so the same post from one
    feed is stored once. ``content`` holds the excerpt/summary (full bodies are
    not fetched).
    """

    __tablename__ = "mi__articles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    source: Mapped[ArticleSource] = mapped_column(
        SAEnum(ArticleSource, name="article_source_enum", values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # Normalized-title hash for collapsing the same story syndicated across sources.
    title_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
