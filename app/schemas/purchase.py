"""
app/schemas/purchase.py

Purchase create করলে সাথে সাথে PurchaseItem (multiple) + প্রতিটা item এর জন্য
নতুন StockBatch তৈরি হয় — সব এক transaction এ। তাই এখানে StockBatch এর
batch-related fields (batch_no, expiry_date, sale_price) input schema তেই
রাখা হয়েছে, আলাদা StockBatch-create endpoint লাগবে না এই ফ্লো তে।

Unit change: আগে ফাইলে PurchaseItemCreate/PurchaseCreateRequest দুইবার
define করা ছিল (দ্বিতীয়টা প্রথমটাকে override করত) — এখানে একটাতেই মার্জ
করে দেওয়া হয়েছে, সাথে unit_id/quantity_in_unit যোগ হয়েছে।
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.date_utils import dhaka_today


# ---------------------------------------------------------------------------
# CREATE (request)
# ---------------------------------------------------------------------------

class PurchaseItemCreate(BaseModel):
    # ঠিক একটা দিতে হবে:
    #   medicine_id        -> এই store এর নিজের Medicine.id
    #   master_medicine_id -> catalog (MasterMedicine.id); store এ না থাকলে auto-create হয়
    medicine_id: Optional[int] = None
    master_medicine_id: Optional[int] = None

    # medicine-টা যে unit-এ (Box/Strip/Pcs) কেনা হচ্ছে — MedicineUnit-এ
    # is_purchase_allowed=True থাকতে হবে
    unit_id: int

    # unit_id এই medicine-এর জন্য প্রথমবার ব্যবহার হলে দরকার — 1 unit_id = কত base unit।
    # না দিলে আর unit_id নতুন হলে, server 422 + code="unit_needs_conversion_factor" দেবে;
    # সেই মান নিয়ে এই field সহ আবার একই request পাঠাও।
    conversion_factor: Optional[Decimal] = Field(
        default=None, gt=0, max_digits=12, decimal_places=4
    )

    # উপরের unit_id অনুযায়ী raw quantity — backend conversion_factor দিয়ে
    # base_unit-এ normalize করে StockBatch/PurchaseItem.quantity-তে বসাবে
    quantity: int = Field(gt=0, le=1_000_000)

    unit_price: Decimal = Field(gt=0, decimal_places=2)  # ঐ unit_id-এর দাম (= StockBatch.purchase_price পার বেস-ইউনিট না, পার ইউনিট)
    sale_price: Decimal = Field(gt=0, decimal_places=2)

    batch_no: Optional[str] = Field(default=None, max_length=100)
    expiry_date: Optional[date] = None

    @field_validator("expiry_date")
    @classmethod
    def expiry_must_be_future(cls, v: Optional[date]) -> Optional[date]:
        # server timezone নয়, Asia/Dhaka এর আজ
        if v is not None and v <= dhaka_today():
            raise ValueError("expiry_date must be in the future")
        return v

    @model_validator(mode="after")
    def exactly_one_medicine_ref(self):
        if (self.medicine_id is None) == (self.master_medicine_id is None):
            raise ValueError("Send exactly one of medicine_id or master_medicine_id")
        return self


class PurchaseCreateRequest(BaseModel):
    branch_id: int
    supplier_id: Optional[int] = None
    # invoice_no ekhon backend theke auto-generate hoy — user ke dite hoy na
    items: List[PurchaseItemCreate] = Field(min_length=1, max_length=500)

    # --- payment (ঐচ্ছিক) ---
    paid_amount: Optional[Decimal] = Field(default=0, ge=0, max_digits=12, decimal_places=2, description="এই purchase এ কত টাকা পরিশোধ করছো")
    payment_method: Optional[str] = Field(default="cash", description="cash / bkash / nagad / bank / other")
    payment_reference: Optional[str] = None
    payment_note: Optional[str] = None


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

    unit_id: int
    unit_name: Optional[str] = None
    quantity_in_unit: int  # যে unit-এ কেনা হয়েছিল, সেই raw quantity (invoice-এ দেখানোর জন্য)

    quantity: int  # base_unit-এ normalize করা quantity — stock এই কলাম দিয়েই ট্র্যাক হয়
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
    paid_amount: Decimal = Decimal("0")
    due_amount: Decimal = Decimal("0")
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
    paid_amount: Decimal = Decimal("0")
    due_amount: Decimal = Decimal("0")
    item_count: int
    created_at: datetime
    pdf_url: Optional[str] = None