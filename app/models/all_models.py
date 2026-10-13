# models.py
# SQLAlchemy models matching the provided PostgreSQL schema
# Multi-tenant Medical Shop SaaS

from datetime import date, datetime, timezone
from typing import Optional
from decimal import Decimal
from sqlalchemy import text

from sqlalchemy import (
    String, Text, Boolean, Integer, Numeric, Date, DateTime,
    ForeignKey, Index, UniqueConstraint, Enum as SAEnum, JSON, BigInteger
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, declarative_base
from app.core.database import Base
from sqlalchemy.dialects.postgresql import JSONB
from enum import Enum as PyEnum


# ---------------------------------------------------------------------------
# ENUMS
# ---------------------------------------------------------------------------

class StoreStatus(str, PyEnum):
    trial = "trial"
    active = "active"
    grace = "grace"
    suspended = "suspended"
    archived = "archived"


class SubscriptionStatus(str, PyEnum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    expired = "expired"


class PaymentStatus(str, PyEnum):
    pending = "pending"
    success = "success"
    failed = "failed"
    refunded = "refunded"


class StaffRole(str, PyEnum):
    owner = "owner"
    manager = "manager"
    pharmacist = "pharmacist"
    cashier = "cashier"
    staff = "staff"




class RequisitionStatus(str, PyEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    ordered = "ordered"
    completed = "completed"
    cancelled = "cancelled"


class StockAdjustmentReason(str, PyEnum):
    purchase_return = "purchase_return"
    damage = "damage"
    expiry = "expiry"
    count_correction = "count_correction"
    other = "other"


class StockReleaseReason(str, PyEnum):
    """Why stock is being written off / adjusted (one reason per voucher)."""
    damage = "damage"
    expiry = "expiry"
    supplier_return = "supplier_return"
    count_correction = "count_correction"


class StockReleaseDirection(str, PyEnum):
    """out = stock leaves the shelf; in = count correction surplus only."""
    out = "out"
    in_ = "in"




class TicketStatus(str, PyEnum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class AnnouncementTarget(str, PyEnum):
    all = "all"
    trial = "trial"
    active = "active"
    plan_specific = "plan_specific"


class StockTransferStatus(str, PyEnum):
    pending = "pending"
    in_transit = "in_transit"
    received = "received"
    canceled = "canceled"


class NotificationChannel(str, PyEnum):
    sms = "sms"
    whatsapp = "whatsapp"


class NotificationStatus(str, PyEnum):
    queued = "queued"
    sent = "sent"
    failed = "failed"


class AuditAction(str, PyEnum):
    create = "create"
    update = "update"
    delete = "delete"


class DuePaymentMethod(str, PyEnum):
    cash = "cash"
    bkash = "bkash"
    nagad = "nagad"
    bank = "bank"
    other = "other"

class BillingCycle(str, PyEnum):
    monthly = "monthly"
    yearly = "yearly"


# ---------------------------------------------------------------------------
# BASE
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# PUBLIC LAYER
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    profile_image: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    # Relationships
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    owned_stores: Mapped[list["Store"]] = relationship(back_populates="owner")
    store_memberships: Mapped[list["StoreMember"]] = relationship(back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    device_info: Mapped[Optional[str]] = mapped_column(String(255))
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), unique=True, nullable=False, index=True)
    address: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    status: Mapped[StoreStatus] = mapped_column(
        SAEnum(StoreStatus, name="store_status", create_type=False),
        nullable=False,
        default=StoreStatus.trial,
        index=True
    )
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # -------- Store settings (logo/invoice template/currency/timezone) --------
    # আগে এই field গুলো model-এ ছিলই না, তাই PUT /store/{id} দিয়ে সেট করা
    # সম্ভব ছিল না। এখন plain column হিসেবে যোগ করা হলো -- এগুলো
    # StoreUpdateRequest-এর মাধ্যমে অন্য profile field গুলোর মতোই
    # (setattr loop-এ) update হবে, আলাদা endpoint লাগবে না।
    logo_url: Mapped[Optional[str]] = mapped_column(String(500))
    invoice_template: Mapped[Optional[str]] = mapped_column(String(50))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BDT", server_default="BDT")
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Dhaka", server_default="Asia/Dhaka")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    owner: Mapped["User"] = relationship(back_populates="owned_stores")
    branches: Mapped[list["Branch"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    members: Mapped[list["StoreMember"]] = relationship(back_populates="store", cascade="all, delete-orphan")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="store")


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_store_branch_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    store: Mapped["Store"] = relationship(back_populates="branches")


# NOTE: এই জায়গায় আগে পুরনো (StaffRole enum ভিত্তিক) StoreMember class ছিলো।
# সেটা মুছে ফেলা হয়েছে — নতুন role_id-ভিত্তিক StoreMember class ফাইলের নিচে
# (Permission/Role/RolePermission এর পরে) আছে, ওটাই এখন একমাত্র সংজ্ঞা।


# ---------------------------------------------------------------------------
# SUPER ADMIN LAYER
# ---------------------------------------------------------------------------

class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_monthly: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    price_yearly: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    max_stores: Mapped[Optional[int]] = mapped_column(Integer)          # NULL = unlimited
    max_staff: Mapped[Optional[int]] = mapped_column(Integer)
    max_branches: Mapped[Optional[int]] = mapped_column(Integer, default=1)
    max_invoices_per_month: Mapped[Optional[int]] = mapped_column(Integer)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, name="subscription_status", create_type=False),
        nullable=False,
        default=SubscriptionStatus.trialing,
        index=True
    )

    start_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    current_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    grace_period_end: Mapped[Optional[date]] = mapped_column(Date)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    canceled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    store: Mapped["Store"] = relationship(back_populates="subscriptions")
    plan: Mapped["SubscriptionPlan"] = relationship(back_populates="subscriptions")
    payments: Mapped[list["Payment"]] = relationship(back_populates="subscription")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status", create_type=False),
        nullable=False,
        default=PaymentStatus.pending,
        index=True
    )
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        SAEnum(BillingCycle, name="billing_cycle", create_type=False),
        nullable=False,
        default=BillingCycle.monthly,
    )
    sender_number: Mapped[Optional[str]] = mapped_column(String(20))       # manual payment এ যে নাম্বার থেকে পাঠানো হয়েছে
    gateway_payment_id: Mapped[Optional[str]] = mapped_column(String(150), index=True)  # bKash paymentID / Nagad paymentRefId
    transaction_ref: Mapped[Optional[str]] = mapped_column(String(120))
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    subscription: Mapped["Subscription"] = relationship(back_populates="payments")


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status", create_type=False),
        nullable=False,
        default=TicketStatus.open,
        index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    replies: Mapped[list["SupportTicketReply"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="SupportTicketReply.created_at"
    )


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[AnnouncementTarget] = mapped_column(
        SAEnum(AnnouncementTarget, name="announcement_target", create_type=False),
        nullable=False,
        default=AnnouncementTarget.all
    )
    target_plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subscription_plans.id", ondelete="SET NULL"))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel", create_type=False),
        nullable=False
    )
    purpose: Mapped[Optional[str]] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status", create_type=False),
        nullable=False,
        default=NotificationStatus.queued,
        index=True
    )
    provider_ref: Mapped[Optional[str]] = mapped_column(String(150))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_store_table_record", "store_id", "table_name", "record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[AuditAction] = mapped_column(
        SAEnum(AuditAction, name="audit_action", create_type=False),
        nullable=False
    )
    table_name: Mapped[str] = mapped_column(String(100), nullable=False)
    record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    old_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    new_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()", index=True)




