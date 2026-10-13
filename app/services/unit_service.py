from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Literal, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.all_models import Medicine, MedicineUnit, Unit

TWO_PLACES = Decimal("0.01")
# base-unit price needs more precision than money: 1000/30 = 33.333333, not 33.33
PRICE_PLACES = Decimal("0.000001")
DEFAULT_UNIT_NAME = "Pcs"
DEFAULT_UNIT_CODE = "pcs"

Purpose = Literal["purchase", "sale"]


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ResolvedUnit:
    unit_id: int
    unit_name: str
    factor: Decimal  # 1 এই unit = কত base_unit
    medicine_unit: Optional[MedicineUnit]  # None => base unit-এর implicit (row না থাকলেও চলে)


def _visible_to_store(store_id: int):
    """global unit (store_id IS NULL) অথবা এই store-এর নিজের custom unit।"""
    return or_(Unit.store_id.is_(None), Unit.store_id == store_id)


# ---------------------------------------------------------------------------
# Unit master
# ---------------------------------------------------------------------------

async def get_active_unit_or_400(db: AsyncSession, store_id: int, unit_id: int) -> Unit:
    unit = await db.scalar(
        select(Unit).where(
            Unit.id == unit_id, _visible_to_store(store_id), Unit.is_active.is_(True)
        )
    )
    if unit is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid unit_id: {unit_id}")
    return unit


async def get_default_unit(db: AsyncSession, store_id: int) -> Unit:
    """
    Default 'Pcs' unit — খোঁজার ক্রম:
      ১. global 'Pcs' (super admin seed করা, সাধারণত এটাই পাওয়া যাবে)
      ২. store-এর নিজের 'Pcs' (পুরনো store যেগুলো global seed-এর আগে বানানো)
      ৩. কোনোটাই না পেলে store-এর জন্য নতুন custom 'Pcs' বানিয়ে দেয় (fallback)
    """
    # শুধু ACTIVE global unit — global row কখনো tenant request দিয়ে mutate হবে না
    unit = await db.scalar(
        select(Unit).where(
            Unit.store_id.is_(None),
            func.lower(Unit.name) == DEFAULT_UNIT_CODE,
            Unit.is_active.is_(True),
        )
    )
    if unit is None:
        unit = await db.scalar(
            select(Unit).where(Unit.store_id == store_id, func.lower(Unit.name) == DEFAULT_UNIT_CODE)
        )
    if unit is None:
        unit = Unit(store_id=store_id, name=DEFAULT_UNIT_NAME, short_code=DEFAULT_UNIT_CODE, is_active=True)
        db.add(unit)
        await db.flush()
    elif not unit.is_active:  # এখানে unit সবসময় store-এর নিজের (global হলে উপরে active filter আছে)
        unit.is_active = True
    return unit


async def load_unit_map(db: AsyncSession, store_id: int, unit_ids: Iterable[int]) -> dict[int, Unit]:
    ids = {i for i in unit_ids if i is not None}
    if not ids:
        return {}
    rows = await db.execute(select(Unit).where(Unit.id.in_(ids), _visible_to_store(store_id)))
    return {u.id: u for u in rows.scalars().all()}


# ---------------------------------------------------------------------------
# MedicineUnit
# ---------------------------------------------------------------------------

async def ensure_base_medicine_unit(db: AsyncSession, store_id: int, medicine: Medicine) -> MedicineUnit:
    """medicine-এর base unit এর জন্য conversion_factor=1 এর MedicineUnit row নিশ্চিত করে।"""
    mu = await db.scalar(
        select(MedicineUnit).where(
            MedicineUnit.medicine_id == medicine.id,
            MedicineUnit.unit_id == medicine.base_unit_id,
        )
    )
    if mu is None:
        mu = MedicineUnit(
            store_id=store_id,
            medicine_id=medicine.id,
            unit_id=medicine.base_unit_id,
            conversion_factor=Decimal("1"),
            is_purchase_allowed=True,
            is_sale_allowed=True,
            is_active=True,
        )
        db.add(mu)
        await db.flush()
    return mu


