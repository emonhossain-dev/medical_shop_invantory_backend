from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user, require_permission
from app.models.all_models import (
    Branch,
    Customer,
    HoldSale,
    HoldSaleItem,
    Medicine,
    Sale,
    StockBatch,
    StoreMember,
    User,
)
from app.schemas.hold_sale import (
    HoldSaleCreateRequest,
    HoldSaleItemResponse,
    HoldSaleListItemResponse,
    HoldSaleResponse,
    HoldSaleUpdateRequest,
    ResumeHoldSaleRequest,
)
from app.schemas.sale import SaleResponse
from app.services.branch_scope import assert_branch_access, effective_branch_filter
from app.services.sale_service import (
    SaleLine,
    apply_totals,
    build_sale_items,
    ensure_credit_customer,
    generate_invoice_no,
    serialize_sale,
)
from app.services.unit_service import load_medicine_units, load_unit_map, money, resolve_unit

router = APIRouter(prefix="/sales/hold", tags=["Sales / POS - Hold Bills"])

# Permission: sales:create (hold/update/resume), sales:view (list/get), sales:delete (discard)
# ⚠ HoldSaleItem এ `unit_id` column আর request item schema তে optional `unit_id` থাকতে হবে।
# Resume এ percent discount: ResumeHoldSaleRequest এ optional
#   discount: Decimal | None, discount_type: Literal["amount","percent"] = "amount"
# যোগ করলে এই কোড নিজে থেকেই সেটা ব্যবহার করবে (না থাকলে hold.discount টাকা ধরা হয়)।


def _line_total(quantity: int, unit_price: Optional[Decimal]) -> Optional[Decimal]:
    if unit_price is None:
        return None
    return money(unit_price * quantity)


async def _attach_medicine_names(db: AsyncSession, items: list[HoldSaleItem]) -> dict[int, str]:
    if not items:
        return {}
    ids = {item.medicine_id for item in items}
    result = await db.execute(select(Medicine.id, Medicine.name).where(Medicine.id.in_(ids)))
    return {row.id: row.name for row in result.all()}


def _estimate_totals(hold: HoldSale, names: dict[int, str], unit_map: dict | None = None):
    unit_map = unit_map or {}
    item_responses = []
    subtotal = Decimal("0")
    for item in hold.items:
        line_total = _line_total(item.quantity, item.unit_price)
        if line_total is not None:
            subtotal += line_total
        unit_id = getattr(item, "unit_id", None)
        item_responses.append(
            HoldSaleItemResponse(
                id=item.id,
                medicine_id=item.medicine_id,
                medicine_name=names.get(item.medicine_id),
                stock_batch_id=item.stock_batch_id,
                unit_id=unit_id,
                unit_name=unit_map[unit_id].name if unit_id in unit_map else None,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=line_total,
            )
        )
    total = max(subtotal - hold.discount, Decimal("0"))
    return item_responses, subtotal, total


async def _to_response(db: AsyncSession, hold: HoldSale) -> HoldSaleResponse:
    names = await _attach_medicine_names(db, hold.items)
    unit_ids = {getattr(i, "unit_id", None) for i in hold.items} - {None}
    unit_map = await load_unit_map(db, hold.store_id, unit_ids)
    item_responses, subtotal, total = _estimate_totals(hold, names, unit_map)
    return HoldSaleResponse(
        id=hold.id,
        store_id=hold.store_id,
        branch_id=hold.branch_id,
        label=hold.label,
        customer_id=hold.customer_id,
        customer_name=hold.customer_name,
        customer_phone=hold.customer_phone,
        discount=hold.discount,
        note=hold.note,
        held_by=hold.held_by,
        created_at=hold.created_at,
        updated_at=hold.updated_at,
        resumed_sale_id=hold.resumed_sale_id,
        items=item_responses,
        estimated_subtotal=subtotal,
        estimated_total=total,
    )


async def _get_hold_or_404(
    db: AsyncSession, hold_id: int, member: StoreMember, *, for_update: bool = False
) -> HoldSale:
    stmt = (
        select(HoldSale)
        .options(selectinload(HoldSale.items))
        .where(HoldSale.id == hold_id, HoldSale.store_id == member.store_id)
    )
    if for_update:
        stmt = stmt.with_for_update(of=HoldSale)
    hold = (await db.execute(stmt)).scalar_one_or_none()
    if hold is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hold bill not found")
    assert_branch_access(member, hold.branch_id)
    return hold


