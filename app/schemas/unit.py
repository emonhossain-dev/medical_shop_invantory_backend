# app/schemas/unit.py
"""
Unit master (Box, Strip, Pcs...) + MedicineUnit (medicine <-> unit mapping,
conversion_factor, purchase/sale allow-flag) — এর জন্য schema।

Unit দুই রকম:
  • GLOBAL (store_id None)  -> super admin seed করে, read-only, সব store দেখে।
  • CUSTOM (store_id সেট)   -> সেই store নিজে বানায়, শুধু সে-ই এডিট করতে পারে।

একটা medicine একাধিক unit-এ (box/strip/pcs) purchase/sale করা যাবে —
MedicineUnit-এই সেই conversion + allow/disallow define হয়।
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Unit master (global + per-store custom)
# ---------------------------------------------------------------------------

class UnitCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)  # "Box", "Strip", "Pcs"
    short_code: Optional[str] = Field(None, max_length=15)  # invoice/print এর জন্য ছোট নাম


class UnitUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    short_code: Optional[str] = Field(None, max_length=15)
    is_active: Optional[bool] = None


class UnitResponse(BaseModel):
    id: int
    store_id: Optional[int] = None  # None => global unit
    name: str
    short_code: Optional[str] = None
    is_active: bool
    is_global: bool  # frontend এটা দেখে lock-icon / edit-button hide করবে
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# MedicineUnit (medicine-প্রতি কোন unit-এ কেনাবেচা করা যাবে + conversion)
# ---------------------------------------------------------------------------

class MedicineUnitCreateRequest(BaseModel):
    unit_id: int
    conversion_factor: Decimal = Field(
        default=Decimal("1"), gt=0, decimal_places=3,
        description="1 এই unit = কত base_unit (e.g. Box=100 হলে conversion_factor=100)",
    )
    is_purchase_allowed: bool = True
    is_sale_allowed: bool = True
    default_purchase_price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    default_sale_price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)


class MedicineUnitUpdateRequest(BaseModel):
    conversion_factor: Optional[Decimal] = Field(None, gt=0, decimal_places=3)
    is_purchase_allowed: Optional[bool] = None
    is_sale_allowed: Optional[bool] = None
    default_purchase_price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    default_sale_price: Optional[Decimal] = Field(None, ge=0, decimal_places=2)
    is_active: Optional[bool] = None


class MedicineUnitResponse(BaseModel):
    id: int
    medicine_id: int
    unit_id: int
    unit_name: Optional[str] = None
    unit_short_code: Optional[str] = None
    conversion_factor: Decimal
    is_purchase_allowed: bool
    is_sale_allowed: bool
    default_purchase_price: Optional[Decimal] = None
    default_sale_price: Optional[Decimal] = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}