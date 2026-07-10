from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.db.managers.base_manager import BaseManager
from src.models.preference import SINGLETON_ID, Preference


def profile_is_empty(pref: Preference) -> bool:
    """
    True when the profile has nothing to target — collectors degrade to broad,
    untargeted queries (and Medium RSS is skipped entirely).

    :param pref: The preferences row.
    :type pref: Preference
    :return: Whether stacks, areas, and keywords are all empty.
    :rtype: bool
    """
    return not (pref.stacks or pref.areas or pref.keywords)


def get_or_create_sync(session: Session) -> Preference:
    """
    Return the singleton preferences row on the **sync** engine (for collectors).

    Collectors run inside Celery tasks on ``SyncSession``; they read the profile
    to build queries. Creates an empty row on first access.

    :param session: Active sync session.
    :type session: Session
    :return: The single preferences row.
    :rtype: Preference
    """
    pref = session.get(Preference, SINGLETON_ID)
    if pref is None:
        pref = Preference(id=SINGLETON_ID)
        session.add(pref)
        session.commit()
        session.refresh(pref)
    return pref


class PreferenceRepository(BaseManager[Preference]):
    def __init__(self) -> None:
        super().__init__(model=Preference)

    async def get_or_create(self, db: AsyncSession) -> Preference:
        """
        Return the singleton preferences row, creating an empty one on first read.

        :param db: Database session.
        :type db: AsyncSession
        :return: The single preferences row.
        :rtype: Preference
        """
        pref = await self.get(db, SINGLETON_ID)
        if pref is None:
            pref = Preference(id=SINGLETON_ID)
            db.add(pref)
            await db.commit()
            await db.refresh(pref)
        return pref
