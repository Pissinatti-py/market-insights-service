from pydantic import BaseModel

from src.db.managers.base_manager import BaseManager
from src.models.library import Library, LibraryRelease
from src.schemas.library_schema import LibraryCreate


class _Empty(BaseModel):
    pass


class LibraryRepository(BaseManager[Library, LibraryCreate, _Empty]):
    def __init__(self) -> None:
        super().__init__(model=Library)


class LibraryReleaseRepository(BaseManager[LibraryRelease, _Empty, _Empty]):
    def __init__(self) -> None:
        super().__init__(model=LibraryRelease)
