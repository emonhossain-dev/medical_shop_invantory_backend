from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date

class StockCreate(BaseModel):
    medicine_id: int
    quantity_in_stock: int
    expiry_date: Optional[date] = None
    received_date: Optional[date] = None

class StockUpdate(BaseModel):
    quantity_in_stock: Optional[int] = None
    quantity_reserved: Optional[int] = None
    quantity_available: Optional[int] = None
    expiry_date: Optional[date] = None

class StockResponse(BaseModel):
    id: int
    store_id: int
    medicine_id: int
    quantity_in_stock: int
    quantity_reserved: int
    quantity_available: int
    expiry_date: Optional[date]
    received_date: Optional[date]
    last_counted_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True