def _ensure_not_resumed(hold: HoldSale) -> None:
    if hold.resumed_sale_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This hold bill has already been resumed into a sale")


async def _validate_branch_and_customer(db, store_id, branch_id, customer_id) -> None:
    branch = await db.get(Branch, branch_id)
    if not branch or branch.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")
    if customer_id is not None:
        customer = await db.get(Customer, customer_id)
        if not customer or customer.store_id != store_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")


def _item_unit_id(item, medicine: Medicine) -> int:
    return getattr(item, "unit_id", None) or medicine.base_unit_id


async def _validate_items(db, store_id: int, branch_id: int, items) -> dict[int, Medicine]:
    medicines: dict[int, Medicine] = {}
    for item in items:
        if item.medicine_id in medicines:
            continue
        medicine = await db.get(Medicine, item.medicine_id)
        if not medicine or medicine.store_id != store_id or not medicine.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Medicine {item.medicine_id} not found")
        medicines[item.medicine_id] = medicine

    unit_ids = [_item_unit_id(i, medicines[i.medicine_id]) for i in items]
    unit_map = await load_unit_map(db, store_id, unit_ids)
    mu_map = await load_medicine_units(db, medicines.keys())

    for item in items:
        medicine = medicines[item.medicine_id]
        resolve_unit(medicine, _item_unit_id(item, medicine), unit_map, mu_map, purpose="sale")
        if item.stock_batch_id is not None:
            batch = await db.get(StockBatch, item.stock_batch_id)
            if (
                not batch
                or batch.store_id != store_id
                or batch.branch_id != branch_id
                or batch.medicine_id != item.medicine_id
            ):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"Stock batch {item.stock_batch_id} not found for medicine {item.medicine_id} in this branch",
                )
    return medicines


def _make_items(store_id, items, medicines) -> list[HoldSaleItem]:
    return [
        HoldSaleItem(
            store_id=store_id,
            medicine_id=i.medicine_id,
            stock_batch_id=i.stock_batch_id,
            unit_id=_item_unit_id(i, medicines[i.medicine_id]),
            quantity=i.quantity,
            unit_price=i.unit_price,
        )
        for i in items
    ]


@router.post("", response_model=HoldSaleResponse, status_code=status.HTTP_201_CREATED)
async def create_hold_sale(
    payload: HoldSaleCreateRequest,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "create")),
    db: AsyncSession = Depends(get_db),
):
    """বিল hold — stock কাটে না, reserve-ও করে না।"""
    assert_branch_access(member, payload.branch_id)
    await _validate_branch_and_customer(db, member.store_id, payload.branch_id, payload.customer_id)
    medicines = await _validate_items(db, member.store_id, payload.branch_id, payload.items)

    hold = HoldSale(
        store_id=member.store_id,
        branch_id=payload.branch_id,
        label=payload.label,
        customer_id=payload.customer_id,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        discount=payload.discount,
        note=payload.note,
        held_by=current_user.id,
    )
    hold.items = _make_items(member.store_id, payload.items, medicines)
    db.add(hold)
    await db.flush()
    await db.commit()

    hold = await _get_hold_or_404(db, hold.id, member)
    return await _to_response(db, hold)


@router.get("", response_model=list[HoldSaleListItemResponse])
async def list_hold_sales(
    branch_id: Optional[int] = Query(None),
    include_resumed: bool = Query(False),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "view")),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(HoldSale).options(selectinload(HoldSale.items)).where(HoldSale.store_id == member.store_id)
    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        stmt = stmt.where(HoldSale.branch_id == branch_id)
    if not include_resumed:
        stmt = stmt.where(HoldSale.resumed_sale_id.is_(None))
    holds = list((await db.execute(stmt.order_by(HoldSale.created_at.desc()))).scalars().unique().all())

    responses = []
    for hold in holds:
        names = await _attach_medicine_names(db, hold.items)
        unit_ids = {getattr(i, "unit_id", None) for i in hold.items} - {None}
        unit_map = await load_unit_map(db, member.store_id, unit_ids)
        _, _, total = _estimate_totals(hold, names, unit_map)
        responses.append(
            HoldSaleListItemResponse(
                id=hold.id,
                branch_id=hold.branch_id,
                label=hold.label,
                customer_name=hold.customer_name,
                customer_phone=hold.customer_phone,
                discount=hold.discount,
                item_count=len(hold.items),
                estimated_total=total,
                held_by=hold.held_by,
                created_at=hold.created_at,
                updated_at=hold.updated_at,
            )
        )
    return responses