# ---------------------------------------------------------------------------
# TENANT LAYER
# ---------------------------------------------------------------------------

class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        Index("ix_suppliers_store_name", "store_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    address: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    payments: Mapped[list["SupplierPayment"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")


class Unit(Base):
    """
    Unit master — দুই ধরনের row থাকে:
      • store_id IS NULL  -> GLOBAL unit (Box, Strip, Pcs, Bottle...) — শুধু
        super admin seed/migration দিয়ে বসায়, কোনো store এগুলো edit/delete
        করতে পারবে না, সব store এগুলো দেখবে ও ব্যবহার করতে পারবে (read-only)।
      • store_id = <id>   -> সেই store-এর নিজস্ব CUSTOM unit — শুধু সে-ই
        দেখবে, এডিট/ডিলিট করতে পারবে, অন্য কোনো store এটা দেখতেও পাবে না।
    """
    __tablename__ = "units"
    __table_args__ = (
        # একই store-এর ভেতরে নাম duplicate না হয় (store এর নিজের custom units)
        UniqueConstraint("store_id", "name", name="uq_store_unit_name"),
        # store_id NULL হলে postgres UniqueConstraint-এ NULL গুলোকে distinct ধরে,
        # তাই global unit-এর নাম duplicate আটকাতে আলাদা partial unique index লাগবে
        Index("uq_global_unit_name", "name", unique=True, postgresql_where=text("store_id IS NULL")),
        Index("ix_units_store", "store_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)  # "Box", "Strip", "Pcs"
    short_code: Mapped[Optional[str]] = mapped_column(String(15))  # "box", "str", "pcs" — invoice/print এর জন্য
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    medicine_links: Mapped[list["MedicineUnit"]] = relationship(back_populates="unit")

    @property
    def is_global(self) -> bool:
        return self.store_id is None


class MedicineUnit(Base):
    """
    একটা medicine কোন কোন unit-এ purchase/sale করা যাবে, এবং প্রতিটা unit
    = কত base_unit (Medicine.base_unit_id) সেই conversion factor।
    Stock সবসময় base_unit-এ track হয় (StockBatch.quantity); purchase/sale
    time-এ user যে unit বাছবে, quantity_in_base = quantity * conversion_factor
    হিসেব করে stock আপডেট হবে।
    """
    __tablename__ = "medicine_units"
    __table_args__ = (
        UniqueConstraint("medicine_id", "unit_id", name="uq_medicine_unit"),
        Index("ix_medicine_units_medicine", "medicine_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"), nullable=False, index=True)

    # 1 এই unit = কত base_unit। base_unit নিজের জন্য conversion_factor সবসময় 1।
    conversion_factor: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=1)

    is_purchase_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_sale_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # optional per-unit price override (না দিলে base_unit price থেকে conversion_factor
    # দিয়ে calculate করে নেওয়া যায়)
    default_purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    default_sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    medicine: Mapped["Medicine"] = relationship(back_populates="units")
    unit: Mapped["Unit"] = relationship(back_populates="medicine_links")


class Medicine(Base):
    __tablename__ = "medicines"
    __table_args__ = (
        Index("ix_medicines_store_name", "store_id", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    generic_name: Mapped[Optional[str]] = mapped_column(String(200))
    category: Mapped[Optional[str]] = mapped_column(String(100))

    # base_unit = যে unit-এ stock internally track হয় (সাধারণত smallest unit, e.g. Pcs).
    # পুরনো String `unit` field এর জায়গায় এটা এখন FK — dropdown + conversion দুটোর জন্যই দরকার।
    base_unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)

    reorder_level: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    master_medicine_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("master_medicines.id", ondelete="SET NULL"), index=True
    )

    base_unit: Mapped["Unit"] = relationship(foreign_keys=[base_unit_id])
    units: Mapped[list["MedicineUnit"]] = relationship(back_populates="medicine", cascade="all, delete-orphan")

class MasterMedicine(Base):

    __tablename__ = "master_medicines"
    __table_args__ = (
        Index("ix_master_medicines_generic_name", "generic_name"),
        Index("ix_master_medicines_brand_name", "brand_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)  # API'r id
    sku: Mapped[Optional[str]] = mapped_column(String(100))
    generic_name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand_name: Mapped[str] = mapped_column(String(200), nullable=False)
    strength: Mapped[Optional[str]] = mapped_column(String(100))
    dosage_form: Mapped[Optional[str]] = mapped_column(String(100))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(200))
    reference_sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))  # API'r sale_price, sudhu suggestion hisebe
    reference_mrp: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")




