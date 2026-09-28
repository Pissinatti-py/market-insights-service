"""
AI curation Celery task.

Scans collected items that have no curation row yet, asks the LLM to summarise /
tag each against the profile, scores it with the local ranker (``src/services/ranker.py``
— nearest reviewed neighbours in embedding space), and writes a ``pending`` curation row.
Every collector chains this task, so runs overlap: a Redis single-flight lock
skips a run while another is draining, and the ``(item_type, item_id)`` unique
constraint is tolerated per item (skip, not crash) so a lost race never kills
the batch or duplicates LLM work for long.
Each run curates one batch and queues the next while backlog remains, so a run
skipped on the lock rarely loses anything: the holder re-checks the backlog after
its batch. Only a trigger landing in the instant between that check and the lock
release is missed, and those items wait for the next chained/scheduled run.
Items whose output never validates get a ``failed`` dead-letter row instead of
being re-selected forever; ``recurate_all`` is their retry path.
Past approve/reject verdicts are loaded once per run as the ranker's examples;
``rerank_all`` re-scores every item against the latest verdicts with no LLM call.
"""

from __future__ import annotations

import json

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.core.celery.celery_app import celery_app
from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.db.managers.curation_manager import reviewed_examples_sync
from src.db.managers.preference_manager import get_or_create_sync
from src.db.session import SyncSession
from src.models.article import Article
from src.models.curation import Curation, CurationItemType, CurationStatus
from src.models.library import LibraryRelease
from src.models.repository import Repository
from src.services import ranker
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


def _load_ranking(session, profile: dict) -> tuple[list[ranker.Example], list[float]]:
    """The ranker's reference set, loaded **once per run**: every reviewed example + the profile vector."""
    return reviewed_examples_sync(session), ranker.embed([json.dumps(profile, ensure_ascii=False)])[0]


def _embed_text(item_type: CurationItemType, item) -> str:
    """The text the ranker embeds for an item — the head of the same rendering the LLM reads."""
    _, render = _RENDERERS[item_type]
    return render(item)[: ranker.EMBED_CHARS]


def _curate_one(item_type: CurationItemType, item, profile: dict) -> tuple:
    """Render → enrich → LLM for one item. Raises ``CollectorTerminal`` on bad output."""
    _, render = _RENDERERS[item_type]
    context = build_context(item_type, item)
    return curation_agent.curate(render(item), profile, context=context)


_LOCK_KEY = "mi:lock:curate_uncurated"
_LOCK_TTL_SECONDS = 3 * 60 * 60  # comfortably above a worst-case batch (200 items × ~35 s LLM + enrichment)
_RERANK_LOCK_KEY = "mi:lock:rerank_all"
_RERANK_LOCK_TTL_SECONDS = 60 * 60  # well above a run; an expired lock only costs a duplicate, harmless run


def _acquire_lock(key: str, ttl_seconds: int):
    """
    Redis single-flight lock so overlapping runs of a task don't do the same work twice.

    Returns the held lock, ``False`` when another run holds it, or ``None`` when
    Redis is unreachable (fail open — both tasks stay harmless when they overlap:
    ``_store`` tolerates a lost insert race, ``rerank_all`` writes idempotent scores).
    """
    try:
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        lock = client.lock(key, timeout=ttl_seconds)
        return lock if lock.acquire(blocking=False) else False
    except RedisError as exc:
        logger.warning(f"{key}: redis lock unavailable, running unlocked: {exc}")
        return None


def _release_lock(lock) -> None:
    if lock:
        try:
            lock.release()
        except RedisError:
            pass  # lock expired mid-run; both tasks already tolerate an overlapping run


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


def _curate_page(
    session, page, vectors: list[list[float]], profile: dict, ranking: tuple[list[ranker.Example], list[float]]
) -> tuple[int, int]:
    """Curate one page of items, already embedded as ``vectors``; returns ``(curated, failed)``."""
    examples, profile_vec = ranking
    curated = 0
    failed = 0
    for (item_type, item), vec in zip(page, vectors, strict=True):
        try:
            result, raw = _curate_one(item_type, item, profile)
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
            importance_score=ranker.score(vec, examples, profile_vec, item.id),
            embedding=vec,
            status=CurationStatus.PENDING,
            model=settings.OLLAMA_MODEL,
            raw_llm_output=raw,
        )
        if _store(session, row):
            curated += 1
    return curated, failed


