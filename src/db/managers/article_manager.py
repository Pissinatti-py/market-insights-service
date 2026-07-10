from pydantic import BaseModel

from src.db.managers.base_manager import BaseManager
from src.models.article import Article
from src.schemas.article_schema import ArticleCreate


class _Empty(BaseModel):
    pass


class ArticleRepository(BaseManager[Article, ArticleCreate, _Empty]):
    def __init__(self) -> None:
        super().__init__(model=Article)
