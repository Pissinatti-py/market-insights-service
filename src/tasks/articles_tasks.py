"""
Article collector Celery tasks.

``collect_articles`` pulls Dev.to + Hacker News (by profile tags) + Medium tag
feeds (RSS) and upserts deduped rows.
"""

from __future__ import annotations

from src.core.celery.celery_app import celery_app
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.db.managers.preference_manager import get_or_create_sync, profile_is_empty
from src.db.session import SyncSession
from src.db.upsert import bulk_upsert_dedup
from src.models.article import Article, ArticleSource
from src.services.collectors import articles
from src.services.logger_service import logger


def _medium_feed(tag: str) -> str:
    """Medium publishes a per-tag RSS feed at this URL shape."""
    return f"https://medium.com/feed/tag/{tag.strip().replace(' ', '-').lower()}"


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
            rows.extend(articles.fetch_hackernews(tags))
        except CollectorTerminal as exc:
            logger.warning(f"collect_articles: hackernews skipped: {exc}")

        for tag in tags:
            # RSS parsing never raises (feedparser is lenient); a dead feed is [].
            rows.extend(articles.fetch_rss(_medium_feed(tag), source=ArticleSource.MEDIUM))

        inserted = bulk_upsert_dedup(session, Article, [r.model_dump() for r in rows])

    logger.info(f"collect_articles: fetched {len(rows)}, inserted {len(inserted)}")
    return {"fetched": len(rows), "inserted": len(inserted)}
