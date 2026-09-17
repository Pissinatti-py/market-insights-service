from collections.abc import AsyncIterator

import redis.asyncio
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.core.conf import settings
from src.core.events import CHANNEL

router = APIRouter(prefix="/events", tags=["Events"])

_KEEPALIVE_SECONDS = 15


async def _stream() -> AsyncIterator[str]:
    client = redis.asyncio.Redis.from_url(settings.REDIS_URL)
    pubsub = client.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        yield "retry: 3000\n\n"
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_KEEPALIVE_SECONDS)
            yield f"data: {msg['data'].decode()}\n\n" if msg else ": ping\n\n"
    finally:
        await pubsub.aclose()
        await client.aclose()


@router.get("", include_in_schema=False)
async def stream_events() -> StreamingResponse:
    """Server-sent events: one ``{"kind": "curation"|"task", ...}`` per change, a comment every 15 s to keep the connection alive."""
    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
