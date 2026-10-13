# app/services/medicine_resolver.py
"""purchase / requisition item-এর medicine resolve।

আগে `medicine_id` store-এর না হলে MasterMedicine.id ধরা হতো -> ID collide করলে ভুল
medicine কেনা হতো। এখন:
  * `medicine_id`         -> অবশ্যই এই store-এর Medicine.id
  * `master_medicine_id`  -> catalog থেকে; store-এ না থাকলে auto-create
দুটোর একটাই পাঠাতে হবে।"""
from fastapi import HTTPException, status
from sqlalchemy import select

from app.models.all_models import MasterMedicine, Medicine
from app.services.unit_service import create_medicine_with_base_unit


async def resolve_item_medicines(db, store_id: int, items) -> list[Medicine]:
    """items-এর সাথে এক-এক মিলিয়ে Medicine list ফেরত দেয়।"""
    store_ids: set[int] = set()
    master_ids: set[int] = set()
    for it in items:
        mid = getattr(it, "medicine_id", None)
        gid = getattr(it, "master_medicine_id", None)
        if mid is not None and gid is not None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Send either medicine_id or master_medicine_id, not both",
            )
        if mid is None and gid is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "medicine_id or master_medicine_id is required"
            )
        (store_ids if gid is None else master_ids).add(mid if gid is None else gid)

    by_store: dict[int, Medicine] = {}
    if store_ids:
        rows = await db.execute(
            select(Medicine).where(Medicine.id.in_(store_ids), Medicine.store_id == store_id)
        )
        by_store = {m.id: m for m in rows.scalars().all()}
        missing = store_ids - by_store.keys()
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid medicine_id(s): {sorted(missing)} "
                "(catalog medicines must be sent as master_medicine_id)",
            )
        for m in by_store.values():
            if not m.is_active:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Medicine '{m.name}' is inactive"
                )

    by_master: dict[int, Medicine] = {}
    if master_ids:
        rows = await db.execute(
            select(MasterMedicine).where(
                MasterMedicine.id.in_(master_ids), MasterMedicine.is_active.is_(True)
            )
        )
        masters = {m.id: m for m in rows.scalars().all()}
        missing = master_ids - masters.keys()
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Invalid master_medicine_id(s): {sorted(missing)}"
            )
        for gid, master in masters.items():
            existing = await db.scalar(
                select(Medicine).where(
                    Medicine.master_medicine_id == gid, Medicine.store_id == store_id
                )
            )
            if existing is not None:
                if not existing.is_active:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, f"Medicine '{existing.name}' is inactive"
                    )
                by_master[gid] = existing
            else:
                by_master[gid] = await create_medicine_with_base_unit(
                    db,
                    store_id,
                    name=master.brand_name,
                    generic_name=master.generic_name,
                    category=None,
                    reorder_level=10,
                    master_medicine_id=master.id,
                    is_active=True,
                )

    out: list[Medicine] = []
    for it in items:
        gid = getattr(it, "master_medicine_id", None)
        out.append(by_master[gid] if gid is not None else by_store[it.medicine_id])
    return out
