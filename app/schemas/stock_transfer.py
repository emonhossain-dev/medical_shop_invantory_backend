"""
app/schemas/stock_transfer.py
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.all_models import StockTransferStatus


class StockTransferCreateRequest(BaseModel):
    from_branch_id: int
    to_branch_id: int
    medicine_id: int
    stock_batch_id: int
    quantity: int = Field(gt=0)
    notes: Optional[str] = None


class StockTransferResponse(BaseModel):
    id: int
    store_id: int
    from_branch_id: int
    from_branch_name: Optional[str] = None
    to_branch_id: int
    to_branch_name: Optional[str] = None
    medicine_id: int
    medicine_name: Optional[str] = None
    stock_batch_id: Optional[int] = None
    batch_no: Optional[str] = None
    quantity: int
    status: StockTransferStatus
    requested_by: Optional[int] = None
    received_by: Optional[int] = None
    requested_at: datetime
    received_at: Optional[datetime] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True