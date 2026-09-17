from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.all_models import Branch, Store, StoreMember, Subscription, SubscriptionPlan
from app.schemas.store import BranchCreateRequest, BranchUpdateRequest, BranchResponse
from app.depends.deps import get_current_store_member, require_store_admin

router = APIRouter(prefix="/branches", tags=["Branch"])


@router.post("", response_model=BranchResponse, status_code=201)
async def create_branch(
    data: BranchCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    store_id = member.store_id

    # Plan limit check
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.store_id == store_id,
            Subscription.status.in_(["trialing", "active"])
        )
    )
    subscription = result.scalar_one_or_none()

    if subscription and subscription.plan.max_branches is not None:
        count_result = await db.execute(
            select(func.count()).select_from(Branch).where(Branch.store_id == store_id)
        )
        current_count = count_result.scalar()
        if current_count >= subscription.plan.max_branches:
            raise HTTPException(
                status_code=400,
                detail=f"Branch limit reached. Your plan allows max {subscription.plan.max_branches} branches."
            )

    # name unique check
    existing = await db.execute(
        select(Branch).where(Branch.store_id == store_id, Branch.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Branch name already exists")

    branch = Branch(
        store_id=store_id,
        name=data.name,
        address=data.address,
        phone=data.phone,
        is_main=data.is_main,
    )
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return BranchResponse.model_validate(branch)


@router.get("", response_model=list[BranchResponse])
async def list_branches(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Branch)
        .where(Branch.store_id == member.store_id)
        .order_by(Branch.is_main.desc(), Branch.created_at)
    )
    branches = result.scalars().all()
    return [BranchResponse.model_validate(b) for b in branches]


@router.patch("/{branch_id}", response_model=BranchResponse)
async def update_branch(
    branch_id: int,
    data: BranchUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.store_id == member.store_id
        )
    )
    branch = result.scalar_one_or_none()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    if data.name is not None:
        # unique check
        existing = await db.execute(
            select(Branch).where(
                Branch.store_id == member.store_id,
                Branch.name == data.name,
                Branch.id != branch_id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Branch name already exists")
        branch.name = data.name

    if data.address is not None:
        branch.address = data.address
    if data.phone is not None:
        branch.phone = data.phone
    if data.is_main is not None:
        branch.is_main = data.is_main
    if data.is_active is not None:
        branch.is_active = data.is_active

    await db.commit()
    await db.refresh(branch)
    return BranchResponse.model_validate(branch)