@router.get("/{hold_id}", response_model=HoldSaleResponse)
async def get_hold_sale(
    hold_id: int,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "view")),
    db: AsyncSession = Depends(get_db),
):
    return await _to_response(db, await _get_hold_or_404(db, hold_id, member))


@router.put("/{hold_id}", response_model=HoldSaleResponse)
async def update_hold_sale(
    hold_id: int,
    payload: HoldSaleUpdateRequest,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "create")),
    db: AsyncSession = Depends(get_db),
):
    hold = await _get_hold_or_404(db, hold_id, member, for_update=True)
    _ensure_not_resumed(hold)

    if payload.customer_id is not None:
        await _validate_branch_and_customer(db, member.store_id, hold.branch_id, payload.customer_id)

    if payload.label is not None:
        hold.label = payload.label
    if payload.customer_id is not None:
        hold.customer_id = payload.customer_id
    if payload.customer_name is not None:
        hold.customer_name = payload.customer_name
    if payload.customer_phone is not None:
        hold.customer_phone = payload.customer_phone
    if payload.discount is not None:
        hold.discount = payload.discount
    if payload.note is not None:
        hold.note = payload.note

    if payload.items is not None:
        medicines = await _validate_items(db, member.store_id, hold.branch_id, payload.items)
        hold.items = _make_items(member.store_id, payload.items, medicines)

    hold.updated_at = datetime.utcnow()
    await db.flush()
    await db.commit()

    return await _to_response(db, await _get_hold_or_404(db, hold_id, member))


@router.delete("/{hold_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_hold_sale(
    hold_id: int,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "delete")),
    db: AsyncSession = Depends(get_db),
):
    hold = await _get_hold_or_404(db, hold_id, member, for_update=True)
    _ensure_not_resumed(hold)
    await db.delete(hold)
    await db.commit()


@router.post("/{hold_id}/resume", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
async def resume_hold_sale(
    hold_id: int,
    payload: ResumeHoldSaleRequest,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "create")),
    db: AsyncSession = Depends(get_db),
):
    """
    Hold বিল -> আসল Sale (create_sale এর হুবহু একই shared logic)।
    Hold row FOR UPDATE লক হয়, তাই double-tap এ দুইবার sale/stock-cut হয় না।
    """
    hold = await _get_hold_or_404(db, hold_id, member, for_update=True)
    _ensure_not_resumed(hold)
    if not hold.items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hold bill has no items")

    await _validate_branch_and_customer(db, member.store_id, hold.branch_id, hold.customer_id)

    sale = Sale(
        store_id=member.store_id,
        branch_id=hold.branch_id,
        invoice_no=payload.invoice_no or generate_invoice_no(member.store_id, hold.branch_id),
        customer_id=hold.customer_id,
        customer_name=hold.customer_name,
        customer_phone=hold.customer_phone,
        sold_by=current_user.id,
    )
    lines = [
        SaleLine(
            medicine_id=i.medicine_id,
            quantity=i.quantity,
            unit_id=getattr(i, "unit_id", None),
            unit_price=i.unit_price,
            stock_batch_id=i.stock_batch_id,
        )
        for i in hold.items
    ]
    override = getattr(payload, "discount", None)
    discount = override if override is not None else hold.discount
    discount_type = getattr(payload, "discount_type", None) or "amount"

    try:
        subtotal = await build_sale_items(
            db, store_id=member.store_id, branch_id=hold.branch_id, lines=lines, sale=sale
        )
        apply_totals(sale, subtotal, discount, discount_type, payload.paid_amount)
        await ensure_credit_customer(db, member.store_id, sale)
    except HTTPException:
        await db.rollback()
        raise

    db.add(sale)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Invoice number already exists for this store, please retry"
        )

    hold.resumed_sale_id = sale.id
    await db.commit()

    result = await db.execute(select(Sale).options(selectinload(Sale.items)).where(Sale.id == sale.id))
    return await serialize_sale(db, result.scalar_one())
