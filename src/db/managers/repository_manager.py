from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from src.db.managers.base_manager import BaseManager
from src.models.repository import Repository, RepositorySnapshot


class RepositoryRepository(BaseManager[Repository]):
    """Repository data access. (Yes, the name is a pun the codebase has to live with.)"""

    def __init__(self) -> None:
        super().__init__(model=Repository)

    async def snapshots_since(self, db: AsyncSession, repository_id, since: datetime) -> list[RepositorySnapshot]:
        """Snapshots for one repository captured on/after ``since``, oldest first."""
        query = (
            select(RepositorySnapshot)
            .where(RepositorySnapshot.repository_id == repository_id)
            .where(RepositorySnapshot.captured_at >= since)
            .order_by(RepositorySnapshot.captured_at.asc())
        )
        return list((await db.execute(query)).scalars().all())
