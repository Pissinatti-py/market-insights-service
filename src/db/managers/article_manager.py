from src.db.managers.base_manager import BaseManager
from src.models.article import Article


class ArticleRepository(BaseManager[Article]):
    def __init__(self) -> None:
        super().__init__(model=Article)
