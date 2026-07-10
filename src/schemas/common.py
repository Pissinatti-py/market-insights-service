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

    @classmethod
    def from_result(cls, result, items: List[T] | None = None) -> "Page[T]":
        """Envelope a manager ``PaginatedResult``; ``items`` overrides when rows were re-mapped."""
        return cls(
            total=result.total,
            items=items if items is not None else result.items,
            page=result.page,
            per_page=result.per_page,
            num_pages=result.num_pages,
        )
