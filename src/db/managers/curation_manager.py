from pydantic import BaseModel

from src.db.managers.base_manager import BaseManager
from src.models.curation import Curation
from src.schemas.curation_schema import CurationReview


class _Empty(BaseModel):
    pass


class CurationRepository(BaseManager[Curation, _Empty, CurationReview]):
    def __init__(self) -> None:
        super().__init__(model=Curation)