async def create_medicine_with_base_unit(
    db: AsyncSession, store_id: int, *, default_unit: Optional[Unit] = None, **medicine_kwargs
) -> Medicine:
    """
    Master থেকে auto-create (purchase / requisition / stock receipt) — সবখানে
    আগে `unit="pcs"` ছিল; এখন global/store-এর Pcs unit + base MedicineUnit row বসে।
    """
    unit = default_unit or await get_default_unit(db, store_id)
    medicine = Medicine(store_id=store_id, base_unit_id=unit.id, **medicine_kwargs)
    db.add(medicine)
    await db.flush()
    await ensure_base_medicine_unit(db, store_id, medicine)
    return medicine


async def load_medicine_units(
    db: AsyncSession, medicine_ids: Iterable[int]
) -> dict[tuple[int, int], MedicineUnit]:
    ids = set(medicine_ids)
    if not ids:
        return {}
    rows = await db.execute(select(MedicineUnit).where(MedicineUnit.medicine_id.in_(ids)))
    return {(mu.medicine_id, mu.unit_id): mu for mu in rows.scalars().all()}


def resolve_unit(
    medicine: Medicine,
    unit_id: int,
    unit_map: dict[int, Unit],
    mu_map: dict[tuple[int, int], MedicineUnit],
    *,
    purpose: Purpose,
) -> ResolvedUnit:
    unit = unit_map.get(unit_id)
    if unit is None or not unit.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid unit_id: {unit_id}")

    mu = mu_map.get((medicine.id, unit_id))
    if mu is None:
        # base unit-এর row না থাকলেও (পুরনো data) base unit-এ কেনাবেচা চলবে
        if unit_id == medicine.base_unit_id:
            return ResolvedUnit(unit_id, unit.name, Decimal("1"), None)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{unit.name}' is not configured as a unit for {medicine.name}",
        )

    if not mu.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{unit.name}' is inactive for {medicine.name}")
    if purpose == "purchase" and not mu.is_purchase_allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{medicine.name} cannot be purchased in '{unit.name}'")
    if purpose == "sale" and not mu.is_sale_allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{medicine.name} cannot be sold in '{unit.name}'")

    return ResolvedUnit(unit_id, unit.name, mu.conversion_factor, mu)


def _new_unit_needs_conversion_factor(
    unit: Unit, medicine: Medicine, requested_medicine_id: int
) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {
            "code": "unit_needs_conversion_factor",
            "message": (
                f"'{unit.name}' is not yet configured as a unit for {medicine.name}. "
                f"Send conversion_factor (1 {unit.name} = কত base unit?) with this line and retry."
            ),
            "unit_id": unit.id,
            "unit_name": unit.name,
            # IMPORTANT: echo back the medicine_id the client actually sent in the
            # request line (which, for an auto-created-from-master medicine, is the
            # MasterMedicine id — NOT medicine.id). If a client retries with an
            # unfilled request, the just-created local Medicine row gets rolled
            # back along with the raised 422, so medicine.id would be a
            # throwaway value the client can never match against its own
            # payload -> it can never attach conversion_factor -> infinite retry
            # loop. Echoing the client's own id lets it match reliably.
            "medicine_id": requested_medicine_id,
            "medicine_name": medicine.name,
        },
    )


