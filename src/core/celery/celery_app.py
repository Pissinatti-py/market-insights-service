"""
Celery application for market-insights-service.

Single ``Celery`` instance imported by the workers, the beat, and the FastAPI
process (for manual triggers). All collectors + curation run on the default
``celery`` queue — the workloads are I/O-bound HTTP calls, no per-source queue
isolation is needed yet.

Mirrors pissync-core's config: JSON-only bus, acks-late, prefetch=1, and a
redbeat (Redis-backed) scheduler so the periodic schedule survives restarts.
"""

from __future__ import annotations

from celery import Celery

import src.core.celery.task_runs  # noqa: F401 — connects the task_postrun recorder
import src.core.events  # noqa: F401 — Curation after_insert → Redis event for the review UI
from src.core.celery.schedules import beat_schedule
from src.core.conf import settings

celery_app = Celery(
    "market_insights",
    include=[
        "src.tasks.github_tasks",
        "src.tasks.packages_tasks",
        "src.tasks.articles_tasks",
        "src.tasks.curation_tasks",
    ],
)

celery_app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    result_backend=settings.CELERY_RESULT_BACKEND,
    # JSON-only message bus — a malicious payload cannot escalate to RCE via pickle.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Collectors block on multi-second external HTTP calls; ack only after the
    # task finishes, requeue if the worker dies, never prefetch while one runs.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    # redbeat — Redis-backed Beat scheduler; survives container restarts.
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=settings.CELERY_BROKER_URL,
    beat_schedule=beat_schedule,
)
