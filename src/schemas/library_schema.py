import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.library import PackageEcosystem


class LibraryCreate(BaseModel):
    """Add a library to monitoring (``POST /api/libraries``)."""

    ecosystem: PackageEcosystem
    name: str


class LibraryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ecosystem: PackageEcosystem
    name: str
    current_version: str | None
    is_monitored: bool
    created_at: datetime


class LibraryReleaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    previous_version: str | None
    new_version: str
    is_major: bool
    release_notes: str | None
    released_at: datetime | None
    created_at: datetime
