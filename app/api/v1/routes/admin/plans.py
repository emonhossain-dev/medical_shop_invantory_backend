# app/api/admin/plans.py
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import require_super_admin
from app.models.all_models import SubscriptionPlan, User
from app.schemas.admin import PlanCreate, PlanUpdate, PlanOut

router = APIRouter(prefix="/admin/plans", tags=["Admin - Subscription Plans"])


@router.post("", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    plan = SubscriptionPlan(**payload.model_dump())
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.get("", response_model=List[PlanOut])
async def list_plans(
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = select(SubscriptionPlan)
    if not include_inactive:
        query = query.where(SubscriptionPlan.is_active == True)
    query = query.order_by(SubscriptionPlan.price_monthly.asc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.put("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: int,
    payload: PlanUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, key, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_plan(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    """
    Hard delete করা হয় না — subscriptions.plan_id এ FK RESTRICT আছে,
    কোনো store এই plan এ থাকলে delete করলে DB error দেবে। তাই
    soft-delete (is_active=False): নতুন subscription আর এই plan select
    করতে পারবে না, পুরনো subscription অক্ষত থাকে।
    """
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.is_active = False
    await db.commit()