class StockBatch(Base):
    __tablename__ = "stock_batches"
    __table_args__ = (
        Index("ix_stock_store_medicine", "store_id", "medicine_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_no: Mapped[Optional[str]] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # base-unit price: 6 decimal (Box 1000 / 30 = 33.333333) — total সবসময় money() দিয়ে 2 decimal-এ round হয়
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"
    __table_args__ = (
        Index("ix_stock_adj_store_branch", "store_id", "branch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    stock_batch_id: Mapped[int] = mapped_column(ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=False,
                                                index=True)

    reason: Mapped[StockAdjustmentReason] = mapped_column(
        SAEnum(StockAdjustmentReason, name="stock_adjustment_reason", create_type=False),
        nullable=False,
        index=True,
    )
    quantity_change: Mapped[int] = mapped_column(Integer, nullable=False)  # negative = decrease, positive = increase
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    purchase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"), index=True)
    purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id", ondelete="SET NULL"))

    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")



class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    from_branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True)
    to_branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[StockTransferStatus] = mapped_column(
        SAEnum(StockTransferStatus, name="stock_transfer_status", create_type=False),
        nullable=False,
        default=StockTransferStatus.pending,
        index=True
    )
    requested_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    received_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)


# ============================================================================
# all_models.py তে এই CHANGES গুলো apply করুন
# ============================================================================


# ---------------------------------------------------------------------------
# CHANGE 1: Purchase model-এ paid_amount, due_amount যোগ করুন
# (আপনার Sale model-এ যেভাবে paid_amount/due_amount আছে, একই pattern)
# ---------------------------------------------------------------------------
#
# বর্তমান Purchase model:
#
# class Purchase(Base):
#     __tablename__ = "purchases"
#
#     id: Mapped[int] = mapped_column(Integer, primary_key=True)
#     store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
#     branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
#     supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
#     invoice_no: Mapped[Optional[str]] = mapped_column(String(100))
#     total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
#     created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
#     created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
#
#     items: Mapped[list["PurchaseItem"]] = relationship(back_populates="purchase", cascade="all, delete-orphan")
#
# নিচের ভার্সন দিয়ে replace করুন:

class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("store_id", "idempotency_key", name="uq_store_purchase_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    invoice_no: Mapped[Optional[str]] = mapped_column(String(100))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    # --- NEW ---
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    due_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0, index=True)
    # --- END NEW ---

    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100))

    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    items: Mapped[list["PurchaseItem"]] = relationship(back_populates="purchase", cascade="all, delete-orphan")
    payments: Mapped[list["SupplierPayment"]] = relationship(back_populates="purchase")  # NEW


