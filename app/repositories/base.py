# app/repositories/base.py
from typing import TypeVar, Generic, Type, Optional, Sequence, Any
from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

ModelType = TypeVar("ModelType")


class BaseRepository(Generic[ModelType]):
    """
    Generic store_id-scoped (and, where applicable, branch_id-scoped) repository.

    এই repository যে model এর জন্য use করবেন, সেই model এ অবশ্যই
    `store_id` কলাম থাকতে হবে। প্রতিটা read/update/delete automatically
    store_id দিয়ে filtered হয় — manually WHERE store_id=... লেখার
    দরকার নেই, এবং ভুলে যাওয়ার (accidental cross-tenant leak) সুযোগ নেই।

    branch_id (optional): যদি caller (get_repository) একটা branch_id পাস করে
    (branch-bound staff এর ক্ষেত্রে member.branch_id), এবং model-এ
    `branch_id` column থাকে, তাহলে সব query automatically সেই branch-এও
    scoped হয়ে যাবে। Model-এ branch_id না থাকলে (e.g. Customer, Medicine)
    এই filter silently skip হয় — কোনো crash হয় না।
    """

    def __init__(
        self,
        db: AsyncSession,
        model: Type[ModelType],
        store_id: int,
        branch_id: Optional[int] = None,
    ):
        self.db = db
        self.model = model
        self.store_id = store_id
        self.branch_id = branch_id
        self._model_has_branch = hasattr(model, "branch_id")

    def _base_query(self):
        query = select(self.model).where(self.model.store_id == self.store_id)
        if self.branch_id is not None and self._model_has_branch:
            query = query.where(self.model.branch_id == self.branch_id)
        return query

    async def get_by_id(self, record_id: int) -> Optional[ModelType]:
        result = await self.db.execute(
            self._base_query().where(self.model.id == record_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id_or_404(self, record_id: int) -> ModelType:
        obj = await self.get_by_id(record_id)
        if obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{self.model.__name__} not found",
            )
        return obj

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        order_by: Any = None,
        filters: Optional[dict] = None,
    ) -> Sequence[ModelType]:
        """
        filters: extra equality filters, e.g. {"is_active": True}
        (branch_id আলাদা করে filters-এ দেওয়ার দরকার নেই — constructor-এর
        branch_id দিয়েই automatic scoping হয়ে যায়)
        """
        query = self._base_query()
        if filters:
            for key, value in filters.items():
                query = query.where(getattr(self.model, key) == value)
        if order_by is not None:
            query = query.order_by(order_by)
        query = query.offset(skip).limit(limit)

        result = await self.db.execute(query)
        return result.scalars().all()

    async def count(self, filters: Optional[dict] = None) -> int:
        query = select(func.count()).select_from(self.model).where(
            self.model.store_id == self.store_id
        )
        if self.branch_id is not None and self._model_has_branch:
            query = query.where(self.model.branch_id == self.branch_id)
        if filters:
            for key, value in filters.items():
                query = query.where(getattr(self.model, key) == value)
        result = await self.db.execute(query)
        return result.scalar_one()

    async def create(self, **kwargs) -> ModelType:
        kwargs["store_id"] = self.store_id  # caller যাই পাঠাক, override করে current store বসিয়ে দেওয়া হয়

        if self._model_has_branch and self.branch_id is not None:
            # Branch-bound staff: created record forcibly তাদের নিজের branch-এ যাবে,
            # caller অন্য branch_id পাঠালেও সেটা override হয়ে যাবে।
            kwargs["branch_id"] = self.branch_id

        obj = self.model(**kwargs)
        self.db.add(obj)
        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def update(self, record_id: int, **kwargs) -> Optional[ModelType]:
        kwargs.pop("store_id", None)  # কোনো record অন্য store এ move করা যাবে না
        kwargs.pop("id", None)

        if self._model_has_branch and self.branch_id is not None:
            # branch-bound staff অন্য branch-এ record move করতে পারবে না
            kwargs.pop("branch_id", None)

        obj = await self.get_by_id(record_id)
        if obj is None:
            return None

        for key, value in kwargs.items():
            setattr(obj, key, value)

        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def delete(self, record_id: int) -> bool:
        obj = await self.get_by_id(record_id)
        if obj is None:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True