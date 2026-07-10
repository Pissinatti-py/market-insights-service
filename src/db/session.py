from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

from src.core.conf import settings

DATABASE_URL = settings.DATABASE_URL
SYNC_DATABASE_URL = DATABASE_URL.replace("+asyncpg", "")

# Async engine — drives the FastAPI request path.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_recycle=300,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

# Sync engine — used by Celery tasks and Alembic (mirrors pissync-core). Every
# collector/curation task opens ``with SyncSession() as session:``.
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=False,
    pool_size=3,
    max_overflow=5,
    pool_recycle=300,
    pool_pre_ping=True,
)

SyncSession = sessionmaker(bind=sync_engine)


async def get_db_async_session() -> AsyncSession:
    """
    FastAPI dependency yielding an async session, closed when the request ends.

    :return: An async generator yielding a single ``AsyncSession``.
    :rtype: AsyncSession
    """
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
