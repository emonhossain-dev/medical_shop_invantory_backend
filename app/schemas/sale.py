from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SaleItemCreateRequest(BaseModel):
    # stock-এর ওষুধে বাধ্যতামূলক; is_external=true হলে None থাকবে
    medicine_id: Optional[int] = None

    # true => ওষুধটা stock-এ নেই (বাইরে থেকে কেনা): stock কাটা হয় না, শুধু record থাকে।
    # এ ক্ষেত্রে external_name ও unit_price (>0) লাগবে।
    is_external: bool = False
    external_name: Optional[str] = Field(default=None, max_length=255)

    # এই medicine-টা যে unit-এ (Box/Strip/Pcs) বিক্রি হচ্ছে — MedicineUnit-এ
    # is_sale_allowed=True থাকতে হবে। external-এ ignore করা হয়।
    unit_id: Optional[int] = None

    # unit_id অনুযায়ী raw quantity — backend conversion_factor দিয়ে base_unit-এ
    # normalize করে StockBatch থেকে deduct করবে
    quantity: int = Field(gt=0, le=1_000_000)

    # None => auto FEFO allocation across available batches at the branch
    stock_batch_id: Optional[int] = None
    # None => backend resolve করবে (MedicineUnit.default_sale_price থাকলে সেটা,
    # নাহলে batch.sale_price × conversion_factor)
    # 2 decimal-এর বেশি দিলে line_total (raw price × qty) আর DB-তে রাখা unit_price (2 decimal) আলাদা হয়ে যেত
    unit_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=10, decimal_places=2)

    @model_validator(mode="after")
    def check_external(self):
        if self.is_external:
            if not (self.external_name or "").strip():
                raise ValueError("external_name is required for external items")
            if self.unit_price is None or self.unit_price <= 0:
                raise ValueError("unit_price (> 0) is required for external items")
            # external line কখনো stock/batch/medicine-এর সাথে জুড়বে না
            self.medicine_id = None
            self.stock_batch_id = None
            self.unit_id = None
        else:
            if self.medicine_id is None:
                raise ValueError("medicine_id is required for non-external items")
        return self


class SaleCreateRequest(BaseModel):
    branch_id: int
    customer_id: Optional[int] = None
    customer_name: Optional[str] = Field(default=None, max_length=150)
    customer_phone: Optional[str] = Field(default=None, max_length=20)
    # discount_type="amount" (default) হলে discount সরাসরি টাকা (যেমন 50 = ৫০ টাকা ছাড়)।
    # discount_type="percent" হলে discount কে 0-100 এর মধ্যে শতাংশ হিসেবে ধরা হয়
    # (যেমন 10 = subtotal-এর ১০% ছাড়) — backend সেটাকে টাকায় কনভার্ট করে নেয়।
    discount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    discount_type: Literal["amount", "percent"] = "amount"
    paid_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    invoice_no: Optional[str] = Field(default=None, max_length=50)  # None => auto-generated
    items: list[SaleItemCreateRequest] = Field(max_length=200)

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v):
        if not v:
            raise ValueError("At least one sale item is required")
        return v


class SaleItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: Optional[int] = None  # external item-এ None
    is_external: bool = False
    external_name: Optional[str] = None
    stock_batch_id: Optional[int]

    unit_id: Optional[int] = None  # external item-এ None
    unit_name: Optional[str] = None
    quantity_in_unit: int  # যে unit-এ বিক্রি হয়েছিল, রেcords/invoice-এ এটাই দেখানো হয়

    quantity: int  # base_unit-এ normalize করা quantity (internal stock deduction এই কলাম দিয়ে)
    unit_price: Decimal
    line_total: Decimal


class SaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    invoice_no: str
    customer_id: Optional[int]
    customer_name: Optional[str]
    customer_phone: Optional[str]
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    sold_by: Optional[int]
    created_at: datetime
    items: list[SaleItemResponse] = []


class SaleListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_no: str
    branch_id: int
    customer_id: Optional[int]
    customer_name: Optional[str]
    customer_phone: Optional[str]
    total: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    created_at: datetime


class AvailableBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_no: Optional[str]
    quantity: int  # সবসময় base_unit-এ (StockBatch.quantity)
    sale_price: Decimal
    expiry_date: Optional[date]