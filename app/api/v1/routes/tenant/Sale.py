import random
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user
from app.models.all_models import (
    Branch,
    Customer,
    Medicine,
    Sale,
    SaleItem,
    StaffRole,
    StockBatch,
    StoreMember,
    User,
)
from app.schemas.sale import (
    AvailableBatchResponse,
    SaleCreateRequest,
    SaleListItemResponse,
    SaleResponse,
)

router = APIRouter(prefix="/sales", tags=["Sales / POS"])

# staff role আলাদা রাখা হয়েছে যদি ভবিষ্যতে view-only staff লাগে
ALLOWED_SALE_ROLES = (StaffRole.owner, StaffRole.manager, StaffRole.pharmacist, StaffRole.cashier)


def _check_can_sell(member: StoreMember) -> None:
    if member.role not in ALLOWED_SALE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to create sales",
        )


def _generate_invoice_no(store_id: int, branch_id: int) -> str:
    suffix = "".join(random.choices("0123456789", k=4))
    return f"INV-{store_id}{branch_id}-{datetime.utcnow():%y%m%d}-{suffix}"


async def _get_available_batches(
    db: AsyncSession,
    store_id: int,
    branch_id: int,
    medicine_id: int,
    lock: bool = False,
) -> list[StockBatch]:
    """FEFO order: nearest expiry first, no-expiry batches last, oldest-received first as tiebreak."""
    stmt = (
        select(StockBatch)
        .where(
            StockBatch.store_id == store_id,
            StockBatch.branch_id == branch_id,
            StockBatch.medicine_id == medicine_id,
            StockBatch.quantity > 0,
        )
        .order_by(
            StockBatch.expiry_date.is_(None),  # batches WITH an expiry date come first
            StockBatch.expiry_date.asc(),
            StockBatch.received_at.asc(),
        )
    )
    if lock:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/stock/{medicine_id}", response_model=list[AvailableBatchResponse])
async def get_available_stock(
    medicine_id: int,
    branch_id: int = Query(...),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """Sellable batches for a medicine at a branch, FEFO order — for the POS item picker."""
    return await _get_available_batches(db, member.store_id, branch_id, medicine_id)


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
async def create_sale(
    payload: SaleCreateRequest,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """
    POS checkout — creates the Sale + SaleItems and decrements stock in one transaction.
    If an item doesn't specify stock_batch_id, it's auto-allocated FEFO and split across
    multiple batches if a single batch doesn't have enough quantity.
    """
    _check_can_sell(member)

    branch = await db.get(Branch, payload.branch_id)
    if not branch or branch.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")

    if payload.customer_id is not None:
        customer = await db.get(Customer, payload.customer_id)
        if not customer or customer.store_id != member.store_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    sale = Sale(
        store_id=member.store_id,
        branch_id=payload.branch_id,
        invoice_no=payload.invoice_no or _generate_invoice_no(member.store_id, payload.branch_id),
        customer_id=payload.customer_id,
        customer_name=payload.customer_name,
        customer_phone=payload.customer_phone,
        discount=payload.discount,
        sold_by=current_user.id,
    )

    subtotal = Decimal("0")

    for item in payload.items:
        medicine = await db.get(Medicine, item.medicine_id)
        if not medicine or medicine.store_id != member.store_id or not medicine.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Medicine {item.medicine_id} not found",
            )

        remaining = item.quantity

        if item.stock_batch_id is not None:
            batch = await db.get(StockBatch, item.stock_batch_id, with_for_update=True)
            if (
                not batch
                or batch.store_id != member.store_id
                or batch.branch_id != payload.branch_id
                or batch.medicine_id != item.medicine_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Stock batch {item.stock_batch_id} not found for this medicine/branch",
                )
            if batch.quantity < remaining:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient stock in batch {batch.id} for {medicine.name} "
                        f"(available {batch.quantity}, requested {remaining})"
                    ),
                )
            unit_price = item.unit_price if item.unit_price is not None else batch.sale_price
            batch.quantity -= remaining
            line_total = (unit_price * remaining).quantize(Decimal("0.01"))
            sale.items.append(
                SaleItem(
                    store_id=member.store_id,
                    medicine_id=item.medicine_id,
                    stock_batch_id=batch.id,
                    quantity=remaining,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )
            subtotal += line_total
            continue

        # auto FEFO allocation — may split across multiple batches
        batches = await _get_available_batches(
            db, member.store_id, payload.branch_id, item.medicine_id, lock=True
        )
        total_available = sum(b.quantity for b in batches)
        if total_available < remaining:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient stock for {medicine.name} "
                    f"(available {total_available}, requested {remaining})"
                ),
            )

        for batch in batches:
            if remaining <= 0:
                break
            take = min(batch.quantity, remaining)
            unit_price = item.unit_price if item.unit_price is not None else batch.sale_price
            batch.quantity -= take
            remaining -= take
            line_total = (unit_price * take).quantize(Decimal("0.01"))
            sale.items.append(
                SaleItem(
                    store_id=member.store_id,
                    medicine_id=item.medicine_id,
                    stock_batch_id=batch.id,
                    quantity=take,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )
            subtotal += line_total

    sale.subtotal = subtotal
    sale.total = subtotal - payload.discount
    if sale.total < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discount cannot exceed subtotal")

    sale.paid_amount = payload.paid_amount
    sale.due_amount = sale.total - payload.paid_amount
    if sale.due_amount < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Paid amount cannot exceed total")

    if sale.due_amount > 0 and not (payload.customer_id or payload.customer_name or payload.customer_phone):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer name/phone (or customer_id) is required for due/credit sales",
        )

    db.add(sale)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invoice number already exists for this store, please retry",
        )

    result = await db.execute(
        select(Sale).options(selectinload(Sale.items)).where(Sale.id == sale.id)
    )
    return result.scalar_one()


@router.get("", response_model=list[SaleListItemResponse])
async def list_sales(
    branch_id: Optional[int] = Query(None),
    customer_id: Optional[int] = Query(None),
    due_only: bool = Query(False),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    q: Optional[str] = Query(None, description="Search invoice no / customer name / phone"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Sale).where(Sale.store_id == member.store_id)

    if branch_id is not None:
        stmt = stmt.where(Sale.branch_id == branch_id)
    if customer_id is not None:
        stmt = stmt.where(Sale.customer_id == customer_id)
    if due_only:
        stmt = stmt.where(Sale.due_amount > 0)
    if date_from is not None:
        stmt = stmt.where(Sale.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Sale.created_at < date_to)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Sale.invoice_no.ilike(like))
            | (Sale.customer_name.ilike(like))
            | (Sale.customer_phone.ilike(like))
        )

    stmt = stmt.order_by(Sale.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{sale_id}", response_model=SaleResponse)
async def get_sale(
    sale_id: int,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Sale)
        .options(selectinload(Sale.items))
        .where(Sale.id == sale_id, Sale.store_id == member.store_id)
    )
    sale = result.scalar_one_or_none()
    if not sale:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")
    return sale