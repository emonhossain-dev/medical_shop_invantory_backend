"""
Multi-tenant Medical Shop SaaS - Database Models
Stack: FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL (asyncpg)

Multi-tenancy strategy: Shared DB + store_id column (row-level isolation).
Every tenant-scoped table carries `store_id` with an index, and application-level
repositories/dependencies MUST always filter by the current user's store_id.
(See note at bottom of audit.py-adjacent modules for optional PostgreSQL Row-Level
Security setup — reproduced below.)

Primary keys: plain auto-incrementing INTEGER (SERIAL) — simple, fast joins,
smaller indexes than UUID. Use invoice_no / slug fields for public-facing IDs.

This package used to be a single models.py file. It's now split by domain:
    base.py          -> declarative Base
    enums.py         -> all enums
    auth.py          -> User, RefreshToken
    tenancy.py       -> Store, StoreMember
    billing.py       -> SubscriptionPlan, Subscription, Payment
    support.py       -> SupportTicket, Announcement
    branches.py      -> Branch, StockTransfer
    notifications.py -> NotificationLog
    audit.py         -> AuditLog
    inventory.py     -> Supplier, Medicine, StockBatch, Purchase, PurchaseItem
    sales.py         -> Customer, Sale, SaleItem, DuePayment

Everything is re-exported here, so existing code that does
`from models import User, Sale, ...` keeps working unchanged.
"""

from __future__ import annotations

from app.core.database import Base

from .all_models import (
    StoreStatus,
    SubscriptionStatus,
    PaymentStatus,
    StaffRole,
    TicketStatus,
    AnnouncementTarget,
    StockTransferStatus,
    NotificationChannel,
    NotificationStatus,
    AuditAction,
    DuePaymentMethod,
)

from .all_models import User, RefreshToken
from .all_models import Store, StoreMember
from .all_models import SubscriptionPlan, Subscription, Payment
from .all_models import SupportTicket, Announcement
from .all_models import Branch, StockTransfer
from .all_models import NotificationLog
from .all_models import AuditLog
from .all_models import Supplier, Medicine, StockBatch, Purchase, PurchaseItem
from .all_models import Customer, Sale, SaleItem, DuePayment

__all__ = [
    "Base",
    # enums
    "StoreStatus",
    "SubscriptionStatus",
    "PaymentStatus",
    "StaffRole",
    "TicketStatus",
    "AnnouncementTarget",
    "StockTransferStatus",
    "NotificationChannel",
    "NotificationStatus",
    "AuditAction",
    "DuePaymentMethod",
    # auth
    "User",
    "RefreshToken",
    # tenancy
    "Store",
    "StoreMember",
    # billing
    "SubscriptionPlan",
    "Subscription",
    "Payment",
    # support
    "SupportTicket",
    "Announcement",
    # branches
    "Branch",
    "StockTransfer",
    # notifications
    "NotificationLog",
    # audit
    "AuditLog",
    # inventory
    "Supplier",
    "Medicine",
    "StockBatch",
    "Purchase",
    "PurchaseItem",
    # sales
    "Customer",
    "Sale",
    "SaleItem",
    "DuePayment",
]

# ---------------------------------------------------------------------------
# OPTIONAL — PostgreSQL Row-Level Security (extra safety net, add later)
# ---------------------------------------------------------------------------
# ALTER TABLE medicines ENABLE ROW LEVEL SECURITY;
# CREATE POLICY store_isolation ON medicines
#   USING (store_id = current_setting('app.current_store_id')::int);
#
# In FastAPI, set this per-request after auth:
#   await session.execute(text("SET app.current_store_id = :sid"), {"sid": store_id})
# ---------------------------------------------------------------------------
