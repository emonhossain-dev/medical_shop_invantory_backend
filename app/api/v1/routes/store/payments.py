# app/api/store/payments.py
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import require_store_admin, get_current_store_member
from app.core import config
from app.models.all_models import (
    Payment, Subscription, SubscriptionPlan, StoreMember,
    PaymentStatus, SubscriptionStatus, BillingCycle,
)
from app.schemas.store import ManualPaymentCreate, PaymentOut, BkashInitiateRequest, NagadInitiateRequest
from app.services.payment_gateways.bkash import bkash_client
from app.services.payment_gateways.nagad import nagad_client
from app.services.subscription_lifecycle import extend_subscription_period

router = APIRouter(prefix="/store/payments", tags=["Store - Payments"])


def _billing_amount(plan: SubscriptionPlan, cycle: BillingCycle):
    return plan.price_yearly if cycle == BillingCycle.yearly else plan.price_monthly


def _get_own_subscription_or_404(sub: Subscription | None, member: StoreMember) -> Subscription:
    if not sub or sub.store_id != member.store_id:
        raise HTTPException(status_code=404, detail="Subscription not found for this store")
    return sub


# ---------- ১. Manual (personal bKash/Nagad number এ পাঠানো টাকা) ----------

@router.get("/manual/receive-numbers")
async def get_manual_receive_numbers(_member: StoreMember = Depends(get_current_store_member)):
    """Store owner কে দেখানোর জন্য — কোন নাম্বারে টাকা পাঠাতে হবে।"""
    return {"bkash": config.MANUAL_BKASH_NUMBER, "nagad": config.MANUAL_NAGAD_NUMBER}


@router.post("/manual", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
async def submit_manual_payment(
    payload: ManualPaymentCreate,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Store owner টাকা পাঠানোর পর যে নাম্বার থেকে পাঠিয়েছে + trxID জমা দেয়।
    এটা 'pending' এ থাকে — super admin ভেরিফাই করে status=success করলে
    (PUT /admin/payments/{id}) subscription active হবে ও period extend হবে।
    """
    subscription = _get_own_subscription_or_404(
        await db.get(Subscription, payload.subscription_id), member
    )
    plan = await db.get(SubscriptionPlan, subscription.plan_id)

    payment = Payment(
        subscription_id=subscription.id,
        store_id=member.store_id,
        amount=_billing_amount(plan, payload.billing_cycle),
        method=f"{payload.provider}_manual",
        status=PaymentStatus.pending,
        billing_cycle=payload.billing_cycle,
        sender_number=payload.sender_number,
        transaction_ref=payload.transaction_ref,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


# ---------- ২. bKash gateway (actual merchant account) ----------

@router.post("/bkash/initiate")
async def initiate_bkash_payment(
    payload: BkashInitiateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    subscription = _get_own_subscription_or_404(
        await db.get(Subscription, payload.subscription_id), member
    )
    plan = await db.get(SubscriptionPlan, subscription.plan_id)
    amount = _billing_amount(plan, payload.billing_cycle)

    payment = Payment(
        subscription_id=subscription.id, store_id=member.store_id,
        amount=amount, method="bkash_gateway",
        status=PaymentStatus.pending, billing_cycle=payload.billing_cycle,
    )
    db.add(payment)
    await db.flush()  # payment.id পাওয়ার জন্য

    gateway_resp = await bkash_client.create_payment(
        amount=str(amount), invoice_no=f"SUB-{subscription.id}-PAY-{payment.id}"
    )
    payment.gateway_payment_id = gateway_resp.get("paymentID")
    await db.commit()

    return {"bkashURL": gateway_resp.get("bkashURL"), "payment_id": payment.id}


@router.get("/bkash/callback")
async def bkash_callback(paymentID: str, status: str, db: AsyncSession = Depends(get_db)):
    """
    bKash checkout শেষে ইউজারের ব্রাউজারকে রিডাইরেক্ট করে এখানে —
    এই এন্ডপয়েন্ট public (auth লাগে না), paymentID দিয়েই payment খুঁজে বের হয়।
    """
    result = await db.execute(select(Payment).where(Payment.gateway_payment_id == paymentID))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if status != "success":
        payment.status = PaymentStatus.failed
        await db.commit()
        return {"status": "failed"}

    exec_result = await bkash_client.execute_payment(paymentID)
    if exec_result.get("transactionStatus") == "Completed":
        payment.status = PaymentStatus.success
        payment.transaction_ref = exec_result.get("trxID")
        payment.paid_at = datetime.now(timezone.utc)

        subscription = await db.get(Subscription, payment.subscription_id)
        subscription.status = SubscriptionStatus.active
        extend_subscription_period(subscription, payment.billing_cycle)

        await db.commit()
        return {"status": "success", "trxID": exec_result.get("trxID")}

    payment.status = PaymentStatus.failed
    await db.commit()
    return {"status": "failed"}


# ---------- ৩. Nagad gateway (actual merchant account) ----------

@router.post("/nagad/initiate")
async def initiate_nagad_payment(
    payload: NagadInitiateRequest,
    request: Request,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    subscription = _get_own_subscription_or_404(
        await db.get(Subscription, payload.subscription_id), member
    )
    plan = await db.get(SubscriptionPlan, subscription.plan_id)
    amount = _billing_amount(plan, payload.billing_cycle)

    payment = Payment(
        subscription_id=subscription.id, store_id=member.store_id,
        amount=amount, method="nagad_gateway",
        status=PaymentStatus.pending, billing_cycle=payload.billing_cycle,
    )
    db.add(payment)
    await db.flush()

    order_id = f"SUB-{subscription.id}-PAY-{payment.id}"
    client_ip = request.client.host if request.client else "0.0.0.0"

    init_resp = await nagad_client.initialize_payment(order_id, client_ip)
    payment.gateway_payment_id = init_resp.get("paymentReferenceId")
    await db.commit()

    complete_resp = await nagad_client.complete_payment(
        payment.gateway_payment_id, str(amount), order_id, client_ip
    )
    return {"callBackUrl": complete_resp.get("callBackUrl"), "payment_id": payment.id}


@router.get("/nagad/callback")
async def nagad_callback(payment_ref_id: str, status: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Payment).where(Payment.gateway_payment_id == payment_ref_id))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    verify = await nagad_client.verify_payment(payment_ref_id)
    if verify.get("status") == "Success":
        payment.status = PaymentStatus.success
        payment.transaction_ref = verify.get("issuerPaymentRefNo")
        payment.paid_at = datetime.now(timezone.utc)

        subscription = await db.get(Subscription, payment.subscription_id)
        subscription.status = SubscriptionStatus.active
        extend_subscription_period(subscription, payment.billing_cycle)

        await db.commit()
        return {"status": "success"}

    payment.status = PaymentStatus.failed
    await db.commit()
    return {"status": "failed"}


# ---------- নিজের সব payment history দেখা ----------

@router.get("", response_model=List[PaymentOut])
async def list_store_payments(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Payment).where(Payment.store_id == member.store_id).order_by(Payment.created_at.desc())
    )
    return result.scalars().all()