# ---------------------------------------------------------------------------
# CHANGE 2: Supplier model-এ payments relationship যোগ করুন (optional, but recommended)
# ---------------------------------------------------------------------------
#
# class Supplier(Base):
#     __tablename__ = "suppliers"
#     ...
#     created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
#
#     payments: Mapped[list["SupplierPayment"]] = relationship(back_populates="supplier", cascade="all, delete-orphan")  # NEW লাইন যোগ করুন


# ---------------------------------------------------------------------------
# CHANGE 3: নতুন SupplierPayment model যোগ করুন
# (Purchase model-এর ঠিক পরে বসান, DuePaymentMethod enum আগে থেকেই আছে বলে reuse করছি)
# ---------------------------------------------------------------------------

class SupplierPayment(Base):
    __tablename__ = "supplier_payments"
    __table_args__ = (
        Index("ix_supplier_payments_store_supplier", "store_id", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True)

    # purchase_id optional -> নির্দিষ্ট purchase-এর due clear করলে দেওয়া হবে,
    # না দিলে backend automatically একটা নতুন Purchase (invoice) বানিয়ে এখানে link করবে
    purchase_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("purchases.id", ondelete="SET NULL"), index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[DuePaymentMethod] = mapped_column(
        SAEnum(DuePaymentMethod, name="due_payment_method", create_type=False),
        nullable=False,
        default=DuePaymentMethod.cash,
    )
    reference_no: Mapped[Optional[str]] = mapped_column(String(120))  # bKash TrxID / cheque no / bank ref ইত্যাদি
    note: Mapped[Optional[str]] = mapped_column(String(255))
    paid_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    supplier: Mapped["Supplier"] = relationship(back_populates="payments")
    purchase: Mapped[Optional["Purchase"]] = relationship(back_populates="payments")


# ---------------------------------------------------------------------------
# CHANGE 4 (দরকার হলে): PurchaseItem ছাড়াই Purchase তৈরি করা যাচ্ছে কিনা check
# ---------------------------------------------------------------------------
# Auto-generated invoice (payment ছাড়া নতুন purchase) এ কোনো PurchaseItem থাকবে না —
# শুধু financial record হিসেবে থাকবে। PurchaseItem relationship nullable/optional
# থাকায় এটা সমস্যা করবে না, items: list[] খালি থাকবে।

class PurchaseItem(Base):
    __tablename__ = "purchase_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False, index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))

    # user যে unit-এ কিনেছে (e.g. Box) + সেই unit-এ raw quantity — invoice/receipt-এ এটাই দেখানো হয়
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    quantity_in_unit: Mapped[int] = mapped_column(Integer, nullable=False)

    # quantity = quantity_in_unit * MedicineUnit.conversion_factor — base_unit-এ normalize করা,
    # stock math (StockBatch.quantity ইত্যাদি) সবসময় এই কলাম দিয়েই হয়
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    purchase: Mapped["Purchase"] = relationship(back_populates="items")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_store_phone", "store_id", "phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    address: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("store_id", "invoice_no", name="uq_store_invoice_no"),
        UniqueConstraint("store_id", "idempotency_key", name="uq_store_sale_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_no: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(150))
    customer_phone: Mapped[Optional[str]] = mapped_column(String(20))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    due_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0, index=True)
    # Client-generated Idempotency-Key -> double submit / retry-তে duplicate sale হবে না
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100))
    sold_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()", index=True)

    items: Mapped[list["SaleItem"]] = relationship(back_populates="sale", cascade="all, delete-orphan")
    due_payments: Mapped[list["DuePayment"]] = relationship(back_populates="sale")


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    # External (stock-এ নেই, বাইরে থেকে কেনা) item-এ medicine_id/unit_id NULL — stock touch হয় না
    medicine_id: Mapped[Optional[int]] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=True)
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    external_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # user যে unit-এ বিক্রি করেছে (Box বা Pcs — যেটা medicine_units এ allowed) + raw quantity
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"), nullable=True)
    quantity_in_unit: Mapped[int] = mapped_column(Integer, nullable=False)

    # base_unit-এ normalize করা quantity — stock_batches থেকে এটাই deduct হয়
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    sale: Mapped["Sale"] = relationship(back_populates="items")


