"""
Unified feed — the product face of the pipeline.

One call returns curated signals across all three item tables (repositories,
library releases, articles), joined with their curation row, ranked by the
LLM's importance score. Defaults to approved items only.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.db.managers.curation_manager import CurationRepository
from src.db.session import get_db_async_session
from src.models.article import Article
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.models.library import Library, LibraryRelease
from src.models.repository import Repository
from src.schemas.common import Page
from src.schemas.feed_schema import FeedItem

router = APIRouter(prefix="/feed", tags=["Feed"])
_curation = CurationRepository()


async def _render_items(
    db: AsyncSession, curations: list[Curation]
) -> dict[tuple[CurationItemType, uuid.UUID], tuple[str, str | None]]:
    """Batch-fetch the items behind the curations → ``{(type, id): (title, url)}``."""
    ids = {t: [c.item_id for c in curations if c.item_type == t] for t in CurationItemType}
    rendered: dict[tuple[CurationItemType, uuid.UUID], tuple[str, str | None]] = {}

    if ids[CurationItemType.REPOSITORY]:
        rows = await db.execute(select(Repository).where(Repository.id.in_(ids[CurationItemType.REPOSITORY])))
        for r in rows.scalars():
            rendered[(CurationItemType.REPOSITORY, r.id)] = (f"{r.owner}/{r.name}", r.url)

    if ids[CurationItemType.LIBRARY_RELEASE]:
        rows = await db.execute(
            select(LibraryRelease, Library.name)
            .join(Library, LibraryRelease.library_id == Library.id)
            .where(LibraryRelease.id.in_(ids[CurationItemType.LIBRARY_RELEASE]))
        )
        for release, lib_name in rows:
            rendered[(CurationItemType.LIBRARY_RELEASE, release.id)] = (f"{lib_name} {release.new_version}", None)

    if ids[CurationItemType.ARTICLE]:
        rows = await db.execute(select(Article).where(Article.id.in_(ids[CurationItemType.ARTICLE])))
        for a in rows.scalars():
            rendered[(CurationItemType.ARTICLE, a.id)] = (a.title, a.url)

    return rendered


@router.get("", response_model=Page[FeedItem])
async def feed(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: CurationStatus = Query(CurationStatus.APPROVED),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[FeedItem]:
    """Curated signals across all sources, most important first."""
    result = await _curation.paginate(
        db, page=page, per_page=per_page, filters={"status": status}, order_by="-importance_score"
    )
    rendered = await _render_items(db, result.items)

    items = []
    for cur in result.items:
        hit = rendered.get((cur.item_type, cur.item_id))
        if hit is None:
            # Orphan curation (item hard-deleted) — nothing to show.
            continue
        title, url = hit
        items.append(
            FeedItem(
                curation_id=cur.id,
                item_type=cur.item_type,
                item_id=cur.item_id,
                title=title,
                url=url,
                summary=cur.summary,
                tags=cur.tags,
                importance_score=cur.importance_score,
                status=cur.status,
                created_at=cur.created_at,
            )
        )
    return Page.from_result(result, items=items)
