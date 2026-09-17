from datetime import datetime
from typing import Optional, List
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict
from app.models.all_models import StaffRole, StoreStatus
from app.models.all_models import TicketStatus, PaymentStatus, BillingCycle, AnnouncementTarget


# -------------------- Store --------------------
class StoreCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    slug: Optional[str] = Field(None, min_length=2, max_length=220)  # ← এই লাইন যোগ করা হয়েছে
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=20)


class StoreResponse(BaseModel):
    id: int
    name: str
    slug: str
    address: Optional[str] = None
    phone: Optional[str] = None
    status: StoreStatus
    trial_ends_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StoreUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=20)

# -------------------- Branch --------------------
class BranchCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=20)
    is_main: bool = False


class BranchUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=20)
    is_main: Optional[bool] = None
    is_active: Optional[bool] = None


class BranchResponse(BaseModel):
    id: int
    store_id: int
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main: bool
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# -------------------- Staff / StoreMember --------------------
class StaffCreateRequest(BaseModel):
    email: str
    full_name: str
    phone: str | None = None
    role: StaffRole = StaffRole.staff
    branch_id: int | None = None
    permissions: dict | None = None


class StaffUpdateRequest(BaseModel):
    role: Optional[StaffRole] = None
    branch_id: Optional[int] = None
    permissions: Optional[dict] = None
    is_active: Optional[bool] = None


class StaffResponse(BaseModel):
    id: int
    store_id: int
    user_id: int
    branch_id: Optional[int] = None
    role: StaffRole
    permissions: Optional[dict] = None
    is_active: bool
    created_at: datetime

    # nested user info
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)



# ---------- Support Tickets ----------

class TicketCreate(BaseModel):
    subject: str
    message: str


class TicketReplyCreate(BaseModel):
    message: str


class ReplyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sender_id: Optional[int]
    is_admin_reply: bool
    message: str
    created_at: datetime


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


# ---------- Payments ----------

class ManualPaymentCreate(BaseModel):
    subscription_id: int
    provider: str  # "bkash" | "nagad"
    sender_number: str
    transaction_ref: str  # trxID
    billing_cycle: BillingCycle = BillingCycle.monthly


class BkashInitiateRequest(BaseModel):
    subscription_id: int
    billing_cycle: BillingCycle = BillingCycle.monthly


class NagadInitiateRequest(BaseModel):
    subscription_id: int
    billing_cycle: BillingCycle = BillingCycle.monthly


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
    transaction_ref: Optional[str]
    paid_at: Optional[datetime]
    created_at: datetime


# ---------- Announcements ----------

class AnnouncementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    message: str
    target: AnnouncementTarget
    published_at: Optional[datetime]
    is_read: bool = False