import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.article import ArticleSource


class ArticleCreate(BaseModel):
    """Validated row produced by an article collector before upsert."""

    dedup_key: str
    source: ArticleSource
    title: str
    title_fingerprint: str | None = None
    author: str | None = None
    url: str
    content: str | None = None
    likes: int = 0
    comments: int = 0
    published_at: datetime | None = None


class ArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: ArticleSource
    title: str
    author: str | None
    url: str
    content: str | None
    likes: int
    comments: int
    published_at: datetime | None
    created_at: datetime
