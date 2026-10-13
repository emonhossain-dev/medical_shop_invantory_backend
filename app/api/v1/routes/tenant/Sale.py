from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user, require_permission
from app.models.all_models import Branch, Customer, Sale, StoreMember, User
from app.schemas.sale import (
    AvailableBatchResponse,
    SaleCreateRequest,
    SaleListItemResponse,
    SaleResponse,
)
from app.services.branch_scope import assert_branch_access, effective_branch_filter
from app.services.date_utils import apply_date_range
from app.services.sale_service import (
    SaleLine,
    apply_totals,
    build_sale_items,
    ensure_credit_customer,
    generate_invoice_no,
    get_available_batches,
    serialize_sale,
)

router = APIRouter(prefix="/sales", tags=["Sales / POS"])

# PERMISSION_CATALOG: sales -> create, read, update, delete, void  ("view" নেই — "read")


async def _load_branch_and_customer(db, store_id, branch_id, customer_id):
    branch = await db.get(Branch, branch_id)
    if not branch or branch.store_id != store_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")
    if customer_id is not None:
        customer = await db.get(Customer, customer_id)
        if not customer or customer.store_id != store_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")


@router.get("/stock/{medicine_id}", response_model=list[AvailableBatchResponse])
async def get_available_stock(
    medicine_id: int,
    branch_id: int = Query(...),
    include_expired: bool = Query(False, description="true হলে expired batch-ও আসবে (শুধু দেখার জন্য)"),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """Sellable batches (FEFO order, expired বাদ) — POS item picker এর জন্য।"""
    assert_branch_access(member, branch_id)
    return await get_available_batches(
        db, member.store_id, branch_id, medicine_id, include_expired=include_expired
    )


async def _sale_by_key(db: AsyncSession, store_id: int, key: Optional[str]) -> Optional[Sale]:
    if not key:
        return None
    return await db.scalar(
        select(Sale)
        .options(selectinload(Sale.items))
        .where(Sale.store_id == store_id, Sale.idempotency_key == key)
    )


async def _replay(db: AsyncSession, store_id: int, member_branch: Optional[int], key: Optional[str]):
    """একই Idempotency-Key আবার এলে নতুন sale না বানিয়ে আগেরটাই ফেরত দেয়।"""
    existing = await _sale_by_key(db, store_id, key)
    if existing is None:
        return None
    if member_branch is not None and existing.branch_id != member_branch:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this branch's data")
    return await serialize_sale(db, existing)


@router.post("", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
async def create_sale(
    payload: SaleCreateRequest,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=100,
        description="Client-generated unique key (UUID)। নেটওয়ার্ক retry / double-tap এ duplicate sale ঠেকায়।",
    ),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "create")),
    db: AsyncSession = Depends(get_db),
):
    """
    POS checkout — Sale + SaleItems তৈরি ও stock কমানো এক transaction এ।

    * stock_batch_id না দিলে FEFO (expired batch বাদ), দরকারে একাধিক batch এ ভাগ হয়।
    * quantity = `unit_id` এর unit এ; unit_price = ঐ unit এর দাম।
      Box/Strip একটা batch থেকেই পূর্ণ unit; ছুটকা বিক্রি base unit এ।
    * discount_type "amount"|"percent"; sale.discount সবসময় টাকায় সেভ হয়।
    * due > 0 হলে customer_id অথবা customer_phone লাগবে (phone দিয়ে customer
      auto-link/create হয়)।
    * Branch-bound staff শুধু নিজের branch এ sale করতে পারবে।
    * `Idempotency-Key` header দিলে একই key-তে দ্বিতীয়বার পাঠালে আগের sale-ই ফেরত আসে।
    """
    assert_branch_access(member, payload.branch_id)
    await _load_branch_and_customer(db, member.store_id, payload.branch_id, payload.customer_id)

    # rollback এর পর ORM object expire হয়ে যায় -> দরকারি মান আগেই plain variable এ
    store_id = member.store_id
    member_branch = member.branch_id
    user_id = current_user.id

    replay = await _replay(db, store_id, member_branch, idempotency_key)
    if replay is not None:
        return replay

    auto_invoice = not payload.invoice_no
    sale_id: Optional[int] = None

    for attempt in range(3):
        sale = Sale(
            store_id=store_id,
            branch_id=payload.branch_id,
            invoice_no=payload.invoice_no or generate_invoice_no(store_id, payload.branch_id),
            customer_id=payload.customer_id,
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            idempotency_key=idempotency_key,
            sold_by=user_id,
        )
        lines = [
            SaleLine(
                medicine_id=i.medicine_id,
                quantity=i.quantity,
                unit_id=i.unit_id,
                unit_price=i.unit_price,
                stock_batch_id=i.stock_batch_id,
                is_external=i.is_external,
                external_name=i.external_name,
            )
            for i in payload.items
        ]
        try:
            subtotal = await build_sale_items(
                db, store_id=store_id, branch_id=payload.branch_id, lines=lines, sale=sale
            )
            apply_totals(sale, subtotal, payload.discount, payload.discount_type, payload.paid_amount)
            await ensure_credit_customer(db, store_id, sale)
            db.add(sale)
            await db.flush()
            sale_id = sale.id
            await db.commit()
            break
        except HTTPException:
            await db.rollback()  # যে stock কাটা হয়েছিল তা ফেরত
            raise
        except IntegrityError:
            await db.rollback()
            # (a) একই request দুইবার race করেছে -> বিজয়ীর sale ফেরত
            replay = await _replay(db, store_id, member_branch, idempotency_key)
            if replay is not None:
                return replay
            # (b) auto invoice no collide করলে নতুন no নিয়ে আবার চেষ্টা (stock সহ সব rollback হয়ে গেছে)
            if auto_invoice and attempt < 2:
                continue
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Invoice number already exists for this store, please retry"
            )
        except Exception:
            await db.rollback()
            raise

    result = await db.execute(select(Sale).options(selectinload(Sale.items)).where(Sale.id == sale_id))
    return await serialize_sale(db, result.scalar_one())


