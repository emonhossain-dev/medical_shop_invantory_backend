from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict
from app.models.all_models import DuePaymentMethod



class SupplierPaymentCreateRequest(BaseModel):
    purchase_id: Optional[int] = Field(
        None,
        description="নির্দিষ্ট purchase-এর due clear করতে চাইলে দিন। ফাঁকা রাখলে automatically নতুন purchase/invoice তৈরি হবে।",
    )
    branch_id: Optional[int] = Field(
        None,
        description="purchase_id না দিলে (নতুন invoice হবে) কোন branch-এর নামে হবে সেটা দিন। না দিলে আপনার account-এর default branch ব্যবহার হবে।",
    )
    amount: Decimal = Field(..., gt=0, description="Payment amount, অবশ্যই 0 এর বেশি")
    method: DuePaymentMethod = DuePaymentMethod.cash
    reference_no: Optional[str] = Field(None, max_length=120, description="bKash TrxID / cheque no / bank ref ইত্যাদি")
    note: Optional[str] = Field(None, max_length=255)

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Amount অবশ্যই 0 এর বেশি হতে হবে")
        return v


class SupplierPaymentResponse(BaseModel):
    id: int
    store_id: int
    supplier_id: int
    purchase_id: Optional[int]
    amount: Decimal
    method: DuePaymentMethod
    reference_no: Optional[str]
    note: Optional[str]
    paid_by: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class SupplierPaymentWithInvoiceResponse(SupplierPaymentResponse):
    """Payment response + সাথে যে invoice (Purchase)-এর সাথে link হলো তার তথ্য।"""
    invoice_no: Optional[str] = None
    purchase_total_amount: Optional[Decimal] = None
    purchase_paid_amount: Optional[Decimal] = None
    purchase_due_amount: Optional[Decimal] = None
    pdf_url: Optional[str] = Field(None, description="Generated invoice PDF-এর URL — সরাসরি browser-এ ক্লিক করে দেখা যাবে")


class SupplierInvoiceResponse(BaseModel):
    """একটা নির্দিষ্ট invoice (Purchase)-এর পুরো detail — GET /suppliers/{id}/invoices/{purchase_id} এর জন্য"""
    purchase_id: int
    invoice_no: Optional[str]
    supplier_id: int
    supplier_name: str
    total_amount: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    created_at: datetime
    payments: list[SupplierPaymentResponse]

    model_config = {"from_attributes": True}


class SupplierPaymentListItem(SupplierPaymentWithInvoiceResponse):
    """সব supplier-এর payment একসাথে list করার জন্য — কোন supplier-কে payment করা হয়েছে সেটাও দেখাবে"""
    supplier_name: str


class SupplierLedgerSummary(BaseModel):
    supplier_id: int
    supplier_name: str
    total_purchase_amount: Decimal
    total_paid_amount: Decimal
    total_due_amount: Decimal


class SupplierPaymentUpdateRequest(BaseModel):
    amount: Optional[Decimal] = Field(None, gt=0, description="নতুন payment amount")
    method: Optional[DuePaymentMethod] = None
    reference_no: Optional[str] = None
    note: Optional[str] = None






class SupplierDueItem(BaseModel):
    """একটা supplier এর due summary — /suppliers/due লিস্টের প্রতিটা row"""
    model_config = ConfigDict(from_attributes=True)

    supplier_id: int
    supplier_name: str
    phone: Optional[str] = None

    total_purchase_amount: Decimal
    total_paid_amount: Decimal
    total_due_amount: Decimal

    # সবচেয়ে পুরনো unpaid purchase কবে হয়েছিল, আর তার থেকে আজ পর্যন্ত কত দিন
    oldest_due_date: Optional[datetime] = None
    oldest_due_days: Optional[int] = None


class SupplierDueListResponse(BaseModel):
    """GET /suppliers/due এর পুরো response — list + সাথে quick total"""
    total_due_amount: Decimal
    supplier_count: int
    items: list[SupplierDueItem]


class SupplierDueSummary(BaseModel):
    """GET /suppliers/due/summary — সব supplier মিলিয়ে এক নম্বর"""
    total_suppliers: int
    suppliers_with_due: int
    total_purchase_amount: Decimal
    total_paid_amount: Decimal
    total_due_amount: Decimal
    generated_at: datetime


class SupplierDueAgingItem(BaseModel):
    """GET /suppliers/due/aging এর প্রতিটা row — supplier-wise aging bucket"""
    model_config = ConfigDict(from_attributes=True)

    supplier_id: int
    supplier_name: str
    phone: Optional[str] = None

    current_0_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_90_plus: Decimal
    total_due_amount: Decimal

    oldest_due_days: Optional[int] = None


class SupplierDueAgingResponse(BaseModel):
    """GET /suppliers/due/aging এর পুরো response"""
    as_of: datetime
    total_due_amount: Decimal
    items: list[SupplierDueAgingItem]


class SupplierLedgerEntry(BaseModel):
    """Ledger-er ekta line: purchase, purchase-er somoy dewa taka, ba alada payment"""
    transaction_date: datetime
    type: Literal["purchase", "initial_payment", "payment"]
    invoice_no: Optional[str] = None
    purchase_id: Optional[int] = None
    payment_id: Optional[int] = None
    method: Optional[DuePaymentMethod] = None
    reference_no: Optional[str] = None
    note: Optional[str] = None
    purchase_amount: Decimal = Decimal("0")   # purchase hole due barbe
    paid_amount: Decimal = Decimal("0")       # payment hole due kombe
    balance: Decimal                          # ei entry-r por supplier-er kache total due (negative = advance)


class SupplierLedgerHistoryResponse(BaseModel):
    """GET /suppliers/{supplier_id}/ledger/history"""
    supplier_id: int
    supplier_name: str
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    opening_balance: Decimal
    total_purchase_amount: Decimal
    total_paid_amount: Decimal
    closing_balance: Decimal
    total_entries: int
    entries: list[SupplierLedgerEntry]