"""
app/schemas/purchase.py

Purchase create করলে সাথে সাথে PurchaseItem (multiple) + প্রতিটা item এর জন্য
নতুন StockBatch তৈরি হয় — সব এক transaction এ। তাই এখানে StockBatch এর
batch-related fields (batch_no, expiry_date, sale_price) input schema তেই
রাখা হয়েছে, আলাদা StockBatch-create endpoint লাগবে না এই ফ্লো তে।
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# CREATE (request)
# ---------------------------------------------------------------------------

class PurchaseItemCreate(BaseModel):
    medicine_id: int
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(gt=0, decimal_places=2)  # = StockBatch.purchase_price

    # নতুন StockBatch এর জন্য প্রয়োজনীয় fields
    sale_price: Decimal = Field(gt=0, decimal_places=2)
    batch_no: Optional[str] = Field(default=None, max_length=100)
    expiry_date: Optional[date] = None

    @field_validator("expiry_date")
    @classmethod
    def expiry_must_be_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v <= date.today():
            raise ValueError("expiry_date must be in the future")
        return v


class PurchaseCreateRequest(BaseModel):
    branch_id: int
    supplier_id: Optional[int] = None
    # invoice_no ekhon backend theke auto-generate hoy — user ke dite hoy na
    items: list[PurchaseItemCreate] = Field(min_length=1)


# ---------------------------------------------------------------------------
# RESPONSE
# ---------------------------------------------------------------------------

class PurchaseItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    medicine_name: str
    stock_batch_id: Optional[int]
    batch_no: Optional[str]
    expiry_date: Optional[date]
    quantity: int
    unit_price: Decimal
    sale_price: Optional[Decimal] = None
    line_total: Decimal


class PurchaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    supplier_id: Optional[int]
    supplier_name: Optional[str] = None
    invoice_no: Optional[str]
    total_amount: Decimal
    created_by: Optional[int]
    created_at: datetime
    items: list[PurchaseItemResponse] = []
    pdf_url: Optional[str] = None


class PurchaseListItemResponse(BaseModel):
    """List view — items বাদ, শুধু summary (heavy joins এড়ানোর জন্য)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_id: int
    supplier_id: Optional[int]
    supplier_name: Optional[str] = None
    invoice_no: Optional[str]
    total_amount: Decimal
    item_count: int
    created_at: datetime
    pdf_url: Optional[str] = None



class PurchaseItemCreate(BaseModel):
    medicine_id: int
    quantity: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., ge=0)
    sale_price: Decimal = Field(..., ge=0)
    batch_no: Optional[str] = None
    expiry_date: Optional[date] = None


class PurchaseCreateRequest(BaseModel):
    branch_id: int
    supplier_id: Optional[int] = None
    items: List[PurchaseItemCreate]

    # --- নতুন ফিল্ড (ঐচ্ছিক) ---
    paid_amount: Optional[Decimal] = Field(default=0, ge=0, description="এই purchase এ কত টাকা পরিশোধ করছো")
    payment_method: Optional[str] = Field(default="cash", description="cash / bkash / nagad / bank / other")
    payment_reference: Optional[str] = None
    payment_note: Optional[str] = None