@celery_app.task(
    bind=True,
    name="src.tasks.curation_tasks.curate_uncurated",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def curate_uncurated(self) -> dict:
    """
    Curate one batch of ``CURATION_BATCH_SIZE`` uncurated items, newest first, then
    queue the next batch as a **new task** while any backlog is left. Each task stays
    short — under the lock TTL and the broker's ack-late visibility timeout, and it
    re-reads profile + review verdicts — instead of one run looping for hours. Types are
    interleaved round-robin so articles cannot starve repos/releases. Single-flight:
    if another run holds the lock this one skips; the holder re-checks the backlog
    after its batch, so items that landed meanwhile are queued, not stranded.

    :return: ``{"curated": int, "failed": int}`` (failed = dead-lettered invalid
        output), or ``{"skipped": "already running"}``.
    :rtype: dict
    """
    lock = _acquire_lock(_LOCK_KEY, _LOCK_TTL_SECONDS)
    if lock is False:
        logger.info("curate_uncurated: another run holds the lock, skipping")
        return {"skipped": "already running"}

    try:
        with SyncSession() as session:
            batch = _next_batch(session, settings.CURATION_BATCH_SIZE)
            if not batch:
                return {"curated": 0, "failed": 0}
            profile = _load_profile(session)
            ranking = _load_ranking(session, profile)
            # Embed the whole batch before any LLM call: an embed failure then costs no
            # LLM work, and Ollama never swaps chat/embed models between items.
            vectors = ranker.embed([_embed_text(item_type, item) for item_type, item in batch])
            curated, failed = _curate_page(session, batch, vectors, profile, ranking)
            # No progress means every insert lost (a concurrent unlocked run owns them,
            # or they can never store) — stop rather than requeue forever.
            more = bool(curated + failed) and bool(_next_batch(session, 1))
    finally:
        _release_lock(lock)

    logger.info(f"curate_uncurated: curated {curated}, failed {failed}, more backlog: {more}")
    if more:
        celery_app.send_task("src.tasks.curation_tasks.curate_uncurated")
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
    Re-run the LLM description over **every** existing curation row.

    Overwrites the AI fields (summary/tags/model/raw output) in place — a full
    refresh after prompt/enrichment changes, and the retry path for
    ``failed`` rows (which go back to ``pending``). Human review decisions are
    preserved: ``approved``/``rejected`` rows keep their status. Rows whose
    source item is gone are counted as ``missing``. The score is the ranker's,
    not the LLM's — ``rerank_all`` refreshes it (and embeds revived ``failed`` rows).

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
                result, raw = _curate_one(cur.item_type, item, profile)
            except CollectorTerminal as exc:
                logger.warning(f"recurate: invalid output for {cur.item_type.value}:{cur.item_id}: {exc}")
                skipped += 1
                continue

            cur.summary = result.summary
            cur.tags = result.tags
            if cur.status == CurationStatus.FAILED:
                cur.status = CurationStatus.PENDING  # approved/rejected keep the human decision
            cur.model = settings.OLLAMA_MODEL
            cur.raw_llm_output = raw
            session.commit()
            recurated += 1

    logger.info(f"recurate_all: recurated {recurated}, skipped {skipped}, missing {missing}")
    return {"recurated": recurated, "skipped": skipped, "missing": missing}


#: Rows rescored per transaction — bounds memory to a chunk of ~4 KB vectors, and how
#: long a review click can wait on the chunk's row locks.
_RERANK_CHUNK = 500


@celery_app.task(
    bind=True,
    name="src.tasks.curation_tasks.rerank_all",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def rerank_all(self) -> dict:
    """
    Re-score **every** curated item with the ranker — no LLM call.

    First backfills missing embeddings (rows curated before the ranker existed,
    ``failed`` rows revived by ``recurate_all``, or all of them after the embedding
    model changed and the column was cleared). Then recomputes ``importance_score``
    for every embedded row against the current review verdicts — each item never
    against its own. Cheap enough to run on a schedule, so new reviews reorder the
    feed without re-curating anything.

    Single-flight, and it commits per chunk of ``_RERANK_CHUNK`` rows in primary-key
    order: scores are computed before the chunk's UPDATE, so a review click only ever
    waits on one chunk's write, never on the whole scoring pass. Only scores that
    changed are written.

    :return: ``{"embedded": int, "reranked": int, "changed": int}`` (reranked = rows
        scored, changed = rows whose stored score moved), or ``{"skipped": "already running"}``.
    :rtype: dict
    """
    lock = _acquire_lock(_RERANK_LOCK_KEY, _RERANK_LOCK_TTL_SECONDS)
    if lock is False:
        logger.info("rerank_all: another run holds the lock, skipping")
        return {"skipped": "already running"}

    embedded = 0
    reranked = 0
    changed = 0
    try:
        with SyncSession() as session:
            missing = session.execute(
                select(Curation.id, Curation.item_type, Curation.item_id).where(
                    Curation.embedding.is_(None), Curation.status != CurationStatus.FAILED
                )
            ).all()
            for start in range(0, len(missing), ranker.EMBED_BATCH):
                found = []
                for row_id, item_type, item_id in missing[start : start + ranker.EMBED_BATCH]:
                    model, _ = _RENDERERS[item_type]
                    item = session.get(model, item_id)
                    if item is not None:  # source item gone: nothing to embed, row keeps its old score
                        found.append((row_id, _embed_text(item_type, item)))
                if found:
                    vectors = ranker.embed([text for _, text in found])
                    session.execute(
                        update(Curation),
                        [{"id": row_id, "embedding": vec} for (row_id, _), vec in zip(found, vectors, strict=True)],
                    )
                    embedded += len(found)
                session.commit()
                session.expunge_all()  # drop the loaded source items between chunks

            examples, profile_vec = _load_ranking(session, _load_profile(session))
            query = (
                select(Curation.id, Curation.item_id, Curation.embedding, Curation.importance_score)
                .where(Curation.embedding.is_not(None))
                .order_by(Curation.id)
                .limit(_RERANK_CHUNK)
            )
            last_id = None
            while chunk := session.execute(query if last_id is None else query.where(Curation.id > last_id)).all():
                last_id = chunk[-1].id
                scores = [
                    (row_id, old, round(ranker.score(vec, examples, profile_vec, item_id), 3))
                    for row_id, item_id, vec, old in chunk
                ]
                # Compare at the column's precision (Numeric(4, 3)) so unchanged rows are not rewritten.
                moved = [
                    {"id": row_id, "importance_score": new}
                    for row_id, old, new in scores
                    if old is None or float(old) != new
                ]
                if moved:
                    session.execute(update(Curation), moved)
                session.commit()
                reranked += len(chunk)
                changed += len(moved)
    finally:
        _release_lock(lock)

    logger.info(f"rerank_all: embedded {embedded}, reranked {reranked}, changed {changed}")
    return {"embedded": embedded, "reranked": reranked, "changed": changed}
