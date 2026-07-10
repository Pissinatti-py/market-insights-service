from typing import Generic, List, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Generic paginated response envelope."""

    total: int
    items: List[T]
    page: int
    per_page: int
    num_pages: int
