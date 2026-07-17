"""
AI curation Celery task.

Scans collected items that have no curation row yet, asks the LLM to summarise /
tag / score each against the profile, and writes a ``pending`` curation row.
Every collector chains this task, so runs overlap: a Redis single-flight lock
skips a run while another is draining, and the ``(item_type, item_id)`` unique
constraint is tolerated per item (skip, not crash) so a lost race never kills
the batch or duplicates LLM work for long.
Items whose output never validates get a ``failed`` dead-letter row instead of
being re-selected forever; ``recurate_all`` is their retry path.
"""

from __future__ import annotations

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.celery.celery_app import celery_app
from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.db.managers.preference_manager import get_or_create_sync
from src.db.session import SyncSession
from src.models.article import Article
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.models.library import LibraryRelease
from src.models.repository import Repository
from src.services.agents import curation_agent
from src.services.agents.enrichment import build_context
from src.services.logger_service import logger


def _render_article(a: Article) -> str:
    published = a.published_at.date().isoformat() if a.published_at else "unknown"
    return (
        f"Article '{a.title}' by {a.author or 'unknown'}. Published: {published}. "
        f"Engagement: {a.likes} likes, {a.comments} comments. {a.content or ''}"
    )


def _render_repository(r: Repository) -> str:
    created = r.repo_created_at.date().isoformat() if r.repo_created_at else "unknown"
    # stars_per_week is a lifetime average, not current velocity — label it honestly.
    return (
        f"GitHub repo {r.owner}/{r.name}: {r.description or ''}. Topics: {', '.join(r.topics or [])}. "
        f"Created: {created}. Stars: {r.stars} (avg {r.stars_per_week:.1f}/week since creation)."
    )


def _render_release(x: LibraryRelease) -> str:
    released = x.released_at.date().isoformat() if x.released_at else "unknown"
    return (
        f"Library '{x.library.name}' release {x.new_version} (was {x.previous_version}). "
        f"Major: {x.is_major}. Released: {released}. Notes: {x.release_notes or ''}."
    )


# (item_type, model, text-renderer) for each curatable entity.
_TARGETS = [
    (CurationItemType.ARTICLE, Article, _render_article),
    (CurationItemType.REPOSITORY, Repository, _render_repository),
    (CurationItemType.LIBRARY_RELEASE, LibraryRelease, _render_release),
]

# item_type -> (model, renderer), for looking up a source item by an existing curation row.
_RENDERERS = {item_type: (model, render) for item_type, model, render in _TARGETS}


def _uncurated(session, item_type: CurationItemType, model, limit: int) -> list:
    """Return up to ``limit`` uncurated rows of ``model``, newest first."""
    curated_ids = select(Curation.item_id).where(Curation.item_type == item_type)
    query = select(model).where(model.id.notin_(curated_ids))
    if hasattr(model, "deleted_at"):
        query = query.where(model.deleted_at.is_(None))
    return list(session.execute(query.order_by(model.created_at.desc()).limit(limit)).scalars().all())


def _next_batch(session, limit: int) -> list[tuple[CurationItemType, object]]:
    """Round-robin across the three types so no source is starved by the batch cap."""
    queues = [(item_type, _uncurated(session, item_type, model, limit)) for item_type, model, _ in _TARGETS]
    batch: list[tuple[CurationItemType, object]] = []
    i = 0
    while len(batch) < limit and any(q for _, q in queues):
        item_type, q = queues[i % len(queues)]
        if q:
            batch.append((item_type, q.pop(0)))
        i += 1
    return batch


def _load_profile(session) -> dict:
    """The preference row rendered as the profile dict the LLM prompt receives."""
    pref = get_or_create_sync(session)
    return {
        "stacks": pref.stacks,
        "areas": pref.areas,
        "keywords": pref.keywords,
        "monitored_libraries": pref.monitored_libraries,
    }


def _curate_one(session, item_type: CurationItemType, item, profile: dict) -> tuple:
    """Render → enrich → LLM for one item. Raises ``CollectorTerminal`` on bad output."""
    _, render = _RENDERERS[item_type]
    context = build_context(item_type, item, session)
    return curation_agent.curate(render(item), profile, context=context)


_LOCK_KEY = "mi:lock:curate_uncurated"
_LOCK_TTL_SECONDS = 3 * 60 * 60  # comfortably above a worst-case batch (200 items × ~35 s LLM + enrichment)


