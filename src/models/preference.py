import uuid

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin

#: Fixed primary key for the single preferences row. This service curates for one
#: technical profile, so preferences is a singleton — the API reads/writes this row.
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Preference(TimestampMixin, Base):
    """
    The user's technical profile — read by every collector to build queries and
    by the curation agent to score relevance.

    Singleton: exactly one row (``SINGLETON_ID``). ``enabled_sources`` toggles
    individual collectors; the JSONB list fields configure what each collector
    searches for.
    """

    __tablename__ = "mi__preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=lambda: SINGLETON_ID)

    stacks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    areas: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    monitored_libraries: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    enabled_sources: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
