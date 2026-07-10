"""
Task-run bookkeeping via Celery signals.

Connecting ``task_postrun`` here records every task completion (success or
failure) in ``mi__task_runs`` with zero per-task code — new tasks are covered
automatically. Recording must never break the task itself, so any bookkeeping
error is logged and swallowed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from celery.signals import task_postrun

from src.db.session import SyncSession
from src.models.task_run import TaskRun
from src.services.logger_service import logger


@task_postrun.connect
def record_task_run(task=None, retval=None, state=None, **kwargs) -> None:
    """Persist one row per finished task run (``task_postrun`` receiver)."""
    if task is None:
        return
    try:
        with SyncSession() as session:
            session.add(
                TaskRun(
                    task_name=task.name,
                    state=state or "UNKNOWN",
                    result=retval if isinstance(retval, dict) else None,
                    error=None if state == "SUCCESS" else (str(retval) if retval is not None else None),
                    finished_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("task_runs: failed to record run — task result is unaffected")
