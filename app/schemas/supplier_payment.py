# app/schemas/supplier_payment.py

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator

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