# app/schemas/stock_release.py
"""
Stock Release = inventory write-off / adjustment voucher.

One voucher, one reason:
  * damage           -> stock OUT (broken / unsaleable)
  * expiry           -> stock OUT (expired / near-expiry write-off)
  * supplier_return  -> stock OUT (returned to supplier; optional purchase_item_id)
  * count_correction -> stock OUT (shortage) or IN (surplus)

Quantity on each line is always positive. Direction decides the sign:
  out -> quantity_change = -quantity
  in  -> quantity_change = +quantity  (count_correction only)
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.all_models import StockReleaseReason

ReleaseDirection = Literal["out", "in"]


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

class StockReleaseItemCreate(BaseModel):
    stock_batch_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1_000_000)
    direction: ReleaseDirection = "out"
    purchase_item_id: Optional[int] = Field(default=None, gt=0)
    notes: Optional[str] = Field(default=None, max_length=500)


class StockReleaseCreateRequest(BaseModel):
    branch_id: int = Field(gt=0)
    reason: StockReleaseReason
    released_date: Optional[date] = None
    reference_no: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=2000)
    items: list[StockReleaseItemCreate] = Field(min_length=1, max_length=200)

    @field_validator("released_date")
    @classmethod
    def _not_in_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError("released_date cannot be in the future")
        return v

    @field_validator("reference_no")
    @classmethod
    def _clean_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _reason_rules(self):
        batch_ids = [i.stock_batch_id for i in self.items]
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("Each stock_batch_id may appear only once on a release")

        if self.reason != StockReleaseReason.count_correction:
            if any(i.direction != "out" for i in self.items):
                raise ValueError(f"{self.reason.value} can only release stock (direction=out)")

        if self.reason != StockReleaseReason.supplier_return:
            if any(i.purchase_item_id is not None for i in self.items):
                raise ValueError("purchase_item_id is only valid when reason is supplier_return")
        else:
            purchase_ids = [i.purchase_item_id for i in self.items if i.purchase_item_id is not None]
            if len(purchase_ids) != len(set(purchase_ids)):
                raise ValueError("Each purchase_item_id may appear only once on a release")

        return self


# ---------------------------------------------------------------------------
# RESPONSE
# ---------------------------------------------------------------------------

class StockReleaseItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    stock_batch_id: int
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    quantity: int
    direction: ReleaseDirection
    quantity_change: int
    quantity_before: int
    quantity_after: int
    unit_price: Decimal
    line_value: Decimal
    purchase_id: Optional[int] = None
    purchase_item_id: Optional[int] = None
    stock_adjustment_id: Optional[int] = None
    notes: Optional[str] = None


class StockReleaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    branch_name: Optional[str] = None
    release_no: Optional[str] = None
    reason: StockReleaseReason
    released_date: date
    reference_no: Optional[str] = None
    notes: Optional[str] = None
    total_quantity_out: int
    total_quantity_in: int
    total_value: Decimal
    released_by: Optional[int] = None
    released_by_name: Optional[str] = None
    created_at: datetime
    items: list[StockReleaseItemResponse] = []


class StockReleaseListItemResponse(BaseModel):
    id: int
    branch_id: int
    branch_name: Optional[str] = None
    release_no: Optional[str] = None
    reason: StockReleaseReason
    released_date: date
    reference_no: Optional[str] = None
    total_quantity_out: int
    total_quantity_in: int
    total_value: Decimal
    item_count: int
    released_by_name: Optional[str] = None
    created_at: datetime


class PaginatedStockReleases(BaseModel):
    items: list[StockReleaseListItemResponse]
    total: int
    skip: int
    limit: int


class ReleasableBatchResponse(BaseModel):
    """Batches the UI can pick when creating a release."""
    stock_batch_id: int
    branch_id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    quantity: int
    purchase_price: Decimal
    sale_price: Decimal
    is_expired: bool
