# app/schemas/current_stock.py
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StockStatus(str, Enum):
    in_stock = "in_stock"
    low_stock = "low_stock"
    out_of_stock = "out_of_stock"


class BatchStatus(str, Enum):
    ok = "ok"
    expiring_soon = "expiring_soon"
    expired = "expired"


class StockItemOut(BaseModel):
    """One medicine with its stock aggregated across batches (in the current scope)."""

    medicine_id: int
    name: str
    generic_name: Optional[str] = None
    category: Optional[str] = None
    unit: str
    reorder_level: int
    is_active: bool

    total_quantity: int = Field(description="All units on hand, including expired batches")
    sellable_quantity: int = Field(description="Units on hand excluding expired batches")
    expired_quantity: int = Field(description="Units in batches past their expiry date")
    batch_count: int = Field(description="Number of batches with quantity > 0")

    purchase_value: Decimal = Field(description="SUM(quantity * purchase_price)")
    sale_value: Decimal = Field(description="SUM(quantity * sale_price)")
    nearest_expiry: Optional[date] = Field(
        default=None, description="Earliest expiry among non-expired batches"
    )
    status: StockStatus


class StockListOut(BaseModel):
    items: list[StockItemOut]
    total: int
    skip: int
    limit: int
    branch_id: Optional[int] = Field(
        default=None, description="Branch this result is scoped to (null = all branches)"
    )


class StockSummaryOut(BaseModel):
    branch_id: Optional[int] = None
    total_medicines: int
    total_quantity: int
    purchase_value: Decimal
    sale_value: Decimal
    low_stock_count: int
    out_of_stock_count: int
    expired_batch_count: int
    expiring_soon_batch_count: int
    expiry_alert_days: int


class StockBatchOut(BaseModel):
    batch_id: int
    branch_id: int
    batch_no: Optional[str] = None
    quantity: int
    purchase_price: Decimal
    sale_price: Decimal
    purchase_value: Decimal
    sale_value: Decimal
    expiry_date: Optional[date] = None
    days_to_expiry: Optional[int] = None
    status: BatchStatus
    received_at: datetime


class StockDetailOut(StockItemOut):
    """Medicine stock + batch-wise breakdown (FEFO order: earliest expiry first)."""

    batches: list[StockBatchOut]