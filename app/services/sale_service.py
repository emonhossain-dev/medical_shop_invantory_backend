# app/services/sale_service.py
"""Sale.py (create_sale) ও hold_sale.py (resume) এর shared logic।
আগে দুই জায়গায় হুবহু copy ছিল -- এখন একটাই।"""
import random
import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import or_, select

from app.models.all_models import Customer, Medicine, Sale, SaleItem, StockBatch
from app.schemas.sale import SaleItemResponse, SaleResponse
from app.services.date_utils import dhaka_now, dhaka_today
from app.services.unit_service import (
    load_medicine_units,
    load_unit_map,
    money,
    price_per_unit,
    resolve_unit,
    to_base_quantity,
)


@dataclass
class SaleLine:
    medicine_id: Optional[int]  # external হলে None
    quantity: int  # sale unit-এ
    unit_id: Optional[int] = None  # None হলে medicine-এর base unit
    unit_price: Optional[Decimal] = None  # ঐ unit-এর দাম
    stock_batch_id: Optional[int] = None
    is_external: bool = False  # true হলে stock কাটা হয় না, শুধু record থাকে
    external_name: Optional[str] = None


def generate_invoice_no(store_id: int, branch_id: int) -> str:
    """INV-<store>-<branch>-<yymmdd>-<HHMMSS><3 digit>  (Dhaka time)।
    আগের 4-digit random suffix দিনে ~১০০ sale এর পর প্রায়ই collide করত।"""
    now = dhaka_now()
    suffix = "".join(random.choices("0123456789", k=3))
    return f"INV-{store_id}-{branch_id}-{now:%y%m%d}-{now:%H%M%S}{suffix}"


async def get_available_batches(
    db,
    store_id: int,
    branch_id: int,
    medicine_id: int,
    lock: bool = False,
    include_expired: bool = False,
) -> list[StockBatch]:
    """FEFO: nearest expiry আগে, expiry-হীন batch শেষে, তারপর পুরনো received আগে।
    Expired batch default-এ বাদ।"""
    stmt = select(StockBatch).where(
        StockBatch.store_id == store_id,
        StockBatch.branch_id == branch_id,
        StockBatch.medicine_id == medicine_id,
        StockBatch.quantity > 0,
    )
    if not include_expired:
        stmt = stmt.where(
            or_(StockBatch.expiry_date.is_(None), StockBatch.expiry_date >= dhaka_today())
        )
    stmt = stmt.order_by(
        StockBatch.expiry_date.is_(None),
        StockBatch.expiry_date.asc(),
        StockBatch.received_at.asc(),
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars().all())


async def load_active_medicines(db, store_id: int, lines: list[SaleLine]) -> dict[int, Medicine]:
    medicines: dict[int, Medicine] = {}
    for line in lines:
        if line.is_external:
            continue
        if line.medicine_id in medicines:
            continue
        medicine = await db.get(Medicine, line.medicine_id)
        if not medicine or medicine.store_id != store_id or not medicine.is_active:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Medicine {line.medicine_id} not found"
            )
        medicines[line.medicine_id] = medicine
    return medicines


