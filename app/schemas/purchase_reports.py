# app/schemas/purchase_reports.py
#
# purchase.py এর নতুন REPORTS ও PAYMENTS endpoint গুলোর জন্য schema।
# আলাদা ফাইলে রাখা হয়েছে যাতে app/schemas/purchase.py এর মূল purchase schema গুলোর
# সাথে গুলিয়ে না যায় — চাইলে এই ক্লাসগুলো purchase.py স্কিমা ফাইলেও নিয়ে যেতে পারো,
# শুধু import path (app.schemas.purchase_reports) সেই অনুযায়ী বদলে নিও।

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.purchase import PurchaseListItemResponse


class PurchaseSummaryResponse(BaseModel):
    """/purchases/reports/summary এর response — কোনো নির্দিষ্ট সময়ে মোট purchase/paid/due।"""

    date_from: Optional[date] = None
    date_to: Optional[date] = None
    total_purchase_count: int
    total_amount: Decimal
    total_paid: Decimal
    total_due: Decimal


class SupplierPurchaseSummaryResponse(BaseModel):
    """/purchases/suppliers/{supplier_id}/summary এর response — supplier-wise full history + total।"""

    supplier_id: int
    supplier_name: Optional[str] = None
    total_purchase_count: int
    total_amount: Decimal
    total_paid: Decimal
    total_due: Decimal
    purchases: list[PurchaseListItemResponse]


class SupplierPaymentCreateRequest(BaseModel):
    """POST /purchases/{purchase_id}/payments এর body — বাকি due এর বিপরীতে নতুন payment।"""

    amount: Decimal = Field(..., gt=0, description="যত টাকা এখন পেমেন্ট করা হচ্ছে (0 এর বেশি হতে হবে)")
    method: Optional[str] = Field(default=None, description="cash | bkash | bank | card | other ইত্যাদি (DuePaymentMethod)")
    reference_no: Optional[str] = None
    note: Optional[str] = None


class SupplierPaymentResponse(BaseModel):
    """একটা payment এর response shape — /{purchase_id}/payments এবং /payments/history দুটোতেই ব্যবহার হয়।"""

    id: int
    store_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    purchase_id: Optional[int] = None
    invoice_no: Optional[str] = None
    amount: Decimal
    method: str
    reference_no: Optional[str] = None
    note: Optional[str] = None
    paid_by: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True