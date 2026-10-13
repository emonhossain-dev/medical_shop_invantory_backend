# app/schemas/purchase_requisition.py

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.models.all_models import RequisitionStatus


# ---------------------------------------------------------------------------
# CREATE (staff -> requisition পাঠায়)
# ---------------------------------------------------------------------------

class RequisitionItemCreate(BaseModel):
    # ঠিক একটা দিতে হবে: medicine_id (এই store এর Medicine.id) অথবা
    # master_medicine_id (catalog; store এ না থাকলে auto-create)
    medicine_id: Optional[int] = None
    master_medicine_id: Optional[int] = None
    # কোন unit-এ (Box/Pcs) requisition চাওয়া হচ্ছে — None দিলে medicine-এর
    # base_unit_id ধরে নেওয়া হবে
    unit_id: Optional[int] = None

    # unit_id এই medicine-এর জন্য প্রথমবার ব্যবহার হলে দরকার — 1 unit_id = কত base unit।
    # না দিলে আর unit_id নতুন হলে, server 422 + code="unit_needs_conversion_factor" দেবে।
    conversion_factor: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=10, decimal_places=4
    )

    quantity_requested: int = Field(gt=0)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one_medicine_ref(self):
        if (self.medicine_id is None) == (self.master_medicine_id is None):
            raise ValueError("Send exactly one of medicine_id or master_medicine_id")
        return self


class RequisitionCreateRequest(BaseModel):
    branch_id: int
    supplier_id: Optional[int] = None  # চাইলে শুরুতেই সাজেস্ট করতে পারে, admin পরে বদলাতে পারবে
    notes: Optional[str] = None
    items: list[RequisitionItemCreate] = Field(min_length=1)


# ---------------------------------------------------------------------------
# APPROVE (admin)
# ---------------------------------------------------------------------------

class RequisitionItemAdjustment(BaseModel):
    item_id: int
    quantity_approved: int = Field(gt=0)


class RequisitionApproveRequest(BaseModel):
    # কোনো item এর quantity adjust করতে চাইলে এখানে দিন, না দিলে quantity_requested-ই approve হবে
    item_adjustments: Optional[list[RequisitionItemAdjustment]] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# REJECT (admin)
# ---------------------------------------------------------------------------

class RequisitionRejectRequest(BaseModel):
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# ORDER (admin -> supplier কে order দিলো)
# ---------------------------------------------------------------------------

class RequisitionItemPrice(BaseModel):
    item_id: int
    estimated_unit_price: Decimal = Field(gt=0)


class RequisitionOrderRequest(BaseModel):
    supplier_id: Optional[int] = None
    item_prices: Optional[list[RequisitionItemPrice]] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# COMPLETE (stock receive -> আসল Purchase + StockBatch তৈরি হবে)
# ---------------------------------------------------------------------------

class RequisitionCompleteItem(BaseModel):
    item_id: int
    # None দিলে requisition_item তৈরির সময়ের unit_id (বা তাও না থাকলে medicine.base_unit_id) ধরা হবে
    unit_id: Optional[int] = None

    # unit_id এই medicine-এর জন্য প্রথমবার ব্যবহার হলে দরকার — 1 unit_id = কত base unit।
    # না দিলে আর unit_id নতুন হলে, server 422 + code="unit_needs_conversion_factor" দেবে।
    conversion_factor: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=10, decimal_places=4
    )

    received_quantity: int = Field(gt=0)
    unit_price: Decimal = Field(gt=0)
    sale_price: Decimal = Field(gt=0)
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None


class RequisitionCompleteRequest(BaseModel):
    items: list[RequisitionCompleteItem] = Field(min_length=1)
    paid_amount: Optional[Decimal] = Field(default=None, ge=0)
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    payment_note: Optional[str] = None


# ---------------------------------------------------------------------------
# CANCEL (admin)
# ---------------------------------------------------------------------------

class RequisitionCancelRequest(BaseModel):
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# RESPONSES
# ---------------------------------------------------------------------------

class RequisitionItemResponse(BaseModel):
    id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    unit_id: Optional[int] = None
    unit_name: Optional[str] = None
    quantity_requested: int
    quantity_approved: Optional[int] = None
    estimated_unit_price: Optional[Decimal] = None
    received_quantity: Optional[int] = None
    unit_price: Optional[Decimal] = None
    sale_price: Optional[Decimal] = None
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class RequisitionResponse(BaseModel):
    id: int
    store_id: int
    branch_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    status: RequisitionStatus
    requisition_no: Optional[str] = None
    notes: Optional[str] = None

    requested_by: int
    requested_by_name: Optional[str] = None
    requested_at: datetime

    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None

    rejected_by: Optional[int] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None

    ordered_by: Optional[int] = None
    ordered_at: Optional[datetime] = None

    completed_by: Optional[int] = None
    completed_at: Optional[datetime] = None
    purchase_id: Optional[int] = None
    pdf_url: Optional[str] = None

    cancelled_by: Optional[int] = None
    cancelled_at: Optional[datetime] = None

    items: list[RequisitionItemResponse] = []

    class Config:
        from_attributes = True


class RequisitionListItemResponse(BaseModel):
    id: int
    branch_id: int
    status: RequisitionStatus
    requisition_no: Optional[str] = None
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    requested_by_name: Optional[str] = None
    item_count: int
    requested_at: datetime

    class Config:
        from_attributes = True