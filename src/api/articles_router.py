import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.managers.article_manager import ArticleRepository
from src.db.session import get_db_async_session
from src.models.article import Article
from src.schemas.article_schema import ArticleRead
from src.schemas.common import Page

router = APIRouter(prefix="/articles", tags=["Articles"])
_repo = ArticleRepository()


@router.get("", response_model=Page[ArticleRead])
async def list_articles(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_async_session),
) -> Page[ArticleRead]:
    """List collected articles, newest first."""
    result = await _repo.paginate(
        db, page=page, per_page=per_page, order_by="-published_at", expressions=[Article.deleted_at.is_(None)]
    )
    return Page(
        total=result.total,
        items=[ArticleRead.model_validate(a) for a in result.items],
        page=result.page,
        per_page=result.per_page,
        num_pages=result.num_pages,
    )


# NOTE: declared before /{article_id} so "search" is not captured as an id.
@router.get("/search", response_model=list[ArticleRead])
async def search_articles(
    q: str = Query(..., min_length=1, description="Match against title, author, or content"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_async_session),
) -> list[ArticleRead]:
    """Search articles by topic/author/title (case-insensitive substring)."""
    pattern = f"%{q}%"
    rows = await _repo.get_multi(
        db,
        limit=limit,
        order_by="-published_at",
        expressions=[
            Article.deleted_at.is_(None),
            or_(
                Article.title.ilike(pattern),
                Article.author.ilike(pattern),
                Article.content.ilike(pattern),
            ),
        ],
    )
    return [ArticleRead.model_validate(a) for a in rows]


@router.get("/{article_id}", response_model=ArticleRead)
async def get_article(article_id: uuid.UUID, db: AsyncSession = Depends(get_db_async_session)) -> ArticleRead:
    """Get one article by id."""
    article = await _repo.get(db, article_id)
    if article is None or article.deleted_at is not None:
        raise HTTPException(status_code=404, detail="article not found")
    return ArticleRead.model_validate(article)
