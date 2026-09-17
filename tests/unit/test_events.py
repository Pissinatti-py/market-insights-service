"""Change-notification plumbing: the Redis publisher never raises, and the SSE stream relays pubsub."""

from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.api import events_router
from src.core import events


def test_publish_swallows_redis_errors(monkeypatch):
    class Boom:
        @staticmethod
        def from_url(*a, **k):
            raise RedisConnectionError("redis down")

    monkeypatch.setattr(events, "Redis", Boom)
    events.publish("curation", item_type="article")  # must not raise


class _FakePubSub:
    def __init__(self):
        self.messages = [{"data": b'{"kind": "curation"}'}, None]
        self.closed = False

    async def subscribe(self, channel):
        self.channel = channel

    async def get_message(self, **kwargs):
        return self.messages.pop(0)

    async def aclose(self):
        self.closed = True


class _FakeRedis:
    pubsub_instance = _FakePubSub()

    @classmethod
    def from_url(cls, url):
        return cls()

    def pubsub(self):
        return self.pubsub_instance

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_stream_relays_messages_and_pings(monkeypatch):
    monkeypatch.setattr(events_router, "redis", SimpleNamespace(asyncio=SimpleNamespace(Redis=_FakeRedis)))
    gen = events_router._stream()

    assert await anext(gen) == "retry: 3000\n\n"
    assert await anext(gen) == 'data: {"kind": "curation"}\n\n'
    assert await anext(gen) == ": ping\n\n"

    await gen.aclose()
    assert _FakeRedis.pubsub_instance.channel == events.CHANNEL
    assert _FakeRedis.pubsub_instance.closed  # client disconnect releases the subscription
