from typing import Optional
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupplierCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)


class SupplierUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_not_null(cls, v):
        # {"name": null} আগে exclude_unset এর পরেও থেকে যেত -> NOT NULL violation (500)
        if v is None:
            raise ValueError("name cannot be null")
        return v


class SupplierResponse(BaseModel):
    id: int
    store_id: int
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}