# app/api/admin/payments.py
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import require_super_admin
from app.models.all_models import Payment, Subscription, User, PaymentStatus, SubscriptionStatus
from app.schemas.admin import PaymentCreate, PaymentUpdate, PaymentOut
from app.services.subscription_lifecycle import extend_subscription_period

router = APIRouter(prefix="/admin/payments", tags=["Admin - Payments"])


@router.post("", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    """
    Manual payment entry — cash/bank ইত্যাদি admin নিজে বসাচ্ছে।
    bKash/Nagad gateway callback দিয়ে auto আসলে সেটার জন্য আলাদা
    public webhook endpoint লাগবে (এখানে না — future scope)।
    """
    subscription = await db.get(Subscription, payload.subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    payment = Payment(**payload.model_dump())
    db.add(payment)

    # TODO: payment success হলে subscription এর period ঠিক কতদিন extend
    # হবে (monthly/yearly, plan অনুযায়ী) — এই business rule Phase 6
    # (subscription lifecycle automation) এ define হবে। আপাতত শুধু
    # status active করা হচ্ছে, date extend হচ্ছে না।
    if payment.status == PaymentStatus.success:
        subscription.status = SubscriptionStatus.active
        extend_subscription_period(subscription, payment.billing_cycle)
        if payment.paid_at is None:
            payment.paid_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(payment)
    return payment


@router.get("", response_model=List[PaymentOut])
async def list_payments(
    store_id: Optional[int] = Query(None),
    subscription_id: Optional[int] = Query(None),
    status_filter: Optional[PaymentStatus] = Query(None, alias="status"),
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = select(Payment)
    if store_id:
        query = query.where(Payment.store_id == store_id)
    if subscription_id:
        query = query.where(Payment.subscription_id == subscription_id)
    if status_filter:
        query = query.where(Payment.status == status_filter)
    query = query.order_by(Payment.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.put("/{payment_id}", response_model=PaymentOut)
async def update_payment(
    payment_id: int,
    payload: PaymentUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    was_success_before = payment.status == PaymentStatus.success

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(payment, key, value)

    if payment.status == PaymentStatus.success and not was_success_before:
        subscription = await db.get(Subscription, payment.subscription_id)
        if subscription:
            subscription.status = SubscriptionStatus.active
            extend_subscription_period(subscription, payment.billing_cycle)
        if payment.paid_at is None:
            payment.paid_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(payment)
    return payment