class SaleReturn(Base):
    """
    Customer return — একটা sale_item এর বিপরীতে (purchase return যেমন
    purchase_item এর বিপরীতে হয়, একই প্যাটার্ন)। মূল Sale/SaleItem রেকর্ড
    কখনো mutate হবে না — এটা একটা আলাদা linked correction record, invoice
    history অক্ষত থাকার জন্য। Reporting এ net sales বের করতে হলে এই টেবিল
    থেকে বিয়োগ করতে হবে।
    """
    __tablename__ = "sale_returns"
    __table_args__ = (
        Index("ix_sale_returns_store_branch", "store_id", "branch_id"),
        Index("ix_sale_returns_sale_item", "sale_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)

    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    sale_item_id: Mapped[int] = mapped_column(ForeignKey("sale_items.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)

    # যে batch এ stock ফেরত যাচ্ছে (restocked=True হলে required, False হলে NULL)
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)  # original sale unit_price
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    refund_method: Mapped[str] = mapped_column(String(20), nullable=False, default="cash")
    # refund = due_adjusted + cash_refunded (আগে শুধু notes-এ ছিল)
    due_adjusted: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cash_refunded: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    # True হলে stock_batch_id তে quantity যোগ হয়েছে (resalable); False হলে stock touch হয়নি
    # (নষ্ট/অবিক্রয়যোগ্য — চাইলে আলাদাভাবে POST /stock-releases দিয়ে damage হিসেবে লগ করা যায়)
    restocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


class DuePayment(Base):
    __tablename__ = "due_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[DuePaymentMethod] = mapped_column(
        SAEnum(DuePaymentMethod, name="due_payment_method", create_type=False),
        nullable=False,
        default=DuePaymentMethod.cash
    )
    received_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    sale: Mapped["Sale"] = relationship(back_populates="due_payments")





class SupportTicketReply(Base):
    __tablename__ = "support_ticket_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    is_admin_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    ticket: Mapped["SupportTicket"] = relationship(back_populates="replies")


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"
    __table_args__ = (
        UniqueConstraint("announcement_id", "store_member_id", name="uq_announcement_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False, index=True)
    store_member_id: Mapped[int] = mapped_column(ForeignKey("store_members.id", ondelete="CASCADE"), nullable=False, index=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")


# ---------------------------------------------------------------------------
# PURCHASE REQUISITION
# ---------------------------------------------------------------------------
#
# workflow: staff creates (pending) -> admin approve/reject -> ordered -> completed
# completed হওয়ার সময়ই আসল Purchase + PurchaseItem + StockBatch তৈরি হয়ে
# stock এ quantity যোগ হয় (এখানেই stock "clear"/receive হয়)।
#
# ⚠️ DB migration দরকার — নতুন enum type আগে বানিয়ে নিন:
#
#   CREATE TYPE requisition_status AS ENUM (
#       'pending', 'approved', 'rejected', 'ordered', 'completed', 'cancelled'
#   );
#
# তারপর purchase_requisitions ও purchase_requisition_items টেবিল migrate করুন।

class PurchaseRequisition(Base):
    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        Index("ix_requisitions_store_status", "store_id", "status"),
        Index("ix_requisitions_store_branch", "store_id", "branch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))

    requisition_no: Mapped[Optional[str]] = mapped_column(String(50))

    status: Mapped[RequisitionStatus] = mapped_column(
        SAEnum(RequisitionStatus, name="requisition_status", create_type=False),
        nullable=False,
        default=RequisitionStatus.pending,
        index=True,
    )

    notes: Mapped[Optional[str]] = mapped_column(Text)

    # --- requested (staff) ---
    # NOT NULL কলাম, তাই ondelete=RESTRICT — requisition থাকা অবস্থায় requesting user delete করা যাবে না
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    # --- approved / rejected (admin) ---
    approved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    rejected_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    # --- ordered (admin -> supplier) ---
    ordered_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    ordered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # --- completed (stock received) ---
    completed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # complete হওয়ার সময় যে আসল Purchase (invoice) তৈরি হয় সেটার লিংক
    purchase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"), index=True)

    # --- cancelled ---
    cancelled_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    items: Mapped[list["PurchaseRequisitionItem"]] = relationship(
        back_populates="requisition", cascade="all, delete-orphan"
    )


class PurchaseRequisitionItem(Base):
    __tablename__ = "purchase_requisition_items"
    __table_args__ = (
        Index("ix_requisition_items_requisition", "requisition_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requisition_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_requisitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)

    # কোন unit-এ (Box/Pcs) চাওয়া/receive হচ্ছে — quantity_requested, quantity_approved,
    # received_quantity, estimated_unit_price, unit_price, sale_price সব এই unit অনুযায়ী।
    # NULL (পুরনো row) মানে medicine.base_unit।
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"))

    quantity_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    # admin approve করার সময় চাইলে quantity adjust করতে পারবে (default = quantity_requested)
    quantity_approved: Mapped[Optional[int]] = mapped_column(Integer)

    # order দেওয়ার সময় আনুমানিক দাম (শুধু তথ্যের জন্য, চূড়ান্ত দাম complete এ বসবে)
    estimated_unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    # --- এগুলো complete (stock receive) করার সময় পূরণ হয় ---
    received_quantity: Mapped[Optional[int]] = mapped_column(Integer)
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    sale_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    batch_no: Mapped[Optional[str]] = mapped_column(String(100))
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)

    # complete হওয়ার সময় তৈরি হওয়া PurchaseItem এর সাথে লিংক
    purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id", ondelete="SET NULL"))

    notes: Mapped[Optional[str]] = mapped_column(Text)

    requisition: Mapped["PurchaseRequisition"] = relationship(back_populates="items")


class StockReceipt(Base):
    __tablename__ = "stock_receipts"
    __table_args__ = (
        UniqueConstraint("store_id", "receipt_no", name="uq_store_receipt_no"),
        UniqueConstraint("store_id", "idempotency_key", name="uq_store_receipt_idempotency"),
        Index("ix_receipts_store_branch_date", "store_id", "branch_id", "received_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)

    # Filled right after the first flush (id based) -> unique & race-safe
    receipt_no: Mapped[Optional[str]] = mapped_column(String(50))

    requisition_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("purchase_requisitions.id", ondelete="SET NULL"), index=True
    )
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"), index=True)
    purchase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"), index=True)

    supplier_challan_no: Mapped[Optional[str]] = mapped_column(String(100))  # supplier's own challan / invoice no
    received_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Client-generated key (Idempotency-Key header) -> safe retries on flaky mobile networks
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100))

    received_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    items: Mapped[list["StockReceiptItem"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="StockReceiptItem.id"
    )


class StockReceiptItem(Base):
    __tablename__ = "stock_receipt_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("stock_receipts.id", ondelete="CASCADE"), nullable=False,
                                            index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)

    requisition_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("purchase_requisition_items.id", ondelete="SET NULL"), index=True
    )
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))
    purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id", ondelete="SET NULL"))

    # যে unit-এ receive হয়েছে + সেই unit-এ raw quantity (NULL = পুরনো row, base unit ধরা হয়)
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"))
    quantity_in_unit: Mapped[Optional[int]] = mapped_column(Integer)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # base_unit-এ normalize করা
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    batch_no: Mapped[Optional[str]] = mapped_column(String(100))
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    receipt: Mapped["StockReceipt"] = relationship(back_populates="items")


class StockRelease(Base):
    """
    Stock write-off / adjustment voucher (damage, expiry, supplier return, count correction).
    Stock is applied in the same transaction as the voucher is created.
    """
    __tablename__ = "stock_releases"
    __table_args__ = (
        UniqueConstraint("store_id", "release_no", name="uq_store_release_no"),
        UniqueConstraint("store_id", "idempotency_key", name="uq_store_release_idempotency"),
        Index("ix_releases_store_branch_date", "store_id", "branch_id", "released_date"),
        Index("ix_releases_store_reason", "store_id", "reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)

    release_no: Mapped[Optional[str]] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(String(40), nullable=False)

    released_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    reference_no: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    total_quantity_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_quantity_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100))

    released_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    items: Mapped[list["StockReleaseItem"]] = relationship(
        back_populates="release", cascade="all, delete-orphan", order_by="StockReleaseItem.id"
    )


class StockReleaseItem(Base):
    __tablename__ = "stock_release_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("stock_releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    stock_batch_id: Mapped[int] = mapped_column(ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=False, index=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="out")
    quantity_change: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_before: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)

    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    batch_no: Mapped[Optional[str]] = mapped_column(String(100))
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)

    purchase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"), index=True)
    purchase_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("purchase_items.id", ondelete="SET NULL"))
    stock_adjustment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_adjustments.id", ondelete="SET NULL"))

    notes: Mapped[Optional[str]] = mapped_column(Text)

    release: Mapped["StockRelease"] = relationship(back_populates="items")


    """Permission"""

