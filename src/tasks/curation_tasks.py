"""
AI curation Celery task.

Scans collected items that have no curation row yet, asks the LLM to summarise /
tag / score each against the profile, and writes a ``pending`` curation row.
Idempotent two ways: only uncurated items are selected, and the
``(item_type, item_id)`` unique constraint makes a concurrent re-run a no-op.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

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

# (item_type, model, text-renderer) for each curatable entity. Articles first: they
# are the primary "news" signal and must not be starved by the shared batch budget.
_TARGETS = [
    (CurationItemType.ARTICLE, Article, lambda a: f"Article '{a.title}' by {a.author or 'unknown'}. {a.content or ''}"),
    (
        CurationItemType.REPOSITORY,
        Repository,
        lambda r: f"GitHub repo {r.owner}/{r.name}: {r.description or ''}. Topics: {', '.join(r.topics or [])}. Stars/week: {r.stars_per_week}.",
    ),
    (
        CurationItemType.LIBRARY_RELEASE,
        LibraryRelease,
        lambda x: f"Library '{x.library.name}' release {x.new_version} (was {x.previous_version}). Major: {x.is_major}. Notes: {x.release_notes or ''}.",
    ),
]

# item_type -> (model, renderer), for looking up a source item by an existing curation row.
_RENDERERS = {item_type: (model, render) for item_type, model, render in _TARGETS}


def _uncurated(session, item_type: CurationItemType, model, limit: int) -> list:
    """Return up to ``limit`` rows of ``model`` that have no curation row yet."""
    curated_ids = select(Curation.item_id).where(Curation.item_type == item_type)
    query = select(model).where(model.id.notin_(curated_ids))
    if hasattr(model, "deleted_at"):
        query = query.where(model.deleted_at.is_(None))
    return list(session.execute(query.limit(limit)).scalars().all())


def _load_profile(session) -> dict:
    """The preference row rendered as the profile dict the LLM prompt receives."""
    pref = get_or_create_sync(session)
    return {"stacks": pref.stacks, "areas": pref.areas, "keywords": pref.keywords}


def _curate_one(session, item_type: CurationItemType, item, profile: dict) -> tuple:
    """Render → enrich → LLM for one item. Raises ``CollectorTerminal`` on bad output."""
    _, render = _RENDERERS[item_type]
    context = build_context(item_type, item, session)
    return curation_agent.curate(render(item), profile, context=context)


@celery_app.task(
    bind=True,
    name="src.tasks.curation_tasks.curate_uncurated",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def curate_uncurated(self) -> dict:
    """
    Drain the uncurated backlog across all three sources, capped at
    ``CURATION_BATCH_SIZE`` items per run (safety cap — the rest waits for the
    next run). Articles are consumed first (see ``_TARGETS``).

    :return: ``{"curated": int, "skipped": int}`` (skipped = invalid LLM output).
    :rtype: dict
    """
    batch = settings.CURATION_BATCH_SIZE
    curated = 0
    skipped = 0

    with SyncSession() as session:
        profile = _load_profile(session)

        remaining = batch
        for item_type, model, render in _TARGETS:
            if remaining <= 0:
                break
            for item in _uncurated(session, item_type, model, remaining):
                try:
                    result, raw = _curate_one(session, item_type, item, profile)
                except CollectorTerminal as exc:
                    logger.warning(f"curate: invalid output for {item_type.value}:{item.id}: {exc}")
                    skipped += 1
                    continue

                session.add(
                    Curation(
                        item_type=item_type,
                        item_id=item.id,
                        summary=result.summary,
                        tags=result.tags,
                        importance_score=result.importance_score,
                        status=CurationStatus.PENDING,
                        model=settings.OLLAMA_MODEL,
                        confidence=Decimal(curation_agent.base_confidence()),
                        raw_llm_output=raw,
                    )
                )
                session.commit()
                curated += 1
                remaining -= 1

    logger.info(f"curate_uncurated: curated {curated}, skipped {skipped}")
    return {"curated": curated, "skipped": skipped}


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

    Overwrites the AI fields (summary/tags/score/model/confidence/raw output) in
    place and resets ``status`` to ``pending`` — a full refresh after prompt/
    enrichment changes. Rows whose source item is gone are counted as ``missing``.

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
            cur.status = CurationStatus.PENDING
            cur.model = settings.OLLAMA_MODEL
            cur.confidence = Decimal(curation_agent.base_confidence())
            cur.raw_llm_output = raw
            session.commit()
            recurated += 1

    logger.info(f"recurate_all: recurated {recurated}, skipped {skipped}, missing {missing}")
    return {"recurated": recurated, "skipped": skipped, "missing": missing}
