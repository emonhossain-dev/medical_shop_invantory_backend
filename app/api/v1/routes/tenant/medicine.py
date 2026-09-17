from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
from app.models.all_models import StoreMember, Medicine, MasterMedicine
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

router = APIRouter(prefix="/medicines", tags=["Medicine"])


@router.post("", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
async def create_medicine(
    data: MedicineCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    medicine = await repo.create(
        name=data.name,
        generic_name=data.generic_name,
        category=data.category,
        unit=data.unit,
        reorder_level=data.reorder_level,
    )
    await repo.db.commit()
    return MedicineResponse.model_validate(medicine)


@router.post("/from-master/{master_medicine_id}", response_model=MedicineResponse, status_code=status.HTTP_201_CREATED)
async def create_medicine_from_master(
    master_medicine_id: int,
    data: MedicineFromMasterRequest,
    member: StoreMember = Depends(require_store_admin),
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

    # Duplicate check — ei master medicine ta global medicines table e age theke add kora ki na
    existing_query = select(Medicine).where(
        Medicine.master_medicine_id == master_medicine_id,
    )
    existing_result = await db.execute(existing_query)
    existing = existing_result.scalar_one_or_none()
    if existing:
        return MedicineResponse.model_validate(existing)

    medicine = await repo.create(
        name=master.brand_name,
        generic_name=master.generic_name,
        category=data.category,
        unit=data.unit or "pcs",
        reorder_level=data.reorder_level if data.reorder_level is not None else 10,
        master_medicine_id=master.id,
    )
    await repo.db.commit()
    return MedicineResponse.model_validate(medicine)


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
        linked_query = select(Medicine.master_medicine_id).where(
            Medicine.master_medicine_id.in_(master_ids),
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

    # --- medicines (global catalog) table check — ekhane always check hoy, ---
    # --- master e result thakle o skip kora hoy na ---
    store_query = (
        select(Medicine)
        .where(
            (Medicine.name.ilike(like)) | (Medicine.generic_name.ilike(like)),
        )
        .order_by(Medicine.name)
        .limit(limit)
    )
    store_medicines = (await db.execute(store_query)).scalars().all()

    for med in store_medicines:
        results.append(
            MedicineSearchResultItem(
                source="store",
                id=med.id,
                name=med.name,
                generic_name=med.generic_name,
                category=med.category,
                unit=med.unit,
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
    query = select(Medicine)

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
    medicines = result.scalars().all()
    return [MedicineResponse.model_validate(m) for m in medicines]


@router.get("/{medicine_id}", response_model=MedicineResponse)
async def get_medicine(
    medicine_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    medicine = await repo.get_by_id_or_404(medicine_id)
    return MedicineResponse.model_validate(medicine)


@router.patch("/{medicine_id}", response_model=MedicineResponse)
async def update_medicine(
    medicine_id: int,
    data: MedicineUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Medicine] = Depends(get_repository(Medicine)),
):
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    medicine = await repo.update(medicine_id, **update_data)
    if medicine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    await repo.db.commit()
    return MedicineResponse.model_validate(medicine)


@router.delete("/{medicine_id}", response_model=MedicineResponse)
async def deactivate_medicine(
    medicine_id: int,
    member: StoreMember = Depends(require_store_admin),
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
    return MedicineResponse.model_validate(medicine)