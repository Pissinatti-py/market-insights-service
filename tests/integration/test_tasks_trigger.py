"""On-demand task triggering: ``POST /api/tasks/{name}/trigger`` enqueues a
whitelisted Celery task (enqueue is monkeypatched — no broker in the test env)."""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

import src.api.tasks_router as tasks_router


@pytest.mark.asyncio
async def test_trigger_enqueues_whitelisted_task(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    sent = []

    def fake_send_task(name):
        sent.append(name)
        return SimpleNamespace(id="fake-task-id")

    monkeypatch.setattr(tasks_router.celery_app, "send_task", fake_send_task)

    resp = await client.post("/api/tasks/collect_trending/trigger")
    assert resp.status_code == 202
    body = resp.json()
    assert body["task"] == "src.tasks.github_tasks.collect_trending"
    assert body["task_id"] == "fake-task-id"
    assert sent == ["src.tasks.github_tasks.collect_trending"]


@pytest.mark.asyncio
async def test_trigger_unknown_task_is_404(client: AsyncClient):
    resp = await client.post("/api/tasks/rm_rf_slash/trigger")
    assert resp.status_code == 404
