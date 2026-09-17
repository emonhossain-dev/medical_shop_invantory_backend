# app/schemas/admin.py
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field

from app.models.all_models import (
    SubscriptionStatus, PaymentStatus, TicketStatus, AnnouncementTarget,
)
from app.models.all_models import BillingCycle


# ---------- Subscription Plan ----------

class PlanCreate(BaseModel):
    name: str
    price_monthly: Decimal
    price_yearly: Optional[Decimal] = None
    max_stores: Optional[int] = None
    max_staff: Optional[int] = None
    max_branches: Optional[int] = 1
    max_invoices_per_month: Optional[int] = None
    features: dict = Field(default_factory=dict)
    is_active: bool = True


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    price_monthly: Optional[Decimal] = None
    price_yearly: Optional[Decimal] = None
    max_stores: Optional[int] = None
    max_staff: Optional[int] = None
    max_branches: Optional[int] = None
    max_invoices_per_month: Optional[int] = None
    features: Optional[dict] = None
    is_active: Optional[bool] = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    price_monthly: Decimal
    price_yearly: Optional[Decimal]
    max_stores: Optional[int]
    max_staff: Optional[int]
    max_branches: Optional[int]
    max_invoices_per_month: Optional[int]
    features: dict
    is_active: bool
    created_at: datetime


# ---------- Subscription ----------

class SubscriptionCreate(BaseModel):
    store_id: int
    plan_id: int
    status: SubscriptionStatus = SubscriptionStatus.trialing
    start_date: Optional[date] = None
    current_period_end: date
    grace_period_end: Optional[date] = None
    auto_renew: bool = True


class SubscriptionUpdate(BaseModel):
    plan_id: Optional[int] = None
    status: Optional[SubscriptionStatus] = None
    current_period_end: Optional[date] = None
    grace_period_end: Optional[date] = None
    auto_renew: Optional[bool] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_id: int
    plan_id: int
    status: SubscriptionStatus
    start_date: date
    current_period_end: date
    grace_period_end: Optional[date]
    auto_renew: bool
    canceled_at: Optional[datetime]
    created_at: datetime


# ---------- Payment ----------

class PaymentCreate(BaseModel):
    subscription_id: int
    store_id: int
    amount: Decimal
    method: str
    status: PaymentStatus = PaymentStatus.pending
    billing_cycle: BillingCycle = BillingCycle.monthly
    sender_number: Optional[str] = None
    transaction_ref: Optional[str] = None
    paid_at: Optional[datetime] = None


class PaymentUpdate(BaseModel):
    status: Optional[PaymentStatus] = None
    transaction_ref: Optional[str] = None
    paid_at: Optional[datetime] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subscription_id: int
    store_id: int
    amount: Decimal
    method: str
    status: PaymentStatus
    billing_cycle: BillingCycle
    sender_number: Optional[str]
    gateway_payment_id: Optional[str]
    transaction_ref: Optional[str]
    paid_at: Optional[datetime]
    created_at: datetime


# ---------- Support Ticket ----------

class TicketUpdate(BaseModel):
    status: TicketStatus

class TicketReplyCreate(BaseModel):
    message: str


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_id: int
    created_by: Optional[int]
    subject: str
    message: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime

class ReplyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sender_id: Optional[int]
    is_admin_reply: bool
    message: str
    created_at: datetime


# TicketOut replace করো এইটা দিয়ে (replies যোগ হলো)
class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_id: int
    created_by: Optional[int]
    subject: str
    message: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    replies: List[ReplyOut] = []


# ---------- Announcement ----------

class AnnouncementCreate(BaseModel):
    title: str
    message: str
    target: AnnouncementTarget = AnnouncementTarget.all
    target_plan_id: Optional[int] = None


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    target: Optional[AnnouncementTarget] = None
    target_plan_id: Optional[int] = None


class AnnouncementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    message: str
    target: AnnouncementTarget
    target_plan_id: Optional[int]
    published_at: Optional[datetime]
    created_at: datetime