"""Idempotency of the dedup upsert — re-running a collector inserts nothing new."""

from datetime import datetime, timezone

from src.db.session import SyncSession
from src.db.upsert import bulk_upsert_dedup
from src.models.repository import Repository


def _row(dedup_key: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "dedup_key": dedup_key,
        "url": f"https://github.com/{dedup_key}",
        "owner": dedup_key.split("/")[0],
        "name": dedup_key.split("/")[1],
        "stars": 100,
        "forks": 1,
        "stars_per_week": 5.0,
        "relevance_score": 1.0,
        "languages": ["python"],
        "topics": ["x"],
        "collected_at": now,
    }


def test_upsert_is_idempotent_on_dedup_key():
    with SyncSession() as session:
        first = bulk_upsert_dedup(session, Repository, [_row("a/one"), _row("b/two")])
        assert len(first) == 2

        # Same dedup_keys again → ON CONFLICT DO NOTHING → zero new rows.
        second = bulk_upsert_dedup(session, Repository, [_row("a/one"), _row("b/two")])
        assert second == []

        # A new key still inserts.
        third = bulk_upsert_dedup(session, Repository, [_row("c/three")])
        assert len(third) == 1

        total = session.query(Repository).count()
        assert total == 3
