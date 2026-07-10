from src.db.managers.base_manager import BaseManager
from src.models.curation import Curation


class CurationRepository(BaseManager[Curation]):
    def __init__(self) -> None:
        super().__init__(model=Curation)
