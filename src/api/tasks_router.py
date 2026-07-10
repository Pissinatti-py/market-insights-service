"""
On-demand task triggering — the HTTP face of ``make trigger``.

Enqueues a Celery task by short name and returns immediately with the Celery
task id; the run itself lands in ``mi__task_runs`` (see ``/status``). Only the
whitelisted tasks below can be triggered — the task name is never taken from
the request verbatim.
"""

from fastapi import APIRouter, HTTPException

from src.core.celery.celery_app import celery_app

router = APIRouter(prefix="/tasks", tags=["Tasks"])

#: short name → full Celery task name. Keep in sync with the beat schedule.
TRIGGERABLE_TASKS = {
    "collect_trending": "src.tasks.github_tasks.collect_trending",
    "collect_releases": "src.tasks.packages_tasks.collect_releases",
    "collect_articles": "src.tasks.articles_tasks.collect_articles",
    "curate_uncurated": "src.tasks.curation_tasks.curate_uncurated",
    "recurate_all": "src.tasks.curation_tasks.recurate_all",
}


@router.post("/{task_name}/trigger", status_code=202)
async def trigger_task(task_name: str) -> dict:
    """
    Enqueue one of the periodic tasks right now instead of waiting for beat.

    :param task_name: Short task name (see ``GET /api/tasks``).
    :return: ``{"task": full_name, "task_id": celery_id, "state": "queued"}``.
    :rtype: dict
    """
    full_name = TRIGGERABLE_TASKS.get(task_name)
    if full_name is None:
        raise HTTPException(status_code=404, detail=f"unknown task; one of: {', '.join(TRIGGERABLE_TASKS)}")
    result = celery_app.send_task(full_name)
    return {"task": full_name, "task_id": result.id, "state": "queued"}


@router.get("")
async def list_triggerable_tasks() -> dict:
    """The tasks that can be triggered, short name → full Celery name."""
    return TRIGGERABLE_TASKS
