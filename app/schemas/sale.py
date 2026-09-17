from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SaleItemCreateRequest(BaseModel):
    medicine_id: int
    quantity: int = Field(gt=0)
    # None => auto FEFO allocation across available batches at the branch
    stock_batch_id: Optional[int] = None
    # None => use the batch's sale_price (or weighted price for auto-split items)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)


class SaleCreateRequest(BaseModel):
    branch_id: int
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_amount: Decimal = Field(default=Decimal("0"), ge=0)
    invoice_no: Optional[str] = None  # None => auto-generated
    items: list[SaleItemCreateRequest]

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v):
        if not v:
            raise ValueError("At least one sale item is required")
        return v


class SaleItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    stock_batch_id: Optional[int]
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class SaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    invoice_no: str
    customer_id: Optional[int]
    customer_name: Optional[str]
    customer_phone: Optional[str]
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    sold_by: Optional[int]
    created_at: datetime
    items: list[SaleItemResponse] = []


class SaleListItemResponse(BaseModel):
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


class AvailableBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_no: Optional[str]
    quantity: int
    sale_price: Decimal
    expiry_date: Optional[date]