class Permission(Base):
    """
    Global, fixed catalog of module + action permissions.
    কোনো store এটা edit করতে পারবে না — শুধু seed script দিয়ে বসানো হবে।
    code ফরম্যাট: "<module>:<action>"  যেমন "medicines:create"
    """
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("module", "action", name="uq_permission_module_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    label: Mapped[Optional[str]] = mapped_column(String(150))  # UI তে দেখানোর জন্য, Bengali/English দুটোই রাখতে পারেন

    role_links: Mapped[list["RolePermission"]] = relationship(
        back_populates="permission", cascade="all, delete-orphan"
    )


class Role(Base):
    """
    Per-store custom role. Owner-এর জন্য প্রতিটা store creation এ একটা
    is_system=True, slug="owner" role auto-create হবে (সব permission সহ,
    delete/rename করা যাবে না)।
    """
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_store_role_name"),
        Index("ix_roles_store", "store_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(110), nullable=False)  # e.g. "owner", "senior-pharmacist"
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # True হলে delete/rename block
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    store: Mapped["Store"] = relationship()
    permission_links: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )
    members: Mapped[list["StoreMember"]] = relationship(back_populates="role")

    @property
    def permission_codes(self) -> set[str]:
        return {link.permission.code for link in self.permission_links}


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)

    role: Mapped["Role"] = relationship(back_populates="permission_links")
    permission: Mapped["Permission"] = relationship(back_populates="role_links", lazy="selectin")




