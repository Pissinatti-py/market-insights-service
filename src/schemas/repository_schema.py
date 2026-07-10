import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RepositoryCreate(BaseModel):
    """Validated row produced by the github collector before upsert."""

    dedup_key: str
    url: str
    owner: str
    name: str
    description: str | None = None
    stars: int = 0
    forks: int = 0
    stars_per_week: float = 0.0
    relevance_score: float = 0.0
    languages: list[str] = []
    topics: list[str] = []
    repo_created_at: datetime | None = None
    collected_at: datetime


class RepositoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dedup_key: str
    url: str
    owner: str
    name: str
    description: str | None
    stars: int
    forks: int
    stars_per_week: float
    relevance_score: float
    languages: list[str]
    topics: list[str]
    repo_created_at: datetime | None
    collected_at: datetime
    created_at: datetime


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stars: int
    stars_per_week: float
    relevance_score: float
    captured_at: datetime


class RepositoryMomentum(BaseModel):
    """Star-growth over the snapshot history (``GET /api/repositories/{id}/momentum``)."""

    repository_id: uuid.UUID
    window_days: int
    snapshots: list[SnapshotRead]
    #: newest snapshot minus oldest within the window; 0 when fewer than 2 snapshots.
    stars_delta: int
    velocity_delta: float


class RepositorySearch(BaseModel):
    """Manual search filters for ``POST /api/repositories/search``."""

    languages: list[str] | None = None
    keywords: list[str] | None = None
    min_stars: int = 0
    max_results: int | None = None