async def resolve_or_create_unit_for_receiving(
    db: AsyncSession,
    store_id: int,
    medicine: Medicine,
    unit_id: int,
    unit_map: dict[int, Unit],
    mu_map: dict[tuple[int, int], MedicineUnit],
    conversion_factor: Optional[Decimal] = None,
    requested_medicine_id: Optional[int] = None,
) -> ResolvedUnit:
    """
    resolve_unit()-এর "receiving-family" variant — stock receiving (GRN), direct
    purchase, এবং purchase requisition তিনটাতেই ব্যবহারযোগ্য (purpose সবসময়
    purchase-side; sale.py এখনো strict resolve_unit()-ই ব্যবহার করে, touched হয়নি)।

    MedicineUnit row না থাকলে সরাসরি 400 না দিয়ে:
      * conversion_factor দেওয়া থাকলে -> এখনই নতুন MedicineUnit বানিয়ে এগিয়ে যায়
        (is_sale_allowed=False দিয়ে বসে -- receiving শুধু purchase-side configure
        করে, বিক্রির জন্য owner কে আলাদাভাবে medicine settings থেকে enable করতে হবে)
      * conversion_factor না থাকলে -> 422 + code="unit_needs_conversion_factor"
        (frontend সেটা user-কে জিজ্ঞেস করে নিয়ে same request আবার পাঠাবে)

    NOTE: resolve_unit() ইচ্ছাকৃতভাবে অপরিবর্তিত রাখা হয়েছে।
    """
    unit = unit_map.get(unit_id)
    if unit is None or not unit.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid unit_id: {unit_id}")

    mu = mu_map.get((medicine.id, unit_id))
    if mu is not None:
        if not mu.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"'{unit.name}' is inactive for {medicine.name}")
        if not mu.is_purchase_allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{medicine.name} cannot be purchased in '{unit.name}'"
            )
        return ResolvedUnit(unit_id, unit.name, mu.conversion_factor, mu)

    if unit_id == medicine.base_unit_id:
        # base unit-এর row না থাকলেও (পুরনো data) base unit-এ চলবে
        return ResolvedUnit(unit_id, unit.name, Decimal("1"), None)

    # নতুন unit — conversion_factor ছাড়া এগোনো যাবে না
    if conversion_factor is None:
        raise _new_unit_needs_conversion_factor(
            unit, medicine, requested_medicine_id or medicine.id
        )
    if conversion_factor <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "conversion_factor must be greater than 0")

    new_mu = MedicineUnit(
        store_id=store_id,
        medicine_id=medicine.id,
        unit_id=unit_id,
        conversion_factor=conversion_factor,
        is_purchase_allowed=True,
        is_sale_allowed=False,
        is_active=True,
    )
    db.add(new_mu)
    await db.flush()
    # same request-এর মধ্যে ২য় লাইনে একই (medicine, unit) এলে আবার জিজ্ঞেস না করে পুনর্ব্যবহার করবে
    mu_map[(medicine.id, unit_id)] = new_mu
    return ResolvedUnit(unit_id, unit.name, conversion_factor, new_mu)


# ---------------------------------------------------------------------------
# Conversion math
# ---------------------------------------------------------------------------

def to_base_quantity(quantity_in_unit: int, factor: Decimal) -> int:
    """quantity_in_unit × factor -> base-unit পূর্ণসংখ্যা। ভগ্নাংশ হলে 400।"""
    base = Decimal(quantity_in_unit) * Decimal(factor)
    if base != base.to_integral_value():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{quantity_in_unit} × {Decimal(factor).normalize()} = {base.normalize()} "
            "is not a whole number of base units",
        )
    return int(base)


def price_per_base(price_per_unit: Decimal, factor: Decimal) -> Decimal:
    """ঐ unit-এর দাম -> base unit-এর দাম (6 decimal; StockBatch Numeric(14,6) এ বসে)।
    Total/line amount সবসময় money() দিয়ে 2 decimal-এ round হয়, এখানে না।"""
    return (Decimal(price_per_unit) / Decimal(factor)).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)


def price_per_unit(price_per_base_unit: Decimal, factor: Decimal) -> Decimal:
    """base unit-এর দাম -> কোনো unit-এর দাম।"""
    return money(Decimal(price_per_base_unit) * Decimal(factor))