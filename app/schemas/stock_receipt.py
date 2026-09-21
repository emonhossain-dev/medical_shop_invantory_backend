# app/schemas/stock_receipt.py
"""
Schemas for the Stock Receiving (GRN) module.

Two ways to receive stock:
  1. Against an ORDERED requisition  -> send `requisition_id` and, per line, `requisition_item_id`.
                                        Partial deliveries are supported (call again for the rest).
  2. Direct (no requisition)          -> send `branch_id` and, per line, `medicine_id`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.all_models import DuePaymentMethod, RequisitionStatus


# ---------------------------------------------------------------------------
# CREATE (request)
# ---------------------------------------------------------------------------

class StockReceiptItemCreate(BaseModel):
    requisition_item_id: Optional[int] = Field(default=None, gt=0)
    medicine_id: Optional[int] = Field(default=None, gt=0)          # medicines.id (store's own medicine)
    master_medicine_id: Optional[int] = Field(default=None, gt=0)   # master_medicines.id (auto-added to the store)

    quantity: int = Field(gt=0, le=1_000_000)
    unit_price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)  # ge=0 -> bonus / free goods allowed
    sale_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    batch_no: Optional[str] = Field(default=None, max_length=100)
    expiry_date: Optional[date] = None

    @field_validator("batch_no")
    @classmethod
    def _clean_batch_no(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("expiry_date")
    @classmethod
    def _expiry_must_be_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v <= date.today():
            raise ValueError("expiry_date must be in the future (expired stock cannot be received)")
        return v

    @model_validator(mode="after")
    def _needs_a_reference(self):
        if self.medicine_id is not None and self.master_medicine_id is not None:
            raise ValueError("Send either medicine_id or master_medicine_id, not both")
        if (
            self.requisition_item_id is None
            and self.medicine_id is None
            and self.master_medicine_id is None
        ):
            raise ValueError("requisition_item_id, medicine_id or master_medicine_id is required")
        return self


class StockReceiptCreateRequest(BaseModel):
    requisition_id: Optional[int] = Field(default=None, gt=0)
    branch_id: Optional[int] = Field(default=None, gt=0)  # required only when there is no requisition
    supplier_id: Optional[int] = Field(default=None, gt=0)

    supplier_challan_no: Optional[str] = Field(default=None, max_length=100)
    received_date: Optional[date] = None  # defaults to today
    notes: Optional[str] = Field(default=None, max_length=2000)

    items: list[StockReceiptItemCreate] = Field(min_length=1, max_length=500)

    # Requisition only: close the requisition even if some items are still short
    # (supplier can't deliver the rest). Remaining quantity is simply not received.
    close_requisition: bool = False

    # Optional payment made at the time of receiving
    paid_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    payment_method: DuePaymentMethod = DuePaymentMethod.cash
    payment_reference: Optional[str] = Field(default=None, max_length=120)
    payment_note: Optional[str] = Field(default=None, max_length=255)

    @field_validator("received_date")
    @classmethod
    def _not_in_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError("received_date cannot be in the future")
        return v

    @model_validator(mode="after")
    def _validate_mode(self):
        if self.requisition_id is None:
            if self.branch_id is None:
                raise ValueError("branch_id is required when receiving without a requisition")
            if any(i.medicine_id is None and i.master_medicine_id is None for i in self.items):
                raise ValueError(
                    "medicine_id or master_medicine_id is required on every item "
                    "when receiving without a requisition"
                )
            if self.close_requisition:
                raise ValueError("close_requisition is only valid together with requisition_id")
        else:
            if any(i.requisition_item_id is None for i in self.items):
                raise ValueError("requisition_item_id is required on every item when receiving against a requisition")
            if any(i.master_medicine_id is not None for i in self.items):
                raise ValueError("master_medicine_id cannot be used when receiving against a requisition")
        return self


# ---------------------------------------------------------------------------
# RESPONSE
# ---------------------------------------------------------------------------

class StockReceiptItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    requisition_item_id: Optional[int] = None
    stock_batch_id: Optional[int] = None
    purchase_item_id: Optional[int] = None
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    quantity: int
    unit_price: Decimal
    sale_price: Decimal
    line_total: Decimal


class StockReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    receipt_no: Optional[str] = None

    requisition_id: Optional[int] = None
    requisition_no: Optional[str] = None
    requisition_status: Optional[RequisitionStatus] = None  # tells the client if it is fully received/closed

    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    supplier_challan_no: Optional[str] = None

    purchase_id: Optional[int] = None
    invoice_no: Optional[str] = None
    total_amount: Decimal
    paid_amount: Decimal = Decimal("0")
    due_amount: Decimal = Decimal("0")

    received_date: date
    notes: Optional[str] = None
    received_by: Optional[int] = None
    received_by_name: Optional[str] = None
    created_at: datetime

    items: list[StockReceiptItemResponse] = []


class StockReceiptListItemResponse(BaseModel):
    id: int
    branch_id: int
    receipt_no: Optional[str] = None
    requisition_id: Optional[int] = None
    requisition_no: Optional[str] = None
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    supplier_challan_no: Optional[str] = None
    received_date: date
    total_amount: Decimal
    item_count: int
    received_by_name: Optional[str] = None
    created_at: datetime


class PaginatedStockReceipts(BaseModel):
    items: list[StockReceiptListItemResponse]
    total: int
    skip: int
    limit: int


# ---------------------------------------------------------------------------
# OUTSTANDING (what is still expected for an ordered requisition)
# ---------------------------------------------------------------------------

class OutstandingItemResponse(BaseModel):
    requisition_item_id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    quantity_ordered: int      # approved qty (falls back to requested qty)
    quantity_received: int
    quantity_outstanding: int
    estimated_unit_price: Optional[Decimal] = None


class RequisitionOutstandingResponse(BaseModel):
    requisition_id: int
    requisition_no: Optional[str] = None
    status: RequisitionStatus
    can_receive: bool          # True only while status == ordered
    branch_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    items: list[OutstandingItemResponse]


# ---------------------------------------------------------------------------
# MEDICINE SEARCH (store medicines + master medicines in one list)
# ---------------------------------------------------------------------------

class MedicineSearchResult(BaseModel):
    """
    source == "store"  -> send `medicine_id` when receiving
    source == "master" -> send `master_medicine_id` when receiving
                          (the backend adds it to the store's medicines automatically)
    """
    source: Literal["store", "master"]
    medicine_id: Optional[int] = None
    master_medicine_id: Optional[int] = None
    name: str
    generic_name: Optional[str] = None
    strength: Optional[str] = None
    dosage_form: Optional[str] = None
    manufacturer: Optional[str] = None
    unit: Optional[str] = None
    reference_sale_price: Optional[Decimal] = None  # master only: handy to pre-fill sale_price