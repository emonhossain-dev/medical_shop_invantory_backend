from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CustomerCreateRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class CustomerResponse(BaseModel):
    id: int
    store_id: int
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True