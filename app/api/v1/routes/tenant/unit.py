# app/api/v1/routes/tenant/unit.py
"""
Unit master (Box / Strip / Pcs ...) এবং medicine-প্রতি unit + conversion factor ম্যানেজমেন্ট।

Unit দুই রকম:
  • GLOBAL  (store_id IS NULL) -> super admin seed করে দেয়, সব store দেখতে/ব্যবহার
    করতে পারবে কিন্তু edit/delete করতে পারবে না।
  • CUSTOM  (store_id = <store's id>) -> সেই store নিজে বানায়, শুধু সে-ই দেখবে এবং
    এডিট/ডিলিট করতে পারবে, অন্য কোনো store-কে affect করবে না।

  POST   /units                                  -> নতুন CUSTOM unit (নিজের store-এর জন্য)
  GET    /units                                  -> global + নিজের custom unit list (dropdown)
  PATCH  /units/{unit_id}                        -> নাম/short_code/active বদল (শুধু নিজের CUSTOM unit)

  GET    /medicines/{medicine_id}/units          -> medicine-এর সব unit + conversion
  POST   /medicines/{medicine_id}/units          -> medicine-এ নতুন unit (e.g. Box=100) যোগ
  PATCH  /medicines/{medicine_id}/units/{unit_id}-> conversion / allow-flag / price বদল
"""

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
from app.models.all_models import Medicine, MedicineUnit, StoreMember, Unit
from app.schemas.unit import (
    MedicineUnitCreateRequest,
    MedicineUnitResponse,
    MedicineUnitUpdateRequest,
    UnitCreateRequest,
    UnitResponse,
    UnitUpdateRequest,
)
from app.services.unit_service import get_active_unit_or_400

router = APIRouter(prefix="/units", tags=["Units"])
medicine_units_router = APIRouter(prefix="/medicines/{medicine_id}/units", tags=["Medicine Units"])


# ---------------------------------------------------------------------------
# Unit master (global + per-store custom)
# ---------------------------------------------------------------------------

