"""
Unified feed — the product face of the pipeline.

One call returns curated signals across all three item tables (repositories,
library releases, articles), joined with their curation row, ranked by the
ranker's importance score. Defaults to pending + approved — rejected/failed items
never surface unless explicitly requested via ``?status=``.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
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


def _to_feed_items(curations: list[Curation], rendered: dict) -> list[FeedItem]:
    """Join curation rows with their rendered titles/urls, dropping orphans."""
    items = []
    for cur in curations:
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
                reviewed_at=cur.reviewed_at,
                created_at=cur.created_at,
            )
        )
    return items


@router.get("", response_model=Page[FeedItem])
async def feed(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: CurationStatus | None = Query(
        None, description="Default: pending + approved (review is optional grooming)"
    ),
    item_type: CurationItemType | None = Query(None),
    min_score: float | None = Query(None, ge=0, le=1),
    tag: str | None = Query(None, description="Exact tag match (lowercase)"),
    since: datetime | None = Query(None, description="Only items curated on/after this moment"),
    # Literal, not a free string: a bad value is a 422 here rather than a ValueError
    # deeper down in BaseManager._order_columns.
    order_by: Literal["-importance_score", "-reviewed_at", "-created_at"] = Query("-importance_score"),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[FeedItem]:
    """Curated signals across all sources, most important first."""
    status_filter = status if status is not None else [CurationStatus.PENDING, CurationStatus.APPROVED]
    filters: dict = {"status": status_filter}
    if item_type is not None:
        filters["item_type"] = item_type

    expressions = []
    if min_score is not None:
        expressions.append(Curation.importance_score >= min_score)
    if tag is not None:
        expressions.append(Curation.tags.contains([tag.strip().lower()]))
    if since is not None:
        expressions.append(Curation.created_at >= since)

    result = await _curation.paginate(
        db,
        page=page,
        per_page=per_page,
        filters=filters,
        expressions=expressions,
        order_by=f"{order_by},-created_at",  # ranker scores tie often (1.000) — newest wins
    )
    items = _to_feed_items(result.items, await _render_items(db, result.items))
    return Page.from_result(result, items=items)


#: Markdown digest sections, in reading order.
_SECTIONS = (
    (CurationItemType.REPOSITORY, "Repositories"),
    (CurationItemType.LIBRARY_RELEASE, "Releases"),
    (CurationItemType.ARTICLE, "Articles"),
)


def _render_markdown(items: list[FeedItem], days: int) -> str:
    """Render the digest as Markdown, grouped by item type, ready to paste anywhere."""
    lines = [f"# Insights — last {days} days", ""]
    for item_type, heading in _SECTIONS:
        group = [i for i in items if i.item_type == item_type]
        if not group:
            continue
        lines += [f"## {heading}", ""]
        for item in group:
            title = f"[{item.title}]({item.url})" if item.url else item.title
            meta = [] if item.importance_score is None else [f"score {item.importance_score:.2f}"]
            if item.tags:
                meta.append(", ".join(item.tags))
            suffix = f" _({' · '.join(meta)})_" if meta else ""
            lines.append(f"- **{title}** — {item.summary or 'no summary yet'}{suffix}")
        lines.append("")
    if len(lines) == 2:
        lines.append("_Nothing curated in this window._")
    return "\n".join(lines).rstrip() + "\n"


# NOTE: no /{param} route exists on this router today, but keep /digest above any
# future one so it isn't captured as a path parameter.
@router.get("/digest", response_model=None)
async def digest(
    days: int = Query(7, ge=1, le=90, description="Window ending now, measured on curation time"),
    min_score: float | None = Query(None, ge=0, le=1),
    limit: int = Query(20, ge=1, le=100),
    format: Literal["json", "markdown"] = Query("json"),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[FeedItem] | PlainTextResponse:
    """
    The period digest: what was worth knowing in the last ``days``, best first.

    Same rows and same shape as ``/api/feed`` (pending + approved, ranked by
    importance) narrowed to a time window — ``format=markdown`` renders them as a
    ready-to-publish document instead of JSON.
    """
    expressions = [Curation.created_at >= datetime.now(timezone.utc) - timedelta(days=days)]
    if min_score is not None:
        expressions.append(Curation.importance_score >= min_score)

    result = await _curation.paginate(
        db,
        page=1,
        per_page=limit,
        filters={"status": [CurationStatus.PENDING, CurationStatus.APPROVED]},
        expressions=expressions,
        order_by="-importance_score,-created_at",
    )
    items = _to_feed_items(result.items, await _render_items(db, result.items))

    if format == "markdown":
        return PlainTextResponse(_render_markdown(items, days), media_type="text/markdown; charset=utf-8")
    return Page.from_result(result, items=items)
