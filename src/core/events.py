"""
Change notifications for the review UI.

Every ``Curation`` insert and every finished task publishes a small JSON event on
the ``mi:events`` Redis channel; ``GET /api/events`` streams it to browsers as
server-sent events. Publishing is fire-and-forget: an unreachable Redis must
never break the task that triggered it.
"""

from __future__ import annotations

import json

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import event

from src.core.conf import settings
from src.models.curation import Curation
from src.services.logger_service import logger

CHANNEL = "mi:events"


def publish(kind: str, **data) -> None:
    """PUBLISH one ``{"kind": ..., **data}`` event; logs and swallows Redis errors."""
    try:
        # ponytail: new client per publish (~1 event / 35 s); pool it if curation ever gets fast.
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        client.publish(CHANNEL, json.dumps({"kind": kind, **data}))
    except RedisError as exc:
        logger.warning(f"events: publish failed: {exc}")


@event.listens_for(Curation, "after_insert")
def _curation_inserted(mapper, connection, target: Curation) -> None:
    publish("curation", item_type=target.item_type.value, status=target.status.value)