async def build_sale_items(
    db, *, store_id: int, branch_id: int, lines: list[SaleLine], sale: Sale
) -> Decimal:
    """Stock কেটে sale.items বানায়, subtotal ফেরত দেয়। Caller commit/rollback করবে।"""
    medicines = await load_active_medicines(db, store_id, lines)

    # External line আগেই যাচাই: নাম ও দাম (>0) লাগবে, medicine_id দেওয়া চলবে না
    for line in lines:
        if line.is_external:
            if not (line.external_name or "").strip():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "external_name is required for external items"
                )
            if line.unit_price is None or line.unit_price <= 0:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"unit_price is required for external item {line.external_name}",
                )
            if line.quantity <= 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be greater than 0")
        elif line.medicine_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "medicine_id is required for non-external items"
            )

    # সব related batch আগে id-order এ lock: দুই cashier উল্টো order-এ medicine বেচলেও deadlock হয় না
    await db.execute(
        select(StockBatch.id)
        .where(
            StockBatch.store_id == store_id,
            StockBatch.branch_id == branch_id,
            StockBatch.medicine_id.in_(list(medicines.keys())),
            StockBatch.quantity > 0,
        )
        .order_by(StockBatch.id)
        .with_for_update()
    )
    unit_ids = [
        l.unit_id or medicines[l.medicine_id].base_unit_id for l in lines if not l.is_external
    ]
    unit_map = await load_unit_map(db, store_id, unit_ids)
    mu_map = await load_medicine_units(db, medicines.keys())
    today = dhaka_today()
    subtotal = Decimal("0")

    for line in lines:
        if line.is_external:
            # বাইরের ওষুধ: stock কাটা হয় না, শুধু invoice/record-এ থাকে
            ext_total = money(line.unit_price * line.quantity)
            sale.items.append(
                SaleItem(
                    store_id=store_id,
                    medicine_id=None,
                    stock_batch_id=None,
                    is_external=True,
                    external_name=line.external_name.strip(),
                    unit_id=None,
                    quantity_in_unit=line.quantity,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=ext_total,
                )
            )
            subtotal += ext_total
            continue

        medicine = medicines[line.medicine_id]
        unit_id = line.unit_id or medicine.base_unit_id
        ru = resolve_unit(medicine, unit_id, unit_map, mu_map, purpose="sale")
        factor = ru.factor
        default_unit_price = (
            ru.medicine_unit.default_sale_price if ru.medicine_unit is not None else None
        )

        def _unit_price(batch: StockBatch) -> Decimal:
            if line.unit_price is not None:
                return line.unit_price
            if default_unit_price is not None:
                return default_unit_price
            return price_per_unit(batch.sale_price, factor)

        def _add_item(batch: StockBatch, units: int) -> None:
            nonlocal subtotal
            base = to_base_quantity(units, factor)
            price = _unit_price(batch)
            batch.quantity -= base
            line_total = money(price * units)
            sale.items.append(
                SaleItem(
                    store_id=store_id,
                    medicine_id=line.medicine_id,
                    stock_batch_id=batch.id,
                    unit_id=ru.unit_id,
                    quantity_in_unit=units,
                    quantity=base,
                    unit_price=price,
                    line_total=line_total,
                )
            )
            subtotal += line_total

        remaining = line.quantity

        if line.stock_batch_id is not None:
            batch = await db.get(StockBatch, line.stock_batch_id, with_for_update=True)
            if (
                not batch
                or batch.store_id != store_id
                or batch.branch_id != branch_id
                or batch.medicine_id != line.medicine_id
            ):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"Stock batch {line.stock_batch_id} not found for this medicine/branch",
                )
            if batch.expiry_date is not None and batch.expiry_date < today:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Batch {batch.batch_no or batch.id} of {medicine.name} is expired "
                    f"({batch.expiry_date}) and cannot be sold",
                )
            base_needed = to_base_quantity(remaining, factor)
            if batch.quantity < base_needed:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Insufficient stock in batch {batch.id} for {medicine.name} "
                    f"(available {batch.quantity} base units, requested "
                    f"{remaining} {ru.unit_name} = {base_needed} base units)",
                )
            _add_item(batch, remaining)
            continue

        batches = await get_available_batches(db, store_id, branch_id, line.medicine_id, lock=True)

        def _whole_units(b: StockBatch) -> int:
            return int((Decimal(b.quantity) / factor).to_integral_value(rounding=ROUND_FLOOR))

        total_units = sum(_whole_units(b) for b in batches)
        if total_units < remaining:
            total_base = sum(b.quantity for b in batches)
            detail = (
                f"Insufficient (non-expired) stock for {medicine.name} "
                f"(available {total_units} {ru.unit_name}, requested {remaining})"
            )
            if factor != 1 and total_base > 0:
                detail += (
                    f". {total_base} base units are in stock, but a whole {ru.unit_name} "
                    f"must come from a single batch — sell the remainder in the base unit"
                )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)

        for batch in batches:
            if remaining <= 0:
                break
            take = min(_whole_units(batch), remaining)
            if take <= 0:
                continue
            _add_item(batch, take)
            remaining -= take

    return subtotal


