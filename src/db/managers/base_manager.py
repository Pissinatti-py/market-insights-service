"""
Generic async CRUD manager — a trimmed copy of pissync-core's BaseManager.

This service has no users, ownership scoping, or SSE streaming, so the owner /
authorship / lifecycle-publish machinery is dropped. What remains is the part
every repository actually uses: typed CRUD, filtered listing, ordering, and
SQL-level pagination.
"""

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any, Dict, Generic, List, Optional, Sequence, Type, TypeVar, Union

from pydantic import BaseModel
from pytz import utc
from sqlalchemy import ColumnElement, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm.interfaces import LoaderOption

ModelType = TypeVar("ModelType", bound=DeclarativeBase)
T = TypeVar("T")


@dataclass
class PaginatedResult(Generic[T]):
    total: int
    items: List[T]
    page: int
    per_page: int
    num_pages: int


class BaseManager(Generic[ModelType]):
    """Generic async CRUD for a SQLAlchemy model."""

    def __init__(self, model: Type[ModelType]):
        self.model = model

    async def create(
        self,
        db: AsyncSession,
        obj_in: Union[BaseModel, Dict[str, Any]],
        auto_commit: bool = True,
    ) -> ModelType:
        """
        Create and persist a new model instance.

        :param db: Database session.
        :type db: AsyncSession
        :param obj_in: Data for the instance (Pydantic model or dict).
        :type obj_in: Union[BaseModel, Dict[str, Any]]
        :param auto_commit: Commit immediately, defaults to True.
        :type auto_commit: bool
        :return: The created instance.
        :rtype: ModelType
        """
        obj_data = obj_in if isinstance(obj_in, dict) else obj_in.model_dump(exclude_unset=True)
        db_obj = self.model(**obj_data)
        db.add(db_obj)
        await db.flush()
        if auto_commit:
            await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def get(
        self,
        db: AsyncSession,
        id: Any,
        options: Optional[Sequence[LoaderOption]] = None,
    ) -> Optional[ModelType]:
        """
        Get a single instance by ID.

        :param db: Database session.
        :type db: AsyncSession
        :param id: Primary key.
        :type id: Any
        :param options: SQLAlchemy loader options.
        :type options: Optional[Sequence[LoaderOption]]
        :return: The instance, or None.
        :rtype: Optional[ModelType]
        """
        query = select(self.model).where(self.model.id == id)
        if options:
            query = query.options(*options)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_field(
        self,
        db: AsyncSession,
        field_name: str,
        field_value: Any,
    ) -> Optional[ModelType]:
        """
        Get a single instance by a field value.

        :param db: Database session.
        :type db: AsyncSession
        :param field_name: Field to filter by.
        :type field_name: str
        :param field_value: Value to match.
        :type field_value: Any
        :raises AttributeError: If the field does not exist on the model.
        :return: The instance, or None.
        :rtype: Optional[ModelType]
        """
        if not hasattr(self.model, field_name):
            raise AttributeError(f"Model {self.model.__name__} has no field '{field_name}'")
        field = getattr(self.model, field_name)
        result = await db.execute(select(self.model).where(field == field_value))
        return result.scalar_one_or_none()

    def _order_columns(self, order_by: Optional[str]) -> List[ColumnElement]:
        """
        Translate an ``order_by`` spec (``"-created_at,id"``) into columns.

        :raises ValueError: If a field doesn't exist on the model. Skipping it
            silently would return rows in insertion order while reporting success;
            callers taking ``order_by`` from a request constrain it with a
            ``Literal`` so a bad value is a 422 before it ever reaches here.
        """
        columns: List[ColumnElement] = []
        if not order_by:
            return columns
        for token in order_by.split(","):
            token = token.strip()
            if not token:
                continue
            descending = token.startswith("-")
            field_name = token[1:] if descending else token
            if not hasattr(self.model, field_name):
                raise ValueError(f"unknown order field {field_name!r} for {self.model.__name__}")
            field = getattr(self.model, field_name)
            columns.append(field.desc() if descending else field.asc())
        return columns

    async def get_multi(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        expressions: Optional[Sequence[ColumnElement]] = None,
    ) -> List[ModelType]:
        """
        List instances with optional equality filters, ordering, and pagination.

        :param db: Database session.
        :param skip: Rows to skip.
        :param limit: Max rows.
        :param filters: ``{field: value}`` equality filters (list value → IN).
        :param order_by: Order spec; prefix ``-`` for descending.
        :param expressions: Raw SQLAlchemy WHERE clauses.
        :return: Matching instances.
        :rtype: List[ModelType]
        """
        query = select(self.model)
        query = self._apply_filters(query, filters, expressions)
        order_columns = self._order_columns(order_by)
        if order_columns:
            query = query.order_by(*order_columns)
        query = query.offset(skip).limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    def _apply_filters(self, query, filters, expressions):
        """Apply equality filters and raw expressions to a select() query."""
        if expressions:
            for expr in expressions:
                query = query.where(expr)
        if filters:
            for field_name, field_value in filters.items():
                if hasattr(self.model, field_name):
                    field = getattr(self.model, field_name)
                    if isinstance(field_value, list):
                        query = query.where(field.in_(field_value))
                    else:
                        query = query.where(field == field_value)
        return query

    async def count(
        self,
        db: AsyncSession,
        filters: Optional[Dict[str, Any]] = None,
        expressions: Optional[Sequence[ColumnElement]] = None,
    ) -> int:
        """
        Count instances matching the filters.

        :param db: Database session.
        :param filters: Equality filters.
        :param expressions: Raw WHERE clauses.
        :return: Number of matching rows.
        :rtype: int
        """
        query = select(func.count(self.model.id))
        query = self._apply_filters(query, filters, expressions)
        result = await db.execute(query)
        return result.scalar()

    async def paginate(
        self,
        db: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        expressions: Optional[Sequence[ColumnElement]] = None,
    ) -> "PaginatedResult[ModelType]":
        """
        SQL-level paginated result: COUNT(*) + SELECT … LIMIT/OFFSET.

        :param db: Database session.
        :param page: 1-based page number.
        :param per_page: Rows per page.
        :param filters: Equality filters.
        :param order_by: Order spec.
        :param expressions: Raw WHERE clauses.
        :return: A PaginatedResult.
        :rtype: PaginatedResult[ModelType]
        """
        total = await self.count(db, filters=filters, expressions=expressions)
        skip = (page - 1) * per_page
        items = await self.get_multi(
            db, skip=skip, limit=per_page, filters=filters, order_by=order_by, expressions=expressions
        )
        num_pages = max(1, ceil(total / per_page))
        return PaginatedResult(total=total, items=items, page=page, per_page=per_page, num_pages=num_pages)

    async def update(
        self,
        db: AsyncSession,
        id: Any,
        obj_in: Union[BaseModel, Dict[str, Any]],
    ) -> Optional[ModelType]:
        """
        Update an instance by ID. Returns None if not found.

        :param db: Database session.
        :param id: Primary key.
        :param obj_in: Update payload (Pydantic model or dict).
        :return: The updated instance, or None.
        :rtype: Optional[ModelType]
        """
        db_obj = await self.get(db, id)
        if not db_obj:
            return None
        update_data = obj_in if isinstance(obj_in, dict) else obj_in.model_dump(exclude_unset=True)
        if update_data and "updated_at" not in update_data and "updated_at" in self.model.__table__.columns:
            update_data = {**update_data, "updated_at": datetime.now(utc)}
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def soft_delete(self, db: AsyncSession, id: Any) -> bool:
        """
        Stamp ``deleted_at`` instead of removing the row (audit trail).

        :param db: Database session.
        :param id: Primary key.
        :return: True if a row was stamped.
        :rtype: bool
        """
        result = await db.execute(update(self.model).where(self.model.id == id).values(deleted_at=datetime.now(utc)))
        await db.commit()
        return result.rowcount > 0
