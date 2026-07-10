"""
Article collector Celery tasks.

``collect_articles`` pulls Dev.to + Hacker News (by profile tags) + Medium tag
feeds (RSS), drops stale rows, collapses near-duplicates by title fingerprint,
and upserts deduped rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.core.celery.celery_app import celery_app
from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.db.managers.preference_manager import get_or_create_sync, profile_is_empty
from src.db.session import SyncSession
from src.db.upsert import bulk_upsert_dedup
from src.models.article import Article, ArticleSource
from src.schemas.article_schema import ArticleCreate
from src.services.collectors import articles
from src.services.logger_service import logger


def _medium_feed(tag: str) -> str:
    """Medium publishes a per-tag RSS feed at this URL shape."""
    return f"https://medium.com/feed/tag/{tag.strip().replace(' ', '-').lower()}"


def _drop_stale(rows: list[ArticleCreate], cutoff: datetime) -> list[ArticleCreate]:
    """Drop rows published before ``cutoff`` (undated rows pass — the LLM date-caps them)."""
    return [r for r in rows if r.published_at is None or r.published_at >= cutoff]


def _collapse_near_dups(session, rows: list[ArticleCreate]) -> list[ArticleCreate]:
    """
    Collapse the same story syndicated across sources, by title fingerprint.

    In-batch: keep the highest-engagement row per fingerprint. Cross-run: drop
    rows whose fingerprint is already stored. Duplicates never hit the DB, so no
    curation-side grouping and no wasted LLM calls.
    """
    best: dict[str, ArticleCreate] = {}
    passthrough: list[ArticleCreate] = []
    for r in rows:
        if not r.title_fingerprint:
            passthrough.append(r)
            continue
        current = best.get(r.title_fingerprint)
        if current is None or (r.likes + r.comments) > (current.likes + current.comments):
            best[r.title_fingerprint] = r

    if best:
        existing = set(
            session.execute(
                select(Article.title_fingerprint).where(Article.title_fingerprint.in_(best.keys()))
            ).scalars()
        )
        best = {fp: r for fp, r in best.items() if fp not in existing}

    return passthrough + list(best.values())


@celery_app.task(
    bind=True,
    name="src.tasks.articles_tasks.collect_articles",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def collect_articles(self) -> dict:
    """
    Collect articles from Dev.to + Hacker News + Medium for the profile's topics.

    :return: ``{"fetched": int, "inserted": int}``.
    :rtype: dict
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.ARTICLE_MAX_AGE_DAYS)

    with SyncSession() as session:
        pref = get_or_create_sync(session)
        if pref.enabled_sources.get("articles", True) is False:
            logger.info("collect_articles: articles source disabled — skipping")
            return {"fetched": 0, "inserted": 0}
        if profile_is_empty(pref):
            logger.warning("collect_articles: profile is empty — untargeted Dev.to/HN fetch, Medium skipped")

        tags = list(dict.fromkeys([*(pref.keywords or []), *(pref.areas or [])]))

        rows = []
        try:
            rows.extend(articles.fetch_devto(tags))
        except CollectorTerminal as exc:
            logger.warning(f"collect_articles: devto skipped: {exc}")

        try:
            rows.extend(articles.fetch_hackernews(tags, since=cutoff))
        except CollectorTerminal as exc:
            logger.warning(f"collect_articles: hackernews skipped: {exc}")

        for tag in tags:
            # RSS parsing never raises (feedparser is lenient); a dead feed is [].
            rows.extend(articles.fetch_rss(_medium_feed(tag), source=ArticleSource.MEDIUM))

        fetched = len(rows)
        rows = _collapse_near_dups(session, _drop_stale(rows, cutoff))

        inserted = bulk_upsert_dedup(session, Article, [r.model_dump() for r in rows])

    if inserted:
        # Chain curation over the fresh rows — idempotent, so a double-fire is a no-op.
        celery_app.send_task("src.tasks.curation_tasks.curate_uncurated")

    logger.info(f"collect_articles: fetched {fetched}, inserted {len(inserted)}")
    return {"fetched": fetched, "inserted": len(inserted)}
