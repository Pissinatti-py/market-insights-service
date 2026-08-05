import uuid
from datetime import datetime

from pydantic import BaseModel

from src.models.curation import CurationItemType, CurationStatus


class FeedItem(BaseModel):
    """One curated signal in the unified feed — curation + a render of its item."""

    curation_id: uuid.UUID
    item_type: CurationItemType
    item_id: uuid.UUID
    title: str
    url: str | None
    summary: str | None
    tags: list[str]
    importance_score: float | None  # JSON number, not Decimal's string rendering
    status: CurationStatus
    reviewed_at: datetime | None = None  # when a human approved/rejected it; null while pending
    created_at: datetime
