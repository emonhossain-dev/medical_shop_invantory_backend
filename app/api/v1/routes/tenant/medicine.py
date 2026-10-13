from collections import defaultdict
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import (
    MasterMedicine,
    Medicine,
    MedicineUnit,
    PurchaseItem,
    SaleItem,
    StockBatch,
    StoreMember,
    Unit,
)
from app.repositories.base import BaseRepository
from app.repositories.deps import get_repository
from app.schemas.medicine import (
    MedicineCreateRequest,
    MedicineUpdateRequest,
    MedicineResponse,
    MedicineSearchResultItem,
    MedicineSearchResponse,
    MedicineFromMasterRequest,
)
from app.schemas.unit import MedicineUnitCreateRequest, MedicineUnitResponse
from app.services.unit_service import (
    ensure_base_medicine_unit,
    get_active_unit_or_400,
    get_default_unit,
)

router = APIRouter(prefix="/medicines", tags=["Medicine"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _serialize_medicines(db: AsyncSession, medicines: list[Medicine]) -> list[MedicineResponse]:
    """
    Medicine.base_unit / Medicine.units async session-এ lazy-load হয় না, তাই
    unit নাম ও MedicineUnit list একসাথে batch query করে response হাতে বানানো হচ্ছে।
    """
    if not medicines:
        return []

    medicine_ids = [m.id for m in medicines]
    rows = await db.execute(
        select(MedicineUnit, Unit)
        .join(Unit, Unit.id == MedicineUnit.unit_id)
        .where(MedicineUnit.medicine_id.in_(medicine_ids))
        .order_by(MedicineUnit.conversion_factor, MedicineUnit.id)
    )
    units_by_medicine: dict[int, list[MedicineUnitResponse]] = defaultdict(list)
    for mu, unit in rows.all():
        units_by_medicine[mu.medicine_id].append(
            MedicineUnitResponse(
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
        )

    base_ids = {m.base_unit_id for m in medicines}
    name_rows = await db.execute(select(Unit.id, Unit.name).where(Unit.id.in_(base_ids)))
    base_names = {r.id: r.name for r in name_rows.all()}

    return [
        MedicineResponse(
            id=m.id,
            name=m.name,
            generic_name=m.generic_name,
            category=m.category,
            base_unit_id=m.base_unit_id,
            base_unit_name=base_names.get(m.base_unit_id),
            units=units_by_medicine.get(m.id, []),
            reorder_level=m.reorder_level,
            is_active=m.is_active,
            master_medicine_id=m.master_medicine_id,
            created_at=m.created_at,
        )
        for m in medicines
    ]


async def _serialize_one(db: AsyncSession, medicine: Medicine) -> MedicineResponse:
    return (await _serialize_medicines(db, [medicine]))[0]


async def _add_medicine_units(
    db: AsyncSession,
    store_id: int,
    medicine: Medicine,
    units: Optional[list[MedicineUnitCreateRequest]],
) -> None:
    """
    base unit-এর row (conversion_factor=1) সবসময় বসে; তার সাথে request-এর extra unit (Box/Strip...)।
    """
    requested: dict[int, MedicineUnitCreateRequest] = {}
    for u in units or []:
        if u.unit_id in requested:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Duplicate unit_id in units: {u.unit_id}")
        requested[u.unit_id] = u

    for unit_id in requested:
        await get_active_unit_or_400(db, store_id, unit_id)

    # base unit row
    base_req = requested.pop(medicine.base_unit_id, None)
    if base_req is not None and base_req.conversion_factor != Decimal("1"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "conversion_factor of the base unit must be 1"
        )
    db.add(
        MedicineUnit(
            store_id=store_id,
            medicine_id=medicine.id,
            unit_id=medicine.base_unit_id,
            conversion_factor=Decimal("1"),
            is_purchase_allowed=base_req.is_purchase_allowed if base_req else True,
            is_sale_allowed=base_req.is_sale_allowed if base_req else True,
            default_purchase_price=base_req.default_purchase_price if base_req else None,
            default_sale_price=base_req.default_sale_price if base_req else None,
            is_active=True,
        )
    )

    for u in requested.values():
        db.add(
            MedicineUnit(
                store_id=store_id,
                medicine_id=medicine.id,
                unit_id=u.unit_id,
                conversion_factor=u.conversion_factor,
                is_purchase_allowed=u.is_purchase_allowed,
                is_sale_allowed=u.is_sale_allowed,
                default_purchase_price=u.default_purchase_price,
                default_sale_price=u.default_sale_price,
                is_active=True,
            )
        )
    await db.flush()


@router.post("", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
async def create_medicine(
    data: MedicineCreateRequest,
    member: StoreMember = Depends(require_permission("medicines", "create")),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    db = repo.db
    await get_active_unit_or_400(db, member.store_id, data.base_unit_id)

    medicine = await repo.create(
        name=data.name,
        generic_name=data.generic_name,
        category=data.category,
        base_unit_id=data.base_unit_id,
        reorder_level=data.reorder_level,
    )
    await db.flush()
    await _add_medicine_units(db, member.store_id, medicine, data.units)

    await db.commit()
    await db.refresh(medicine)
    return await _serialize_one(db, medicine)


@router.post("/from-master/{master_medicine_id}", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
async def create_medicine_from_master(
    master_medicine_id: int,
    data: MedicineFromMasterRequest,
    member: StoreMember = Depends(require_permission("medicines", "create")),
    db: AsyncSession = Depends(get_db),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    """
    Search-e master_medicines theke user je medicine ta select korbe,
    seta e-i endpoint diye store-er nijer medicines table e add hobe.
    """
    master = await db.get(MasterMedicine, master_medicine_id)
    if master is None or not master.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master medicine not found")

    # Duplicate check — ei store-e ei master medicine ta age theke add kora ki na
    # (FIX: age store_id filter chhilo na, tai onno store-er Medicine row eshe
    # jeto — sheita e "pcs is not configured" bug-er root cause)
    existing_query = select(Medicine).where(
        Medicine.master_medicine_id == master_medicine_id,
        Medicine.store_id == member.store_id,
    )
    existing_result = await db.execute(existing_query)
    existing = existing_result.scalar_one_or_none()
    if existing:
        return await _serialize_one(db, existing)

    # base_unit_id না দিলে store-এর default "Pcs" unit (না থাকলে তৈরি হয়)
    if data.base_unit_id is not None:
        base_unit = await get_active_unit_or_400(repo.db, member.store_id, data.base_unit_id)
    else:
        base_unit = await get_default_unit(repo.db, member.store_id)

    medicine = await repo.create(
        name=master.brand_name,
        generic_name=master.generic_name,
        category=data.category,
        base_unit_id=base_unit.id,
        reorder_level=data.reorder_level if data.reorder_level is not None else 10,
        master_medicine_id=master.id,
    )
    await repo.db.flush()
    await ensure_base_medicine_unit(repo.db, member.store_id, medicine)

    await repo.db.commit()
    await repo.db.refresh(medicine)
    return await _serialize_one(repo.db, medicine)


@router.get("/search", response_model=MedicineSearchResponse)
async def search_medicine(
    q: str = Query(..., min_length=1, description="Medicine name/generic name/brand/SKU diye search"),
    limit: int = Query(20, ge=1, le=50),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """
    Ekta-i API — er moddhei master_medicines AND medicines (global catalog)
    duita table-i check kore, result gulo ekta single flat list e combine
    kore firiye dey (source: "master" / "store" diye tag kora).

    - source="master" -> master theke, "Add" korle /medicines/from-master/{id}
      call korte hobe. already_in_store=true mane eta already global medicines
      table e add kora (kono specific store er jonno na).
    - source="store"  -> global medicines table e already ache, notun kore
      add korar dorkar nei, sorasori eta select kore nite parbe.
    - found=false (results khali) hole frontend manual "add medicine"
      form dekhabe (POST /medicines diye).
    """
    like = f"%{q}%"
    results: list[MedicineSearchResultItem] = []

    # --- master_medicines table check ---
    master_query = (
        select(MasterMedicine)
        .where(
            MasterMedicine.is_active.is_(True),
            (MasterMedicine.generic_name.ilike(like))
            | (MasterMedicine.brand_name.ilike(like))
            | (MasterMedicine.sku.ilike(like)),
        )
        .order_by(MasterMedicine.brand_name)
        .limit(limit)
    )
    master_medicines = (await db.execute(master_query)).scalars().all()

    linked_ids: set[int] = set()
    if master_medicines:
        master_ids = [m.id for m in master_medicines]
        # শুধু এই store এর medicine দেখে already_in_store ঠিক হবে (আগে সব store দেখত)
        linked_query = select(Medicine.master_medicine_id).where(
            Medicine.master_medicine_id.in_(master_ids),
            Medicine.store_id == member.store_id,
        )
        linked_ids = {row[0] for row in (await db.execute(linked_query)).all()}

    for m in master_medicines:
        results.append(
            MedicineSearchResultItem(
                source="master",
                id=m.id,
                name=m.brand_name,
                generic_name=m.generic_name,
                strength=m.strength,
                dosage_form=m.dosage_form,
                manufacturer=m.manufacturer,
                already_in_store=m.id in linked_ids,
            )
        )

    # --- ei store-er nijer medicines table check — ekhane always check hoy, ---
    # --- master e result thakle o skip kora hoy na ---
    # (FIX: age store_id filter chhilo na, tai onno store-er medicine o
    # search result e chole ashto — data-isolation leak)
    store_query = (
        select(Medicine)
        .where(
            Medicine.store_id == member.store_id,
            Medicine.is_active.is_(True),
            (Medicine.name.ilike(like)) | (Medicine.generic_name.ilike(like)),
        )
        .order_by(Medicine.name)
        .limit(limit)
    )
    store_medicines = (await db.execute(store_query)).scalars().all()

    base_names: dict[int, str] = {}
    if store_medicines:
        name_rows = await db.execute(
            select(Unit.id, Unit.name).where(Unit.id.in_({m.base_unit_id for m in store_medicines}))
        )
        base_names = {r.id: r.name for r in name_rows.all()}

    for med in store_medicines:
        results.append(
            MedicineSearchResultItem(
                source="store",
                id=med.id,
                name=med.name,
                generic_name=med.generic_name,
                category=med.category,
                base_unit_id=med.base_unit_id,
                base_unit_name=base_names.get(med.base_unit_id),
                master_medicine_id=med.master_medicine_id,
            )
        )

    return MedicineSearchResponse(query=q, found=bool(results), results=results)


@router.get("", response_model=list[MedicineResponse])
async def list_medicines(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by name or generic_name"),
    category: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None, description="Omit to get both active and inactive"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    # (FIX: age store_id filter chhilo na, tai onno store-er medicine-o
    # list e chole ashto — data-isolation leak)
    query = select(Medicine).where(Medicine.store_id == member.store_id)

    if search:
        query = query.where(
            (Medicine.name.ilike(f"%{search}%")) | (Medicine.generic_name.ilike(f"%{search}%"))
        )
    if category:
        query = query.where(Medicine.category == category)
    if is_active is not None:
        query = query.where(Medicine.is_active == is_active)

    query = query.order_by(Medicine.name).offset(skip).limit(limit)

    result = await db.execute(query)
    medicines = list(result.scalars().all())
    return await _serialize_medicines(db, medicines)


@router.get("/{medicine_id}", response_model=MedicineResponse)
async def get_medicine(
    medicine_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    medicine = await repo.get_by_id_or_404(medicine_id)
    return await _serialize_one(repo.db, medicine)


@router.patch("/{medicine_id}", response_model=MedicineResponse)
async def update_medicine(
    medicine_id: int,
    data: MedicineUpdateRequest,
    member: StoreMember = Depends(require_permission("medicines", "update")),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    db = repo.db

    # base_unit_id NOT NULL — explicit null মানে "বদলাবো না"
    if "base_unit_id" in update_data and update_data["base_unit_id"] is None:
        update_data.pop("base_unit_id")
        if not update_data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    new_base_id = update_data.get("base_unit_id")
    if new_base_id is not None:
        current = await repo.get_by_id_or_404(medicine_id)
        if new_base_id == current.base_unit_id:
            update_data.pop("base_unit_id")
        else:
            await get_active_unit_or_400(db, member.store_id, new_base_id)
            # Stock/purchase/sale সব পুরনো base unit-এ আছে — base বদলালে সব quantity ভুল হয়ে যাবে।
            for model in (StockBatch, PurchaseItem, SaleItem):
                used = await db.scalar(select(model.id).where(model.medicine_id == medicine_id).limit(1))
                if used is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="base_unit cannot be changed after this medicine has stock or transactions",
                    )

    if update_data:
        medicine = await repo.update(medicine_id, **update_data)
        if medicine is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    else:
        medicine = await repo.get_by_id_or_404(medicine_id)

    if new_base_id is not None and "base_unit_id" in update_data:
        await ensure_base_medicine_unit(db, member.store_id, medicine)

    await db.commit()
    await db.refresh(medicine)
    return await _serialize_one(db, medicine)


@router.delete("/{medicine_id}", response_model=MedicineResponse)
async def deactivate_medicine(
    medicine_id: int,
    member: StoreMember = Depends(require_permission("medicines", "delete")),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    """
    Hard delete দেওয়া হয়নি ইচ্ছাকৃতভাবে — medicine_id FK RESTRICT দিয়ে
    purchase_items/sale_items এ protected, একবার কোনো transaction হয়ে গেলে
    DB লেভেলে delete fail করবে। তাই এখানে soft-delete (is_active=False)।
    """
    medicine = await repo.update(medicine_id, is_active=False)
    if medicine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    await repo.db.commit()
    await repo.db.refresh(medicine)
    return await _serialize_one(repo.db, medicine)


@router.post("/{medicine_id}/reactivate", response_model=MedicineResponse)
async def reactivate_medicine(
    medicine_id: int,
    member: StoreMember = Depends(require_permission("medicines", "update")),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    """Soft-delete করা medicine আবার চালু করে।"""
    medicine = await repo.update(medicine_id, is_active=True)
    if medicine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    await repo.db.commit()
    await repo.db.refresh(medicine)
    return await _serialize_one(repo.db, medicine)
