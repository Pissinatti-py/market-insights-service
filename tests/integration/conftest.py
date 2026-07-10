"""Fixtures for the DB-backed integration suite.

Runs against a **real** PostgreSQL (production parity — asyncpg + JSONB +
ON CONFLICT). The environment is pinned before any ``src`` import (the engine is
built at import time), and we refuse to run against a DB whose name doesn't look
like a test DB so a stray ``DATABASE_URL`` can't truncate real data.
"""

import os

_DEFAULT_DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/market_insights_test"

_test_db = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or _DEFAULT_DB
_db_name = _test_db.rsplit("/", 1)[-1].split("?")[0]
if "test" not in _db_name and os.environ.get("ALLOW_NONTEST_DB") != "1":
    raise RuntimeError(
        f"Refusing to run integration tests against database {_db_name!r}: it is not a test database. "
        "Set TEST_DATABASE_URL to a dedicated DB or ALLOW_NONTEST_DB=1 to override."
    )
os.environ["DATABASE_URL"] = _test_db


def _ensure_test_database(url: str) -> None:
    """Create the test database if missing (a bare ``pytest`` against a fresh server)."""
    import psycopg2
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    try:
        conn = psycopg2.connect(
            host=parsed.host, port=parsed.port, user=parsed.username, password=parsed.password, dbname="postgres"
        )
    except Exception:
        return
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (parsed.database,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        conn.close()


_ensure_test_database(_test_db)

import asyncio  # noqa: E402
from collections.abc import AsyncIterator, Iterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import src.models  # noqa: E402,F401 — register mappers
from src.core.conf import settings  # noqa: E402
from src.db.base import Base  # noqa: E402
from src.db.session import get_db_async_session  # noqa: E402
from src.main import app  # noqa: E402

# NullPool: each session opens/closes its own connection within the per-test loop
# pytest-asyncio creates, so nothing is reused across loops.
test_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
TestSession = async_sessionmaker(bind=test_engine, expire_on_commit=False)

_TABLES = (
    "mi__repository_snapshots, mi__repositories, mi__library_releases, mi__libraries, "
    "mi__articles, mi__curation, mi__preferences, mi__task_runs"
)


async def _override_get_db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session


app.dependency_overrides[get_db_async_session] = _override_get_db


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    async with TestSession() as session:
        await session.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
        await session.commit()
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
