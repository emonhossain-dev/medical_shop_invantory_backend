"""
app/schemas/stock_adjustment.py
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.all_models import StockAdjustmentReason


# ---------------------------------------------------------------------------
# Purchase Return (specific — POST /purchases/{purchase_id}/items/{item_id}/return)
# ---------------------------------------------------------------------------

class PurchaseReturnRequest(BaseModel):
    quantity: int = Field(gt=0)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# General Stock Adjustment (damage / expiry / count_correction / other)
# purchase_return এই endpoint দিয়ে করা যাবে না — উপরের নির্দিষ্ট endpoint ব্যবহার করতে হবে,
# কারণ সেটার জন্য purchase_id/purchase_item_id link লাগে ও validation আলাদা।
# ---------------------------------------------------------------------------

class StockAdjustmentCreateRequest(BaseModel):
    stock_batch_id: int
    reason: StockAdjustmentReason
    quantity_change: int = Field(description="ঋণাত্মক = কমানো (damage/expiry), ধনাত্মক = বাড়ানো (count_correction)")
    notes: Optional[str] = None

    def model_post_init(self, __context) -> None:
        if self.reason == StockAdjustmentReason.purchase_return:
            raise ValueError(
                "purchase_return এর জন্য /purchases/{purchase_id}/items/{item_id}/return endpoint ব্যবহার করো"
            )
        if self.quantity_change == 0:
            raise ValueError("quantity_change 0 হতে পারবে না")
        if self.reason != StockAdjustmentReason.count_correction and self.quantity_change > 0:
            raise ValueError(f"{self.reason.value} এ quantity_change ধনাত্মক হতে পারবে না")


class StockAdjustmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    branch_id: int
    medicine_id: int
    medicine_name: str
    stock_batch_id: int
    batch_no: Optional[str]
    reason: StockAdjustmentReason
    quantity_change: int
    unit_price: Optional[Decimal]
    purchase_id: Optional[int]
    purchase_item_id: Optional[int]
    notes: Optional[str]
    created_by: Optional[int]
    created_at: datetime
    batch_quantity_after: int  # response এ দেখানো, নতুন quantity কনফার্ম করার জন্য