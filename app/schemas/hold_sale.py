from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class HoldSaleItemCreate(BaseModel):
    medicine_id: int
    # কোন unit এ (Box/Strip/Pcs) বিক্রি -- None হলে medicine এর base unit।
    # quantity ও unit_price ঐ unit এর হিসাবে। (HoldSaleItem model এ nullable unit_id FK লাগবে)
    unit_id: Optional[int] = None
    quantity: int = Field(gt=0)
    stock_batch_id: Optional[int] = None
    # না দিলে resume করার সময় তখনকার batch/medicine price বসবে
    unit_price: Optional[Decimal] = Field(default=None, ge=0)


class HoldSaleCreateRequest(BaseModel):
    branch_id: int
    label: Optional[str] = Field(default=None, max_length=100)
    customer_id: Optional[int] = None
    customer_name: Optional[str] = Field(default=None, max_length=150)
    customer_phone: Optional[str] = Field(default=None, max_length=20)
    discount: Decimal = Field(default=Decimal("0"), ge=0)
    note: Optional[str] = None
    items: list[HoldSaleItemCreate] = Field(min_length=1)


class HoldSaleUpdateRequest(BaseModel):
    """
    সব field optional — cashier bill hold-এ রেখে item add/remove, discount
    বদলানো, customer বসানো ইত্যাদি করতে পারবে। `items` দিলে পুরনো item list
    পুরোপুরি replace হয়ে যাবে (partial patch না, পুরো নতুন list পাঠাতে হবে)।
    """
    label: Optional[str] = Field(default=None, max_length=100)
    customer_id: Optional[int] = None
    customer_name: Optional[str] = Field(default=None, max_length=150)
    customer_phone: Optional[str] = Field(default=None, max_length=20)
    discount: Optional[Decimal] = Field(default=None, ge=0)
    note: Optional[str] = None
    items: Optional[list[HoldSaleItemCreate]] = Field(default=None, min_length=1)


class ResumeHoldSaleRequest(BaseModel):
    """Hold বিলটা checkout করে আসল Sale বানানোর সময় যা লাগে — বাকি সব hold থেকেই আসে।"""
    invoice_no: Optional[str] = None
    paid_amount: Decimal = Field(default=Decimal("0"), ge=0)
    # ঐচ্ছিক: checkout এর সময় discount বদলানো / percent দেওয়া। না দিলে hold.discount (টাকা) ধরা হয়।
    discount: Optional[Decimal] = Field(default=None, ge=0)
    discount_type: Literal["amount", "percent"] = "amount"


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class HoldSaleItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    medicine_id: int
    medicine_name: Optional[str] = None
    stock_batch_id: Optional[int] = None
    unit_id: Optional[int] = None
    unit_name: Optional[str] = None
    quantity: int  # unit_id এর unit এ
    unit_price: Optional[Decimal] = None
    line_total: Optional[Decimal] = None  # unit_price থাকলেই শুধু হিসাব করে বসানো হয়


class HoldSaleListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_id: int
    label: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    discount: Decimal
    item_count: int
    estimated_total: Decimal
    held_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class HoldSaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    branch_id: int
    label: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    discount: Decimal
    note: Optional[str] = None
    held_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    resumed_sale_id: Optional[int] = None
    items: list[HoldSaleItemResponse]
    estimated_subtotal: Decimal
    estimated_total: Decimal