async def _name_taken(db: AsyncSession, store_id: int, name: str, exclude_id: Optional[int] = None) -> bool:
    """
    নাম clash চেক করে global unit + এই store-এর নিজের custom unit — দুই জায়গাতেই,
    যাতে একই নামের unit store নিজে বানিয়ে global unit-কে shadow করতে না পারে।
    """
    stmt = select(Unit.id).where(
        or_(Unit.store_id.is_(None), Unit.store_id == store_id),
        func.lower(Unit.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Unit.id != exclude_id)
    return (await db.scalar(stmt)) is not None


async def _get_own_unit_or_404(db: AsyncSession, unit_id: int, store_id: int) -> Unit:
    """
    শুধু নিজের CUSTOM unit ফেরত দেয়। global unit বা অন্য store-এর unit হলে ৪০৪/৪০৩।
    """
    unit = await db.scalar(select(Unit).where(Unit.id == unit_id))
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    if unit.store_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Global unit is read-only and cannot be modified")
    if unit.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    return unit


@router.post("", response_model=UnitResponse, status_code=status.HTTP_201_CREATED)
async def create_unit(
    data: UnitCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """store-এর নিজস্ব CUSTOM unit বানায়। global unit এখান থেকে তৈরি হয় না।"""
    if await _name_taken(db, member.store_id, data.name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Unit '{data.name}' already exists")

    unit = Unit(
        store_id=member.store_id,
        name=data.name.strip(),
        short_code=data.short_code,
        is_active=True,
    )
    db.add(unit)
    await db.commit()
    await db.refresh(unit)
    return UnitResponse.model_validate(unit)


@router.delete("/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_unit(
    unit_id: int,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    শুধু নিজের CUSTOM unit ডিলিট করা যায় (global unit ৪০৩, অন্য store-এর/না-থাকা unit ৪০৪)।
    কোনো medicine এই unit ব্যবহার করলে (MedicineUnit.unit_id FK ondelete=RESTRICT)
    ডিলিট আটকে ৪০৯ রিটার্ন করি — DB IntegrityError না দিয়ে ফ্রেন্ডলি মেসেজ দেখানোর জন্য।
    """
    unit = await _get_own_unit_or_404(db, unit_id, member.store_id)

    in_use = await db.scalar(
        select(MedicineUnit.id).where(MedicineUnit.unit_id == unit_id).limit(1)
    )
    if in_use is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Unit '{unit.name}' is used by one or more medicines and cannot be deleted",
        )

    await db.delete(unit)
    await db.commit()


@router.get("", response_model=list[UnitResponse])
async def list_units(
    is_active: Optional[bool] = Query(None, description="Omit to get both active and inactive"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """Global unit + এই store-এর নিজের custom unit — দুটোই একসাথে dropdown-এর জন্য।"""
    stmt = select(Unit).where(or_(Unit.store_id.is_(None), Unit.store_id == member.store_id))
    if is_active is not None:
        stmt = stmt.where(Unit.is_active == is_active)
    # global unit আগে, তারপর store-এর custom unit — dropdown-এ পরিষ্কার আলাদা দেখাবে
    rows = await db.execute(stmt.order_by(Unit.store_id.is_not(None), Unit.name))
    return [UnitResponse.model_validate(u) for u in rows.scalars().all()]


@router.patch("/{unit_id}", response_model=UnitResponse)
async def update_unit(
    unit_id: int,
    data: UnitUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """শুধু নিজের CUSTOM unit এডিট করা যায়; global unit ছুঁলে ৪০৩।"""
    unit = await _get_own_unit_or_404(db, unit_id, member.store_id)

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    if update_data.get("name") is not None:
        if await _name_taken(db, member.store_id, update_data["name"], exclude_id=unit.id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"Unit '{update_data['name']}' already exists")
        unit.name = update_data["name"].strip()
    if "short_code" in update_data:
        unit.short_code = update_data["short_code"]
    if update_data.get("is_active") is not None:
        unit.is_active = update_data["is_active"]

    await db.commit()
    await db.refresh(unit)
    return UnitResponse.model_validate(unit)


# ---------------------------------------------------------------------------
# Medicine <-> Unit
# ---------------------------------------------------------------------------

def _mu_response(mu: MedicineUnit, unit: Unit) -> MedicineUnitResponse:
    return MedicineUnitResponse(
        id=mu.id,
        medicine_id=mu.medicine_id,
        unit_id=mu.unit_id,
        unit_name=unit.name,
        unit_short_code=unit.short_code,
        conversion_factor=mu.conversion_factor,
        is_purchase_allowed=mu.is_purchase_allowed,
        is_sale_allowed=mu.is_sale_allowed,
        default_purchase_price=mu.default_purchase_price,
        default_sale_price=mu.default_sale_price,
        is_active=mu.is_active,
        created_at=mu.created_at,
    )


async def _get_medicine_or_404(db: AsyncSession, medicine_id: int, store_id: int) -> Medicine:
    medicine = await db.scalar(select(Medicine).where(Medicine.id == medicine_id, Medicine.store_id == store_id))
    if medicine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Medicine not found")
    return medicine


@medicine_units_router.get("", response_model=list[MedicineUnitResponse])
async def list_medicine_units(
    medicine_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    await _get_medicine_or_404(db, medicine_id, member.store_id)
    rows = await db.execute(
        select(MedicineUnit, Unit)
        .join(Unit, Unit.id == MedicineUnit.unit_id)
        .where(MedicineUnit.medicine_id == medicine_id)
        .order_by(MedicineUnit.conversion_factor, MedicineUnit.id)
    )
    return [_mu_response(mu, unit) for mu, unit in rows.all()]


@medicine_units_router.post("", response_model=MedicineUnitResponse, status_code=status.HTTP_201_CREATED)
async def add_medicine_unit(
    medicine_id: int,
    data: MedicineUnitCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    medicine = await _get_medicine_or_404(db, medicine_id, member.store_id)
    # get_active_unit_or_400 কে global (store_id IS NULL) + নিজের custom unit দুটোই
    # allow করতে হবে — নিচে services/unit_service.py এর নোট দ্রষ্টব্য।
    unit = await get_active_unit_or_400(db, member.store_id, data.unit_id)

    if data.unit_id == medicine.base_unit_id and data.conversion_factor != Decimal("1"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "conversion_factor of the base unit must be 1")

    exists = await db.scalar(
        select(MedicineUnit.id).where(
            MedicineUnit.medicine_id == medicine_id, MedicineUnit.unit_id == data.unit_id
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"'{unit.name}' is already added to this medicine")

    mu = MedicineUnit(
        store_id=member.store_id,
        medicine_id=medicine_id,
        unit_id=data.unit_id,
        conversion_factor=data.conversion_factor,
        is_purchase_allowed=data.is_purchase_allowed,
        is_sale_allowed=data.is_sale_allowed,
        default_purchase_price=data.default_purchase_price,
        default_sale_price=data.default_sale_price,
        is_active=True,
    )
    db.add(mu)
    await db.commit()
    await db.refresh(mu)
    return _mu_response(mu, unit)


@medicine_units_router.patch("/{unit_id}", response_model=MedicineUnitResponse)
async def update_medicine_unit(
    medicine_id: int,
    unit_id: int,
    data: MedicineUnitUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    medicine = await _get_medicine_or_404(db, medicine_id, member.store_id)

    row = (
        await db.execute(
            select(MedicineUnit, Unit)
            .join(Unit, Unit.id == MedicineUnit.unit_id)
            .where(MedicineUnit.medicine_id == medicine_id, MedicineUnit.unit_id == unit_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This unit is not configured for the medicine")
    mu, unit = row

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    if unit_id == medicine.base_unit_id:
        if update_data.get("conversion_factor") not in (None, Decimal("1")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "conversion_factor of the base unit must be 1")
        if update_data.get("is_active") is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The base unit cannot be deactivated")

    # NOT NULL columns — null মানে "বদলাবো না"; price-গুলো null দিলে মুছে যায়
    for field in ("conversion_factor", "is_purchase_allowed", "is_sale_allowed", "is_active"):
        if field in update_data and update_data[field] is None:
            update_data.pop(field)
    for field, value in update_data.items():
        setattr(mu, field, value)

    # নোট: conversion_factor বদলালে আগের purchase/sale history বদলায় না (item-এ quantity_in_unit + quantity দুটোই থাকে)
    await db.commit()
    await db.refresh(mu)
    return _mu_response(mu, unit)