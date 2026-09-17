from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class MedicineCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    generic_name: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = Field(None, max_length=100)
    unit: str = Field("pcs", max_length=30)
    reorder_level: int = Field(10, ge=0)


class MedicineUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    generic_name: Optional[str] = Field(None, max_length=200)
    category: Optional[str] = Field(None, max_length=100)
    unit: Optional[str] = Field(None, max_length=30)
    reorder_level: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None


class MedicineResponse(BaseModel):
    id: int
    name: str
    generic_name: Optional[str] = None
    category: Optional[str] = None
    unit: str
    reorder_level: int
    is_active: bool
    master_medicine_id: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Unified search: ekbar-e master_medicines + medicines duita table-i check hoy
# ---------------------------------------------------------------------------

class MedicineSearchResultItem(BaseModel):
    source: str  # "master" -> master_medicines theke, "store" -> ei store-er nijer medicines theke
    id: int  # source="master" hole master_medicines.id, source="store" hole medicines.id
    name: str  # master hole brand_name, store hole medicine.name
    generic_name: Optional[str] = None

    # শুধু source="master" result-এ থাকবে
    strength: Optional[str] = None
    dosage_form: Optional[str] = None
    manufacturer: Optional[str] = None
    already_in_store: bool = False  # ei master medicine ta global medicines table e already add kora kina (store-specific na)

    # শুধু source="store" result-এ থাকবে
    category: Optional[str] = None
    unit: Optional[str] = None
    master_medicine_id: Optional[int] = None

    model_config = {"from_attributes": True}


class MedicineSearchResponse(BaseModel):
    query: str
    found: bool
    results: list[MedicineSearchResultItem] = []


class MedicineFromMasterRequest(BaseModel):
    """
    Master medicine theke store-er medicines table e add korar somoy
    optional override — na dile master-er default value ba schema default use hobe.
    """
    category: Optional[str] = Field(None, max_length=100)
    unit: Optional[str] = Field(None, max_length=30)
    reorder_level: Optional[int] = Field(None, ge=0)