def _acquire_curation_lock():
    """
    Redis single-flight lock so overlapping runs don't curate the same items twice.

    Returns the held lock, ``False`` when another run holds it, or ``None`` when
    Redis is unreachable (fail open — the per-item ``IntegrityError`` guard in
    ``_store`` still keeps a concurrent run harmless).
    """
    try:
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        lock = client.lock(_LOCK_KEY, timeout=_LOCK_TTL_SECONDS)
        return lock if lock.acquire(blocking=False) else False
    except RedisError as exc:
        logger.warning(f"curate: redis lock unavailable, running unlocked: {exc}")
        return None


def _store(session, row: Curation) -> bool:
    """Insert one curation row; ``False`` when a concurrent run already curated the item."""
    session.add(row)
    try:
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        logger.info(f"curate: {row.item_type.value}:{row.item_id} already curated by a concurrent run, skipping")
        return False


@celery_app.task(
    bind=True,
    name="src.tasks.curation_tasks.curate_uncurated",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def curate_uncurated(self) -> dict:
    """
    Drain the uncurated backlog, capped at ``CURATION_BATCH_SIZE`` items per run
    (safety cap — the rest waits for the next run). Types are interleaved
    round-robin so articles cannot starve repos/releases. Single-flight: if
    another run is already draining, this one skips (the backlog waits for the
    next scheduled/chained fire).

    :return: ``{"curated": int, "failed": int}`` (failed = dead-lettered invalid
        output), or ``{"skipped": "already running"}``.
    :rtype: dict
    """
    lock = _acquire_curation_lock()
    if lock is False:
        logger.info("curate_uncurated: another run holds the lock, skipping")
        return {"skipped": "already running"}

    curated = 0
    failed = 0

    try:
        with SyncSession() as session:
            profile = _load_profile(session)

            for item_type, item in _next_batch(session, settings.CURATION_BATCH_SIZE):
                try:
                    result, raw = _curate_one(session, item_type, item, profile)
                except CollectorTerminal as exc:
                    logger.warning(f"curate: invalid output for {item_type.value}:{item.id}: {exc}")
                    # Dead-letter: the unique constraint keeps the item out of future
                    # selections; recurate_all re-processes it after prompt fixes.
                    dead_letter = Curation(
                        item_type=item_type,
                        item_id=item.id,
                        status=CurationStatus.FAILED,
                        model=settings.OLLAMA_MODEL,
                        raw_llm_output={"error": str(exc)},
                    )
                    if _store(session, dead_letter):
                        failed += 1
                    continue

                row = Curation(
                    item_type=item_type,
                    item_id=item.id,
                    summary=result.summary,
                    tags=result.tags,
                    importance_score=result.importance_score,
                    status=CurationStatus.PENDING,
                    model=settings.OLLAMA_MODEL,
                    raw_llm_output=raw,
                )
                if _store(session, row):
                    curated += 1
    finally:
        if lock:
            try:
                lock.release()
            except RedisError:
                pass  # lock expired mid-run; _store already tolerated any overlap

    logger.info(f"curate_uncurated: curated {curated}, failed {failed}")
    return {"curated": curated, "failed": failed}


@celery_app.task(
    bind=True,
    name="src.tasks.curation_tasks.recurate_all",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def recurate_all(self) -> dict:
    """
    Re-run curation over **every** existing curation row with the current logic.

    Overwrites the AI fields (summary/tags/score/model/raw output) in place — a
    full refresh after prompt/enrichment changes, and the retry path for
    ``failed`` rows (which go back to ``pending``). Human review decisions are
    preserved: ``approved``/``rejected`` rows keep their status. Rows whose
    source item is gone are counted as ``missing``.

    :return: ``{"recurated": int, "skipped": int, "missing": int}``.
    :rtype: dict
    """
    recurated = 0
    skipped = 0
    missing = 0

    with SyncSession() as session:
        profile = _load_profile(session)

        curations = list(session.execute(select(Curation)).scalars().all())
        for cur in curations:
            model, _ = _RENDERERS[cur.item_type]
            item = session.get(model, cur.item_id)
            if item is None:
                missing += 1
                continue

            try:
                result, raw = _curate_one(session, cur.item_type, item, profile)
            except CollectorTerminal as exc:
                logger.warning(f"recurate: invalid output for {cur.item_type.value}:{cur.item_id}: {exc}")
                skipped += 1
                continue

            cur.summary = result.summary
            cur.tags = result.tags
            cur.importance_score = result.importance_score
            if cur.status == CurationStatus.FAILED:
                cur.status = CurationStatus.PENDING  # approved/rejected keep the human decision
            cur.model = settings.OLLAMA_MODEL
            cur.raw_llm_output = raw
            session.commit()
            recurated += 1

    logger.info(f"recurate_all: recurated {recurated}, skipped {skipped}, missing {missing}")
    return {"recurated": recurated, "skipped": skipped, "missing": missing}
