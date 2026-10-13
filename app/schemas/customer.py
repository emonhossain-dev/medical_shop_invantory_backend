from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Basic Customer CRUD schemas (unchanged)
# ---------------------------------------------------------------------------

class CustomerCreateRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerResponse(BaseModel):
    id: int
    store_id: int
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Customer Due schemas
# ---------------------------------------------------------------------------

class CustomerDueItem(BaseModel):
    """সব customer এর due list এ প্রতিটা row (group by customer)।"""
    customer_id: int
    name: str
    phone: Optional[str] = None
    total_sale_amount: Decimal
    total_paid_amount: Decimal
    total_due_amount: Decimal
    last_payment_date: Optional[datetime] = None
    oldest_due_date: Optional[datetime] = None
    aging_days: Optional[int] = None  # সবচেয়ে পুরনো unpaid sale কতদিন আগের

    class Config:
        from_attributes = True


class CustomerDueListResponse(BaseModel):
    items: list[CustomerDueItem]
    total: int
    skip: int
    limit: int


class CustomerTotalDueResponse(BaseModel):
    """সব customer মিলিয়ে এক combined total due — এক call এ।"""
    total_due: Decimal
    customers_with_due: int


# ---------------------------------------------------------------------------
# Individual customer full detail schemas
# ---------------------------------------------------------------------------

class SaleSummary(BaseModel):
    id: int
    invoice_no: str
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    created_at: datetime

    class Config:
        from_attributes = True


class DuePaymentSummary(BaseModel):
    id: int
    amount: Decimal
    method: str
    note: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CustomerFullDetailResponse(BaseModel):
    """একজন customer এর সম্পূর্ণ প্রোফাইল: sale history + due + payment history।"""
    customer: CustomerResponse
    total_sale_amount: Decimal
    total_paid_amount: Decimal
    total_due_amount: Decimal
    total_sale_count: int
    oldest_due_date: Optional[datetime] = None
    aging_days: Optional[int] = None
    last_payment_date: Optional[datetime] = None
    recent_sales: list[SaleSummary]
    recent_due_payments: list[DuePaymentSummary]