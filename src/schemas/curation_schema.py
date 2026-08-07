import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models.curation import CurationItemType, CurationStatus


class CurationCreate(BaseModel):
    """
    The validated shape of an LLM curation result.

    The curation agent coerces the model's raw JSON into this schema before
    persisting — invalid output (missing summary, score out of range) is rejected
    as a terminal failure, exactly like pissync validates LLM output through the
    create-schema.
    """

    summary: str = Field(min_length=1)
    tags: list[str] = []
    # float, not Decimal: pydantic renders Decimal as anyOf[number, string+lookahead
    # regex] in JSON schema, and that regex segfaults Ollama's grammar compiler.
    importance_score: float = Field(ge=0, le=1)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        """Strip blanks and dedupe while preserving order."""
        seen: list[str] = []
        for tag in v:
            t = str(tag).strip().lower()
            if t and t not in seen:
                seen.append(t)
        return seen


class CurationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_type: CurationItemType
    item_id: uuid.UUID
    summary: str | None
    tags: list[str]
    # float, not Decimal: the DB column is Numeric(4,3) but Decimal serializes to a
    # JSON string ("1.000"); clients want a number.
    importance_score: float | None
    status: CurationStatus
    reviewed_by: str | None
    reviewed_at: datetime | None = None
    model: str | None
    created_at: datetime


class CurationReview(BaseModel):
    """Manual review decision (``PUT /api/curation/{id}/review``)."""

    status: CurationStatus
    reviewed_by: str | None = None


class CurationBulkReview(CurationReview):
    """One review decision applied to many rows (``PUT /api/curation/review``)."""

    ids: list[uuid.UUID] = Field(min_length=1)


class CurationStats(BaseModel):
    """Aggregate counts for ``GET /api/curation/stats``."""

    total: int
    pending: int
    approved: int
    rejected: int
    failed: int


class CalibrationBand(BaseModel):
    """How one score band fared under human review."""

    band: str
    approved: int
    rejected: int
    #: Share of reviewed items in this band that were kept. ``None`` when the band is empty.
    approval_rate: float | None


class CurationCalibration(BaseModel):
    """
    Whether the LLM's scores agree with the human verdict (``GET /api/curation/calibration``).

    ``separation`` is the headline number: how much higher the model scored what was
    approved than what was rejected. It should widen as review feedback accumulates
    into the prompt; a value near zero means the score is not discriminating at all.
    """

    reviewed: int
    approved_mean_score: float | None
    rejected_mean_score: float | None
    separation: float | None
    bands: list[CalibrationBand]