class StoreMember(Base):
    __tablename__ = "store_members"
    __table_args__ = (
        UniqueConstraint("store_id", "user_id", name="uq_store_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # branch_id None মানে store-wide access (owner/manager-type role), নাহলে সেই
    # নির্দিষ্ট branch-এ bound staff — BaseRepository ইতিমধ্যে এটা respect করে।
    branch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)

    # per-member fine-tuning, role এর বাইরেও/বাদেও নির্দিষ্ট permission দেওয়া-নেওয়ার জন্য:
    # {"extra": ["sales:void"], "revoke": ["medicines:delete"]}
    permissions: Mapped[Optional[dict]] = mapped_column(JSONB)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")

    store: Mapped["Store"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="store_memberships")
    branch: Mapped[Optional["Branch"]] = relationship()
    role: Mapped["Role"] = relationship(back_populates="members", lazy="selectin")

    @property
    def permission_codes(self) -> list[str]:
        """
        Effective permission codes for this membership: role.permission_codes
        দিয়ে base, তারপর per-member override (self.permissions JSONB এ
        {"extra": [...], "revoke": [...]}) apply করে ফাইনাল list রিটার্ন করে।
        role.permission_links -> RolePermission.permission দুটোই lazy="selectin"
        হওয়ায় User -> store_memberships -> role লোড হলেই এটা এক্সট্রা কোনো
        query/await ছাড়াই ব্যবহার করা যায় (async-safe)।
        """
        codes = set(self.role.permission_codes) if self.role is not None else set()
        overrides = self.permissions or {}
        extra = set(overrides.get("extra") or [])
        revoke = set(overrides.get("revoke") or [])
        return sorted((codes | extra) - revoke)





# ============================================================================
# ADD TO app/models/all_models.py
# এই দুইটা class বসান SaleItem এর ঠিক পরে, SaleReturn এর আগে।
# আলাদা কোনো নতুন import লাগবে না — Base, ForeignKey, Mapped, mapped_column,
# Integer, String, Text, Numeric, Decimal, DateTime, datetime, Optional,
# Index, relationship — সবকিছু আগে থেকেই all_models.py তে import করা আছে।
# ============================================================================


class HoldSale(Base):
    """
    POS "Hold / Draft Bill" — কাস্টমার wait করলে বা cashier অন্য customer
    ধরার আগে চলতি বিলটা সাময়িকভাবে সরিয়ে রাখার জন্য। Sale.py এর create_sale
    এর মতো এখানে কোনো stock deduct হয় না — শুধু items মনে রাখা হয়, আসল
    stock allocation হয় resume করার সময়েই (FEFO, ঠিক sale checkout এর মতো)।
    তাই দুইজন cashier একই batch hold করে রাখলেও stock double-block হয় না।
    """
    __tablename__ = "hold_sales"
    __table_args__ = (
        Index("ix_hold_sales_store_branch", "store_id", "branch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), nullable=False, index=True)

    # cashier বিলটা সহজে চিনতে যেই label/note দেয় — না দিলে UI তে customer_name/phone
    # অথবা "Hold #<id>" দেখানো যাবে।
    label: Mapped[Optional[str]] = mapped_column(String(100))

    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), index=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(150))
    customer_phone: Mapped[Optional[str]] = mapped_column(String(20))
    discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    note: Mapped[Optional[str]] = mapped_column(Text)

    held_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()", index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()",
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # resume করে আসল Sale বানানোর পর এখানে সেই sale.id বসে যায় — hold রেকর্ডটা
    # তখন "active" থাকে না (list endpoint এটা বাদ দিয়ে দেখায়), কিন্তু audit
    # trail হিসেবে row-টা delete হয় না, resumed হওয়া holds ইতিহাস হিসেবে থেকে যায়।
    resumed_sale_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sales.id", ondelete="SET NULL"), index=True)

    items: Mapped[list["HoldSaleItem"]] = relationship(back_populates="hold_sale", cascade="all, delete-orphan")


class HoldSaleItem(Base):
    __tablename__ = "hold_sale_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hold_sale_id: Mapped[int] = mapped_column(ForeignKey("hold_sales.id", ondelete="CASCADE"), nullable=False, index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False)

    # hold অবস্থায় stock touch হয় না, তাই একটা নির্দিষ্ট batch pin করা optional —
    # cashier চাইলে নির্দিষ্ট batch বেছে রাখতে পারে, না দিলে resume এর সময়
    # ঠিক create_sale এর মতোই FEFO অনুযায়ী auto-allocate হবে।
    stock_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_batches.id", ondelete="SET NULL"))

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    # cashier price override না করলে NULL থাকে — resume এর সময় তখনকার
    # batch.sale_price ব্যবহার হবে (hold করে রাখার সময় price change হতে পারে,
    # তাই stale price জোর করে বসিয়ে রাখা হয় না)।
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))

    # কোন unit-এ hold করা (NULL = medicine.base_unit)
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("units.id", ondelete="RESTRICT"))

    hold_sale: Mapped["HoldSale"] = relationship(back_populates="items")