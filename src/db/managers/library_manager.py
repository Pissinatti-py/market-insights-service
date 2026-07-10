from src.db.managers.base_manager import BaseManager
from src.models.library import Library, LibraryRelease


class LibraryRepository(BaseManager[Library]):
    def __init__(self) -> None:
        super().__init__(model=Library)


class LibraryReleaseRepository(BaseManager[LibraryRelease]):
    def __init__(self) -> None:
        super().__init__(model=LibraryRelease)
