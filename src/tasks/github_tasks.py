"""
GitHub collector Celery task.

Runs on the sync engine: read the profile, query GitHub, upsert repos by
``dedup_key`` (idempotent), and append a metrics snapshot per inserted repo.
Retriable failures autoretry with exponential backoff; terminal ones fail the
run without retrying.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.celery.celery_app import celery_app
from src.core.exceptions import CollectorRetriable
from src.db.managers.preference_manager import get_or_create_sync, profile_is_empty
from src.db.session import SyncSession
from src.db.upsert import bulk_upsert_dedup
from src.models.repository import Repository, RepositorySnapshot
from src.services.collectors import github
from src.services.logger_service import logger

# Repos pushed within this window are considered "active" enough to collect.
_RECENCY_DAYS = 30


@celery_app.task(
    bind=True,
    name="src.tasks.github_tasks.collect_trending",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def collect_trending(self) -> dict:
    """
    Collect trending repositories matching the user's profile.

    :return: ``{"fetched": int, "inserted": int}`` for observability.
    :rtype: dict
    """
    now = datetime.now(timezone.utc)
    pushed_since = (now - timedelta(days=_RECENCY_DAYS)).date().isoformat()

    with SyncSession() as session:
        pref = get_or_create_sync(session)
        if pref.enabled_sources.get("github", True) is False:
            logger.info("collect_trending: github source disabled — skipping")
            return {"fetched": 0, "inserted": 0}
        if profile_is_empty(pref):
            logger.warning("collect_trending: profile is empty — collecting untargeted/global trending")

        languages = list(pref.stacks or [])
        keywords = list(pref.keywords or [])

        rows = github.fetch_trending(languages, keywords, pushed_since=pushed_since)
        inserted = bulk_upsert_dedup(session, Repository, [r.model_dump() for r in rows])

        # Append a metrics snapshot for each freshly-inserted repo (change history).
        if inserted:
            session.add_all(
                [
                    RepositorySnapshot(
                        repository_id=repo.id,
                        stars=repo.stars,
                        stars_per_week=repo.stars_per_week,
                        relevance_score=repo.relevance_score,
                        captured_at=now,
                    )
                    for repo in inserted
                ]
            )
            session.commit()

    if inserted:
        # Chain curation over the fresh rows — idempotent, so a double-fire is a no-op.
        celery_app.send_task("src.tasks.curation_tasks.curate_uncurated")

    logger.info(f"collect_trending: fetched {len(rows)}, inserted {len(inserted)}")
    return {"fetched": len(rows), "inserted": len(inserted)}
