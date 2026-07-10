from fastapi import APIRouter
from sqlalchemy import select, text

from src.core.conf import settings
from src.db.session import engine
from src.models.preference import SINGLETON_ID, Preference
from src.models.task_run import TaskRun

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health() -> dict:
    """
    Liveness probe — the process is up.

    :return: A static OK payload.
    :rtype: dict
    """
    return {"status": "ok", "service": settings.APP_NAME, "version": settings.APP_VERSION}


@router.get("/status")
async def status() -> dict:
    """
    Readiness probe — the database is reachable, plus the latest recorded run
    per Celery task ("did last night's curation actually run?").

    :return: ``{"status": "ok"|"degraded", "database": bool, "profile_empty": bool,
        "tasks": {name: {state, finished_at}}}``.
    :rtype: dict
    """
    db_ok = True
    profile_empty = True
    tasks: dict = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            latest = select(TaskRun).distinct(TaskRun.task_name).order_by(TaskRun.task_name, TaskRun.finished_at.desc())
            for run in (await conn.execute(latest)).all():
                tasks[run.task_name] = {"state": run.state, "finished_at": run.finished_at.isoformat()}
            # Empty profile means collectors run broad/global queries — surface it here.
            pref = (
                await conn.execute(
                    select(Preference.stacks, Preference.areas, Preference.keywords).where(
                        Preference.id == SINGLETON_ID
                    )
                )
            ).first()
            profile_empty = pref is None or not (pref.stacks or pref.areas or pref.keywords)
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "profile_empty": profile_empty, "tasks": tasks}
