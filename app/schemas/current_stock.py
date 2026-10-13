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

    # আগের `unit: str` এর বদলে এখন base_unit reference — stock সবসময় এই
    # unit-এই (quantity) দেখানো হয়, কারণ StockBatch.quantity base_unit-এ থাকে
    base_unit_id: int
    base_unit_name: Optional[str] = None

    reorder_level: int
    is_active: bool

    total_quantity: int = Field(description="All units on hand, including expired batches (base_unit-এ)")
    sellable_quantity: int = Field(description="Units on hand excluding expired batches (base_unit-এ)")
    expired_quantity: int = Field(description="Units in batches past their expiry date (base_unit-এ)")
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



class StockAlertItemOut(StockItemOut):
    """StockItemOut + কতটুকু ঘাটতি আছে সেটা।"""

    alert_level: int = Field(description="এই medicine এর জন্য যে level ব্যবহার হয়েছে (reorder_level অথবা ?threshold)")
    shortage: int = Field(description="alert_level এর কত নিচে আছে (already ঠিক থাকলে 0)")


class StockAlertListOut(BaseModel):
    items: list[StockAlertItemOut]
    total: int
    skip: int
    limit: int
    branch_id: Optional[int] = None
    threshold: Optional[int] = Field(
        default=None, description="সেট থাকলে বোঝাবে flat override ব্যবহার হয়েছে, না হলে যার যার reorder_level"
    )
