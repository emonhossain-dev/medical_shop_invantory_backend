from datetime import datetime, timedelta, timezone
import re
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.all_models import (
    Store,
    StoreMember,
    Branch,
    StaffRole,
    StoreStatus,
    User,
    Subscription,
)
from app.depends.deps import get_current_user
from app.schemas.store import (
    StoreCreateRequest,
    StoreResponse,
    StoreUpdateRequest,
)


router = APIRouter(prefix="/store", tags=["Store"])


def make_slug(text: str) -> str:
    """Pure Python slug generator"""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = re.sub(r'^-+|-+$', '', text)
    return text[:200] or "store"


async def generate_unique_slug(name: str, db: AsyncSession) -> str:
    base = make_slug(name)
    slug = base
    counter = 1

    while True:
        result = await db.execute(select(Store).where(Store.slug == slug))
        if not result.scalar_one_or_none():
            return slug
        slug = f"{base}-{counter}"
        counter += 1


async def get_store_or_404(store_id: int, db: AsyncSession) -> Store:
    result = await db.execute(select(Store).where(Store.id == store_id))
    store = result.scalar_one_or_none()
    if not store:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


async def get_store_with_relations_or_404(store_id: int, db: AsyncSession) -> Store:
    """Same as get_store_or_404 but eager-loads branches + members so that
    the ORM-level cascade="all, delete-orphan" on Store.branches /
    Store.members actually fires when we db.delete(store). Without loading
    these, SQLAlchemy doesn't know the children exist and won't cascade the
    delete, which would otherwise hit a FK constraint error at the DB."""
    result = await db.execute(
        select(Store)
        .options(
            selectinload(Store.branches),
            selectinload(Store.members),
        )
        .where(Store.id == store_id)
    )
    store = result.scalar_one_or_none()
    if not store:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


async def check_store_owner(store: Store, current_user: User) -> None:
    if store.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the store owner can perform this action"
        )


class StoreListItem(BaseModel):
    id: int
    name: str
    slug: str
    status: StoreStatus
    role: StaffRole
    is_owner: bool

    model_config = {"from_attributes": True}


# -------------------- LIST (my stores) --------------------
@router.get("/my/list", response_model=list[StoreListItem])
async def list_my_stores(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns every store the current user has active access to, either as
    owner or as staff, along with their role in that store. The owner is
    also inserted as a StoreMember (role=owner) at creation time, so a
    single query against store_members covers both cases."""
    result = await db.execute(
        select(StoreMember, Store)
        .join(Store, StoreMember.store_id == Store.id)
        .where(
            StoreMember.user_id == current_user.id,
            StoreMember.is_active == True,
        )
        .order_by(Store.created_at)
    )
    rows = result.all()

    return [
        StoreListItem(
            id=store.id,
            name=store.name,
            slug=store.slug,
            status=store.status,
            role=member.role,
            is_owner=(store.owner_id == current_user.id),
        )
        for member, store in rows
    ]


# -------------------- CREATE --------------------
@router.post("", response_model=StoreResponse, status_code=status.HTTP_201_CREATED)
async def create_store(
    data: StoreCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Slug handle
    if data.slug:
        existing = await db.execute(select(Store).where(Store.slug == data.slug))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This slug is already taken")
        slug = data.slug
    else:
        slug = await generate_unique_slug(data.name, db)

    # Store create
    store = Store(
        owner_id=current_user.id,
        name=data.name,
        slug=slug,
        address=data.address,
        phone=data.phone,
        status=StoreStatus.trial,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.add(store)
    await db.flush()

    # Main Branch
    main_branch = Branch(
        store_id=store.id,
        name="Main Branch",
        address=data.address,
        phone=data.phone,
        is_main=True,
        is_active=True,
    )
    db.add(main_branch)
    await db.flush()

    # Owner as StoreMember
    membership = StoreMember(
        store_id=store.id,
        user_id=current_user.id,
        branch_id=main_branch.id,
        role=StaffRole.owner,
        is_active=True,
    )
    db.add(membership)

    await db.commit()
    await db.refresh(store)

    return StoreResponse.model_validate(store)


# -------------------- GET --------------------
@router.get("/{store_id}", response_model=StoreResponse)
async def get_store(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_or_404(store_id, db)

    # Owner or active member check
    result = await db.execute(
        select(StoreMember).where(
            StoreMember.store_id == store_id,
            StoreMember.user_id == current_user.id,
            StoreMember.is_active == True,
        )
    )
    membership = result.scalar_one_or_none()
    if not membership and store.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this store")

    return StoreResponse.model_validate(store)


# -------------------- UPDATE --------------------
@router.put("/{store_id}", response_model=StoreResponse)
async def update_store(
    store_id: int,
    data: StoreUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_or_404(store_id, db)
    await check_store_owner(store, current_user)

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    for field, value in update_data.items():
        setattr(store, field, value)

    store.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(store)

    return StoreResponse.model_validate(store)


# -------------------- DELETE --------------------
@router.delete("/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_with_relations_or_404(store_id, db)
    await check_store_owner(store, current_user)

    # Subscription has no cascade defined on the Store side, so deleting a
    # store with a live (trialing/active) subscription would either orphan
    # billing records or blow up on the FK -- block it explicitly and make
    # the owner cancel/downgrade first. Adjust the status list if your
    # SubscriptionStatus enum uses different values.
    result = await db.execute(
        select(Subscription).where(
            Subscription.store_id == store_id,
            Subscription.status.in_(["trialing", "active"]),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a store with an active or trialing subscription. Cancel it first.",
        )

    # branches + members are already eager-loaded, so the ORM-level
    # cascade="all, delete-orphan" on Store.branches / Store.members will
    # correctly emit DELETE statements for them before/along with the store.
    await db.delete(store)
    await db.commit()
    return None