from typing import Any, Generic, TypeVar
from uuid import uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


class SqlAlchemyRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, statement: Select | None = None) -> list[ModelT]:
        stmt = statement if statement is not None else select(self.model)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, item_id: str) -> ModelT | None:
        return await self.session.get(self.model, item_id)

    async def create(self, values: dict[str, Any]) -> ModelT:
        values.setdefault("id", str(uuid4()))
        row = self.model(**values)
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def update(self, row: ModelT, values: dict[str, Any]) -> ModelT:
        for key, value in values.items():
            setattr(row, key, value)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def delete(self, row: ModelT) -> None:
        await self.session.delete(row)
        await self.session.flush()