@router.get("", response_model=list[SaleListItemResponse])
async def list_sales(
    branch_id: Optional[int] = Query(None),
    customer_id: Optional[int] = Query(None),
    due_only: bool = Query(False),
    date_from: Optional[date] = Query(None, description="YYYY-MM-DD (inclusive, Asia/Dhaka)"),
    date_to: Optional[date] = Query(None, description="YYYY-MM-DD (inclusive, Asia/Dhaka)"),
    q: Optional[str] = Query(None, description="Search invoice no / customer name / phone"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "read")),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Sale).where(Sale.store_id == member.store_id)

    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        stmt = stmt.where(Sale.branch_id == branch_id)
    if customer_id is not None:
        stmt = stmt.where(Sale.customer_id == customer_id)
    if due_only:
        stmt = stmt.where(Sale.due_amount > 0)
    stmt = apply_date_range(stmt, Sale.created_at, date_from, date_to)
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            (Sale.invoice_no.ilike(like, escape="\\"))
            | (Sale.customer_name.ilike(like, escape="\\"))
            | (Sale.customer_phone.ilike(like, escape="\\"))
        )

    stmt = stmt.order_by(Sale.created_at.desc()).limit(limit).offset(offset)
    return (await db.execute(stmt)).scalars().all()


@router.get("/{sale_id:int}", response_model=SaleResponse)
async def get_sale(
    sale_id: int,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("sales", "read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Sale)
        .options(selectinload(Sale.items))
        .where(Sale.id == sale_id, Sale.store_id == member.store_id)
    )
    sale = result.scalar_one_or_none()
    if not sale:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found")
    assert_branch_access(member, sale.branch_id)
    return await serialize_sale(db, sale)