from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class SupplierCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = None


class SupplierUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = None


class SupplierResponse(BaseModel):
    id: int
    store_id: int
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}