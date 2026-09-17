# app/repositories/deps.py
from typing import Type, TypeVar
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member
from app.models.all_models import StoreMember
from app.repositories.base import BaseRepository

ModelType = TypeVar("ModelType")


def get_repository(model: Type[ModelType]):
    """
    Usage in a router:

        @router.get("/suppliers")
        async def list_suppliers(
            repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
        ):
            return await repo.list()

    X-Store-Id header থেকে resolved store_id automatically bind হয়ে যায়
    (get_current_store_member এর মাধ্যমে) — router এ store_id নিয়ে কিছু
    ভাবতে হবে না।

    branch-bound staff (member.branch_id সেট করা থাকলে) হলে সেটাও
    automatically repo-তে bind হয়ে যায় — model-এ branch_id column থাকলে
    সব read/write সেই branch-এও scoped হবে। Store-wide role হলে
    (member.branch_id is None -> owner/manager) branch filter apply হবে না,
    সব branch-এর data দেখা যাবে।
    """
    async def _get_repo(
        db: AsyncSession = Depends(get_db),
        member: StoreMember = Depends(get_current_store_member),
    ) -> BaseRepository[ModelType]:
        return BaseRepository(
            db=db,
            model=model,
            store_id=member.store_id,
            branch_id=getattr(member, "branch_id", None),
        )

    return _get_repo