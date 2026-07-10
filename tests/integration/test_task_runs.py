"""Task-run bookkeeping: the postrun signal writes ``mi__task_runs`` and
``/status`` surfaces the latest state per task."""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from src.core.celery.task_runs import record_task_run


@pytest.mark.asyncio
async def test_task_run_recorded_and_latest_surfaced(client: AsyncClient):
    task = SimpleNamespace(name="src.tasks.demo.collect")
    record_task_run(task=task, retval=RuntimeError("boom"), state="FAILURE")
    record_task_run(task=task, retval={"fetched": 2}, state="SUCCESS")

    body = (await client.get("/status")).json()
    run = body["tasks"]["src.tasks.demo.collect"]
    assert run["state"] == "SUCCESS"
    assert run["finished_at"] is not None


@pytest.mark.asyncio
async def test_status_has_empty_tasks_without_runs(client: AsyncClient):
    body = (await client.get("/status")).json()
    assert body["tasks"] == {}
