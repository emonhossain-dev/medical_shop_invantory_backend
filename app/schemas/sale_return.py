

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SaleReturnCreateRequest(BaseModel):
    quantity: int = Field(gt=0)
    restock: bool = Field(
        default=True,
        description="True (default) হলে medicine ভালো ধরে নিয়ে stock এ ফেরত যোগ হবে। "
        "নষ্ট/অবিক্রয়যোগ্য হলে False দাও — তখন stock touch হবে না, refund তারপরও হবে।",
    )
    stock_batch_id: Optional[int] = Field(
        default=None,
        description="কোন batch এ ফেরত যাবে তা override করার জন্য — না দিলে যেই batch থেকে "
        "বিক্রি হয়েছিল সেটাই ব্যবহার হবে (sale_item.stock_batch_id)। restock=False হলে দরকার নেই।",
    )
    notes: Optional[str] = None


class SaleReturnResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sale_id: int
    sale_item_id: int
    medicine_id: int
    medicine_name: str
    stock_batch_id: Optional[int]
    quantity: int
    unit_price: Decimal
    refund_amount: Decimal
    refund_method: str
    restocked: bool
    notes: Optional[str]
    created_by: Optional[int]
    created_at: datetime
    batch_quantity_after: Optional[int]  # restocked=True হলেই মান থাকবে