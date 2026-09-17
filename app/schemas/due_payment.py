from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.all_models import DuePaymentMethod


class DuePaymentCreateRequest(BaseModel):
    sale_id: int
    amount: Decimal = Field(gt=0, decimal_places=2)
    method: DuePaymentMethod = DuePaymentMethod.cash
    note: Optional[str] = Field(default=None, max_length=255)


class DuePaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    sale_id: int
    customer_id: Optional[int]
    amount: Decimal
    method: DuePaymentMethod
    received_by: Optional[int]
    note: Optional[str]
    created_at: datetime


class SaleDueSnapshot(BaseModel):
    """Payment করার পরে sale-এর updated state — response-এ দেখানোর জন্য।"""
    model_config = ConfigDict(from_attributes=True)

    sale_id: int
    invoice_no: str
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal


class DuePaymentCreateResponse(BaseModel):
    payment: DuePaymentResponse
    sale: SaleDueSnapshot


# ---- Reports ----

class DueSaleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_no: str
    branch_id: int
    customer_id: Optional[int]
    customer_name: Optional[str]
    customer_phone: Optional[str]
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    created_at: datetime


class CustomerDueSummaryResponse(BaseModel):
    customer_id: int
    customer_name: str
    customer_phone: Optional[str]
    total_due: Decimal
    sales: list[DueSaleSummary]