def apply_totals(
    sale: Sale,
    subtotal: Decimal,
    discount: Decimal,
    discount_type: Optional[str],
    paid_amount: Decimal,
) -> None:
    """subtotal -> discount -> total -> paid -> due। sale.discount সবসময় টাকায় সেভ হয়।"""
    discount = Decimal(discount or 0)
    paid_amount = Decimal(paid_amount or 0)
    if discount < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount cannot be negative")
    if paid_amount < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paid amount cannot be negative")

    if discount_type == "percent":
        if discount > 100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount percent cannot exceed 100")
        discount_amount = money(subtotal * discount / Decimal("100"))
    else:
        discount_amount = money(discount)

    total = subtotal - discount_amount
    if total < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Discount cannot exceed subtotal")
    due = total - paid_amount
    if due < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paid amount cannot exceed total")

    sale.subtotal = subtotal
    sale.discount = discount_amount
    sale.total = total
    sale.paid_amount = paid_amount
    sale.due_amount = due


def _phone_variants(raw: str) -> tuple[str, set[str]]:
    """BD number: 017xxxxxxxx / +88017xxxxxxxx / 88017xxxxxxxx / '017-xxx xxxxx' একই customer।
    (normalized, lookup-এ ধরার মতো সব form) ফেরত দেয়। BD pattern না হলে raw-ই থাকে।"""
    digits = re.sub(r"[^\d]", "", raw)
    norm = raw
    if len(digits) == 13 and digits.startswith("880"):
        norm = "0" + digits[3:]
    elif len(digits) == 11 and digits.startswith("01"):
        norm = digits
    variants = {raw, norm}
    if norm.startswith("01") and len(norm) == 11:
        variants |= {"+88" + norm, "88" + norm}
    return norm, variants


async def ensure_credit_customer(db, store_id: int, sale: Sale) -> None:
    """Due sale-এ অবশ্যই trackable customer লাগবে। customer_id না থাকলে phone দিয়ে
    খুঁজে (না পেলে বানিয়ে) link করা হয়, নাহলে due কোনো customer-due report-এ আসত না।"""
    if sale.due_amount is None or sale.due_amount <= 0 or sale.customer_id is not None:
        return
    phone = (sale.customer_phone or "").strip()
    if not phone:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "customer_id or customer_phone is required for due/credit sales",
        )
    norm_phone, variants = _phone_variants(phone)
    customer = await db.scalar(
        select(Customer)
        .where(Customer.store_id == store_id, Customer.phone.in_(variants))
        .order_by(Customer.id)
        .limit(1)
    )
    if customer is None:
        customer = Customer(
            store_id=store_id, name=(sale.customer_name or norm_phone).strip(), phone=norm_phone
        )
        db.add(customer)
        await db.flush()
    sale.customer_id = customer.id


async def serialize_sale(db, sale: Sale) -> SaleResponse:
    """SaleItem-এ unit_name column নেই — unit master থেকে নাম বসানো।"""
    unit_map = await load_unit_map(
        db, sale.store_id, {i.unit_id for i in sale.items if i.unit_id is not None}
    )
    return SaleResponse(
        id=sale.id,
        store_id=sale.store_id,
        branch_id=sale.branch_id,
        invoice_no=sale.invoice_no,
        customer_id=sale.customer_id,
        customer_name=sale.customer_name,
        customer_phone=sale.customer_phone,
        subtotal=sale.subtotal,
        discount=sale.discount,
        total=sale.total,
        paid_amount=sale.paid_amount,
        due_amount=sale.due_amount,
        sold_by=sale.sold_by,
        created_at=sale.created_at,
        items=[
            SaleItemResponse(
                id=i.id,
                medicine_id=i.medicine_id,
                is_external=i.is_external,
                external_name=i.external_name,
                stock_batch_id=i.stock_batch_id,
                unit_id=i.unit_id,
                unit_name=unit_map[i.unit_id].name if i.unit_id in unit_map else None,
                quantity_in_unit=i.quantity_in_unit,
                quantity=i.quantity,
                unit_price=i.unit_price,
                line_total=i.line_total,
            )
            for i in sorted(sale.items, key=lambda x: x.id)
        ],
    )