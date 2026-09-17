# app/api/admin/subscriptions.py
from datetime import date, datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import require_super_admin
from app.models.all_models import (
    Subscription, SubscriptionPlan, Store, User,
    SubscriptionStatus, StoreStatus,
)
from app.schemas.admin import SubscriptionCreate, SubscriptionUpdate, SubscriptionOut
from app.services.subscription_lifecycle import run_lifecycle_check, STATUS_SYNC_MAP as _STATUS_SYNC_MAP

router = APIRouter(prefix="/admin/subscriptions", tags=["Admin - Subscriptions"])

# Subscription.status বদলালে Store.status ও sync রাখার mapping
# (Phase 6 এ scheduled job আসলে এটাই central জায়গায় থাকবে, আপাতত এখানে)
_STATUS_SYNC_MAP = {
    SubscriptionStatus.trialing: StoreStatus.trial,
    SubscriptionStatus.active: StoreStatus.active,
    SubscriptionStatus.past_due: StoreStatus.grace,
    SubscriptionStatus.canceled: StoreStatus.archived,
    SubscriptionStatus.expired: StoreStatus.suspended,
}


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    store = await db.get(Store, payload.store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    plan = await db.get(SubscriptionPlan, payload.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(status_code=400, detail="Plan not found or inactive")

    data = payload.model_dump()
    if data.get("start_date") is None:
        data["start_date"] = date.today()

    subscription = Subscription(**data)
    db.add(subscription)

    mapped_status = _STATUS_SYNC_MAP.get(subscription.status)
    if mapped_status:
        store.status = mapped_status

    await db.commit()
    await db.refresh(subscription)
    return subscription


@router.get("", response_model=List[SubscriptionOut])
async def list_subscriptions(
    status_filter: Optional[SubscriptionStatus] = Query(None, alias="status"),
    store_id: Optional[int] = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = select(Subscription)
    if status_filter:
        query = query.where(Subscription.status == status_filter)
    if store_id:
        query = query.where(Subscription.store_id == store_id)
    query = query.order_by(Subscription.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription(
    subscription_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    sub = await db.get(Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@router.put("/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    sub = await db.get(Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "plan_id" in update_data:
        plan = await db.get(SubscriptionPlan, update_data["plan_id"])
        if not plan or not plan.is_active:
            raise HTTPException(status_code=400, detail="Plan not found or inactive")

    for key, value in update_data.items():
        setattr(sub, key, value)

    if "status" in update_data:
        store = await db.get(Store, sub.store_id)
        mapped_status = _STATUS_SYNC_MAP.get(sub.status)
        if store and mapped_status:
            store.status = mapped_status

    await db.commit()
    await db.refresh(sub)
    return sub


@router.post("/{subscription_id}/cancel", response_model=SubscriptionOut)
async def cancel_subscription(
    subscription_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    sub = await db.get(Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.status = SubscriptionStatus.canceled
    sub.canceled_at = datetime.now(timezone.utc)
    sub.auto_renew = False

    store = await db.get(Store, sub.store_id)
    if store:
        store.status = StoreStatus.archived

    await db.commit()
    await db.refresh(sub)
    return sub



# এই নতুন endpoint যোগ করো (router এর নিচে যেকোনো জায়গায়)
@router.post("/run-lifecycle-check")
async def trigger_lifecycle_check(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    """
    Manual trigger — testing বা emergency এ scheduled job এর জন্য
    অপেক্ষা না করে এখনই lifecycle check চালাতে চাইলে এটা কল করো।
    """
    result = await run_lifecycle_check(db)
    return result