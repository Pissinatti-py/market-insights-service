"""
Idempotent bulk upsert keyed on ``dedup_key``.

The collector pattern from ``pissync-core``'s ``scan_expiring_policies``:
``INSERT … ON CONFLICT (dedup_key) DO NOTHING`` so re-running a collector is a
no-op for rows already stored. Runs on the **sync** session (collectors are
Celery tasks). Returns the rows actually inserted so the caller can log / chain
curation only over the new ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Type

from pytz import utc
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.db.base import Base


def bulk_upsert_dedup(session: Session, model: Type[Base], rows: list[dict]) -> list:
    """
    Insert ``rows`` into ``model``, skipping any whose ``dedup_key`` already exists.

    ``id`` / ``created_at`` / ``updated_at`` are filled per row when absent —
    Python-side column defaults are not reliably applied to a multi-row Core
    ``VALUES`` insert. Commits before returning.

    :param session: Active sync session.
    :type session: Session
    :param model: The mapped class to insert into; must have a unique ``dedup_key``.
    :type model: Type[Base]
    :param rows: Column-value dicts (each must include ``dedup_key``).
    :type rows: list[dict]
    :return: The freshly-inserted rows (excludes deduped ones).
    :rtype: list
    """
    if not rows:
        return []

    now = datetime.now(utc)
    columns = model.__table__.columns
    prepared = []
    for row in rows:
        base = {}
        if "id" in columns:
            base["id"] = row.get("id") or uuid.uuid4()
        if "created_at" in columns:
            base["created_at"] = row.get("created_at") or now
        if "updated_at" in columns:
            base["updated_at"] = row.get("updated_at") or now
        prepared.append({**base, **row})

    stmt = pg_insert(model).values(prepared).on_conflict_do_nothing(index_elements=["dedup_key"]).returning(model.id)
    inserted_ids = [r[0] for r in session.execute(stmt).all()]
    session.commit()
    if not inserted_ids:
        return []

    from sqlalchemy import select

    fetched = session.execute(select(model).where(model.id.in_(inserted_ids))).scalars().all()
    return list(fetched)
