"""Every Curation insert and every recorded task run publishes a change event."""

import uuid
from types import SimpleNamespace

import pytest

from src.core import events
from src.core.celery.task_runs import record_task_run
from src.db.session import SyncSession
from src.models.curation import Curation, CurationItemType


@pytest.fixture
def published(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(events, "publish", lambda kind, **data: calls.append({"kind": kind, **data}))
    return calls


@pytest.mark.asyncio
async def test_curation_insert_publishes_event(published):
    with SyncSession() as session:
        session.add(Curation(item_type=CurationItemType.ARTICLE, item_id=uuid.uuid4(), summary="s"))
        session.commit()

    assert published == [{"kind": "curation", "item_type": "article", "status": "pending"}]


@pytest.mark.asyncio
async def test_task_run_publishes_event(published):
    record_task_run(task=SimpleNamespace(name="src.tasks.demo.collect"), retval={"inserted": 2}, state="SUCCESS")

    assert published == [
        {"kind": "task", "name": "src.tasks.demo.collect", "state": "SUCCESS", "result": {"inserted": 2}}
    ]
