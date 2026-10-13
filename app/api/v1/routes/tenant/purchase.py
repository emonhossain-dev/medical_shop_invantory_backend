import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import (
    Branch,
    DuePaymentMethod,
    Medicine,
    Purchase,
    PurchaseItem,
    StockAdjustment,
    StockAdjustmentReason,
    StockBatch,
    Store,
    StoreMember,
    Supplier,
    SupplierPayment,
    Unit,
)
from app.schemas.purchase import (
    PurchaseCreateRequest,
    PurchaseItemResponse,
    PurchaseListItemResponse,
    PurchaseResponse,
)
from app.schemas.purchase_reports import (
    PurchaseSummaryResponse,
    SupplierPaymentCreateRequest,
    SupplierPaymentResponse,
    SupplierPurchaseSummaryResponse,
)
from app.schemas.stock_adjustment import PurchaseReturnRequest, StockAdjustmentResponse
from app.services.branch_scope import assert_branch_access, effective_branch_filter
from app.services.date_utils import apply_date_range, dhaka_today
from app.services.invoice_pdf import build_purchase_invoice_pdf
from app.services.medicine_resolver import resolve_item_medicines
from app.services.unit_service import (
    load_medicine_units,
    load_unit_map,
    money,
    price_per_base,
    price_per_unit,
    resolve_or_create_unit_for_receiving,
    to_base_quantity,
)

# supplier.py::pay_supplier_allocate যখন বাড়তি (advance) টাকা রাখে, তখন "SPI-" invoice এর
# একটা pseudo Purchase বানায়: total_amount = 0, paid = advance, due = -advance (supplier credit)।
# এটা আসল inventory purchase নয় -> শুধু purchase COUNT থেকে বাদ; টাকার যোগফলে সবসময় ধরা হয়
# (নাহলে /suppliers/due ও এই report এর সংখ্যা মেলে না)।
ADVANCE_PAYMENT_INVOICE_PREFIX = "SPI-"


def _is_real_purchase():
    return ~Purchase.invoice_no.startswith(ADVANCE_PAYMENT_INVOICE_PREFIX)


router = APIRouter(prefix="/purchases")

TAG_CORE = "Purchases"
TAG_INVOICE = "Purchases - Invoice"
TAG_RETURNS = "Purchases - Returns"
TAG_PAYMENTS = "Purchases - Payments"
TAG_REPORTS = "Purchases - Reports"


def _resolve_period_range(period: Optional[str]) -> tuple[Optional[date], Optional[date]]:
    """period shortcut ('today' | 'week' | 'month') -> date range (Asia/Dhaka অনুযায়ী)।"""
    if period is None:
        return None, None
    today = dhaka_today()
    normalized = period.strip().lower()
    if normalized == "today":
        return today, today
    if normalized == "week":
        return today - timedelta(days=today.weekday()), today
    if normalized == "month":
        return today.replace(day=1), today
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "Invalid period. Use one of: today, week, month (or use date_from/date_to instead)",
    )


def purchase_invoice_pdf_url(request: Request, purchase: Optional[Purchase]) -> Optional[str]:
    """Authenticated download endpoint এর URL (আগে public /static/ ছিল -> অন্য store এর
    invoice guess করে নামানো যেত)। Client কে Authorization + X-Store-Id header সহ GET করতে হবে।"""
    if purchase is None:
        return None
    return str(request.url_for("download_purchase_invoice_pdf", purchase_id=purchase.id))


async def _load_item_responses(db: AsyncSession, purchase_id: int) -> list[PurchaseItemResponse]:
    rows = await db.execute(
        select(
            PurchaseItem,
            Medicine.name,
            StockBatch.batch_no,
            StockBatch.expiry_date,
            StockBatch.sale_price,
            Unit.name,
        )
        .join(Medicine, Medicine.id == PurchaseItem.medicine_id)
        .join(Unit, Unit.id == PurchaseItem.unit_id)
        .outerjoin(StockBatch, StockBatch.id == PurchaseItem.stock_batch_id)
        .where(PurchaseItem.purchase_id == purchase_id)
        .order_by(PurchaseItem.id)
    )
    items: list[PurchaseItemResponse] = []
    for pi, medicine_name, batch_no, expiry_date, batch_sale_price, unit_name in rows.all():
        # StockBatch.sale_price base unit-এর; purchase unit-এর দামে ফিরিয়ে দেখানো হচ্ছে।
        # factor = base quantity / unit quantity (MedicineUnit পরে বদলালেও history ঠিক থাকে)
        factor = Decimal(pi.quantity) / Decimal(pi.quantity_in_unit)
        items.append(
            PurchaseItemResponse(
                id=pi.id,
                medicine_id=pi.medicine_id,
                medicine_name=medicine_name,
                stock_batch_id=pi.stock_batch_id,
                batch_no=batch_no,
                expiry_date=expiry_date,
                unit_id=pi.unit_id,
                unit_name=unit_name,
                quantity_in_unit=pi.quantity_in_unit,
                quantity=pi.quantity,
                unit_price=pi.unit_price,
                sale_price=price_per_unit(batch_sale_price, factor) if batch_sale_price is not None else None,
                line_total=money(pi.unit_price * pi.quantity_in_unit),
            )
        )
    return items



# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

@router.post("", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED, tags=[TAG_CORE])
async def create_purchase(
    request: Request,
    payload: PurchaseCreateRequest,
    idempotency_key: Optional[str] = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=100,
        description="Client-generated unique key (UUID)। retry / double-tap এ duplicate stock-in ঠেকায়।",
    ),
    member: StoreMember = Depends(require_permission("purchases", "create")),
    db: AsyncSession = Depends(get_db),
):
    """
    Item এ `medicine_id` (এই store এর Medicine.id) অথবা `master_medicine_id` (catalog) --
    দুটোর একটাই। ⚠ PurchaseItem request schema তে optional `master_medicine_id` field
    যোগ করতে হবে এবং `medicine_id` optional করতে হবে।
    """
    store_id = member.store_id
    member_branch = member.branch_id  # rollback এর পর ORM attribute expire হয়, তাই আগেই রাখা
    assert_branch_access(member, payload.branch_id)

    replay = await _purchase_replay(db, request, store_id, member_branch, idempotency_key)
    if replay is not None:
        return replay

    branch = await db.scalar(select(Branch).where(Branch.id == payload.branch_id, Branch.store_id == store_id))
    if branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid branch_id for this store")

    supplier = None
    if payload.supplier_id is not None:
        supplier = await db.scalar(
            select(Supplier).where(Supplier.id == payload.supplier_id, Supplier.store_id == store_id)
        )
        if supplier is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid supplier_id for this store")

    medicines = await resolve_item_medicines(db, store_id, payload.items)

    # --- unit validate + base conversion + টাকার হিসাব: DB তে কিছু লেখার আগেই ---
    unit_map = await load_unit_map(db, store_id, [i.unit_id for i in payload.items])
    mu_map = await load_medicine_units(db, [m.id for m in medicines])
    resolved_lines = []
    total_amount = Decimal("0")
    for item_in, medicine in zip(payload.items, medicines):
        ru = await resolve_or_create_unit_for_receiving(
            db, store_id, medicine, item_in.unit_id, unit_map, mu_map,
            conversion_factor=item_in.conversion_factor,
            # 422 এ client-এর নিজের id echo হবে (master line হলে master id) — auto-created
            # store medicine rollback হলে ঐ id দিয়ে client আর match করতে পারত না
            requested_medicine_id=item_in.medicine_id or item_in.master_medicine_id or medicine.id,
        )
        base_qty = to_base_quantity(item_in.quantity, ru.factor)
        line_total = money(item_in.unit_price * item_in.quantity)
        total_amount += line_total
        resolved_lines.append((item_in, medicine, ru, base_qty, line_total))

    paid_amount = payload.paid_amount or Decimal("0")
    if paid_amount < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "paid_amount cannot be negative")
    if paid_amount > total_amount:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"paid_amount ({paid_amount}) cannot be greater than total_amount ({total_amount})",
        )
    due_amount = total_amount - paid_amount
    if due_amount > 0 and payload.supplier_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "supplier_id is required for a purchase with due amount"
        )

    # --- 1) Purchase header ---
    purchase = Purchase(
        store_id=store_id,
        branch_id=payload.branch_id,
        supplier_id=payload.supplier_id,
        total_amount=total_amount,
        paid_amount=paid_amount,
        due_amount=due_amount,
        idempotency_key=idempotency_key,
        created_by=member.user_id,
    )
    db.add(purchase)
    await db.flush()
    purchase.invoice_no = f"PUR-{purchase.id:06d}"

    # --- 2) Items + StockBatch (StockBatch সবসময় base unit এ) ---
    item_responses: list[PurchaseItemResponse] = []
    for item_in, medicine, ru, base_qty, line_total in resolved_lines:
        batch = StockBatch(
            store_id=store_id,
            branch_id=payload.branch_id,
            medicine_id=medicine.id,
            batch_no=item_in.batch_no,
            quantity=base_qty,
            purchase_price=price_per_base(item_in.unit_price, ru.factor),
            sale_price=price_per_base(item_in.sale_price, ru.factor),
            expiry_date=item_in.expiry_date,
        )
        db.add(batch)
        await db.flush()

        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            store_id=store_id,
            medicine_id=medicine.id,
            stock_batch_id=batch.id,
            unit_id=ru.unit_id,
            quantity_in_unit=item_in.quantity,
            quantity=base_qty,
            unit_price=item_in.unit_price,
        )
        db.add(purchase_item)
        await db.flush()

        item_responses.append(
            PurchaseItemResponse(
                id=purchase_item.id,
                medicine_id=medicine.id,
                medicine_name=medicine.name,
                stock_batch_id=batch.id,
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                unit_id=ru.unit_id,
                unit_name=ru.unit_name,
                quantity_in_unit=item_in.quantity,
                quantity=base_qty,
                unit_price=item_in.unit_price,
                sale_price=item_in.sale_price,
                line_total=line_total,
            )
        )

    # --- 3) initial payment record ---
    if paid_amount > 0 and payload.supplier_id is not None:
        method = DuePaymentMethod.cash
        if payload.payment_method:
            try:
                method = DuePaymentMethod(payload.payment_method.lower())
            except ValueError:
                method = DuePaymentMethod.other
        db.add(
            SupplierPayment(
                store_id=store_id,
                supplier_id=payload.supplier_id,
                purchase_id=purchase.id,
                amount=paid_amount,
                method=method,
                reference_no=payload.payment_reference,
                note=payload.payment_note,
                paid_by=member.user_id,
            )
        )

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # একই Idempotency-Key এর দুই request race করলে হেরে যাওয়াটা বিজয়ীর purchase ফেরত পায়
        replay = await _purchase_replay(db, request, store_id, member_branch, idempotency_key)
        if replay is not None:
            return replay
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Could not save the purchase because of a conflicting update. Please retry."
        )
    await db.refresh(purchase)

    # PDF আর এখানে বানানো/save হয় না (আগে fail করলে commit এর পরেও 500 আসত -> retry এ
    # stock duplicate)। এখন on-demand: GET /purchases/{id}/invoice/pdf
    return PurchaseResponse(
        id=purchase.id,
        store_id=store_id,
        branch_id=purchase.branch_id,
        supplier_id=purchase.supplier_id,
        supplier_name=supplier.name if supplier else None,
        invoice_no=purchase.invoice_no,
        total_amount=total_amount,
        paid_amount=paid_amount,
        due_amount=due_amount,
        created_by=purchase.created_by,
        created_at=purchase.created_at,
        items=item_responses,
        pdf_url=purchase_invoice_pdf_url(request, purchase),
    )


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

async def _build_purchase_response(
    db: AsyncSession, request: Request, purchase: Purchase
) -> PurchaseResponse:
    supplier_name = None
    if purchase.supplier_id is not None:
        supplier_name = await db.scalar(select(Supplier.name).where(Supplier.id == purchase.supplier_id))
    items = await _load_item_responses(db, purchase.id)
    return PurchaseResponse(
        id=purchase.id,
        store_id=purchase.store_id,
        branch_id=purchase.branch_id,
        supplier_id=purchase.supplier_id,
        supplier_name=supplier_name,
        invoice_no=purchase.invoice_no,
        total_amount=purchase.total_amount,
        paid_amount=purchase.paid_amount or Decimal("0"),
        due_amount=purchase.due_amount or Decimal("0"),
        created_by=purchase.created_by,
        created_at=purchase.created_at,
        items=items,
        pdf_url=purchase_invoice_pdf_url(request, purchase),
    )


async def _purchase_by_key(db: AsyncSession, store_id: int, key: Optional[str]) -> Optional[Purchase]:
    if not key:
        return None
    return await db.scalar(
        select(Purchase).where(Purchase.store_id == store_id, Purchase.idempotency_key == key)
    )


async def _purchase_replay(
    db: AsyncSession, request: Request, store_id: int, member_branch: Optional[int], key: Optional[str]
) -> Optional[PurchaseResponse]:
    existing = await _purchase_by_key(db, store_id, key)
    if existing is None:
        return None
    if member_branch is not None and existing.branch_id != member_branch:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this branch's data")
    return await _build_purchase_response(db, request, existing)


@router.get("", response_model=list[PurchaseListItemResponse], tags=[TAG_CORE])
async def list_purchases(
    request: Request,
    branch_id: int | None = Query(default=None),
    supplier_id: int | None = Query(default=None),
    date_from: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    date_to: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    সব purchase-এর list — branch/supplier দিয়ে filter করা যায়, এবং এখন date_from/date_to
    দিয়েও filter করা যায় (যেমন: শুধু আজকের বা এই মাসের purchase বের করতে)।
    """
    item_count_subq = (
        select(func.count(PurchaseItem.id))
        .where(PurchaseItem.purchase_id == Purchase.id)
        .correlate(Purchase)
        .scalar_subquery()
    )

    query = (
        select(
            Purchase,
            Supplier.name.label("supplier_name"),
            item_count_subq.label("item_count"),
        )
        .outerjoin(Supplier, Supplier.id == Purchase.supplier_id)
        .where(Purchase.store_id == member.store_id)
        .order_by(Purchase.created_at.desc())
    )

    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        query = query.where(Purchase.branch_id == branch_id)
    if supplier_id is not None:
        query = query.where(Purchase.supplier_id == supplier_id)
    query = apply_date_range(query, Purchase.created_at, date_from, date_to)

    result = await db.execute(query.offset(skip).limit(limit))

    return [
        PurchaseListItemResponse(
            id=row.Purchase.id,
            branch_id=row.Purchase.branch_id,
            supplier_id=row.Purchase.supplier_id,
            supplier_name=row.supplier_name,
            invoice_no=row.Purchase.invoice_no,
            total_amount=row.Purchase.total_amount,
            paid_amount=row.Purchase.paid_amount or Decimal("0"),
            due_amount=row.Purchase.due_amount or Decimal("0"),
            item_count=row.item_count,
            created_at=row.Purchase.created_at,
            pdf_url=purchase_invoice_pdf_url(request, row.Purchase),
        )
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------

@router.get("/{purchase_id}", response_model=PurchaseResponse, tags=[TAG_CORE])
async def get_purchase(
    request: Request,
    purchase_id: int,
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.id == purchase_id, Purchase.store_id == member.store_id
        )
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    assert_branch_access(member, purchase.branch_id)
    return await _build_purchase_response(db, request, purchase)


# ---------------------------------------------------------------------------
# INVOICE PDF DOWNLOAD
# ---------------------------------------------------------------------------

@router.get("/{purchase_id}/invoice/pdf", tags=[TAG_INVOICE])
async def download_purchase_invoice_pdf(
    request: Request,
    purchase_id: int,
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """নির্দিষ্ট purchase-এর invoice PDF সরাসরি download/view করার API (supplier.py এর invoice pdf endpoint এর মতো)।"""
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.id == purchase_id, Purchase.store_id == member.store_id
        )
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    assert_branch_access(member, purchase.branch_id)

    supplier = None
    if purchase.supplier_id is not None:
        supplier = await db.get(Supplier, purchase.supplier_id)

    store = await db.get(Store, member.store_id)

    items = await _load_item_responses(db, purchase.id)

    pdf_bytes = build_purchase_invoice_pdf(store, supplier, purchase, items)

    filename = f"invoice-{purchase.invoice_no or purchase.id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# PURCHASE RETURN — supplier কে stock ফেরত (partial return সাপোর্ট করে)
# ---------------------------------------------------------------------------

@router.post(
    "/{purchase_id}/items/{item_id}/return",
    response_model=StockAdjustmentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=[TAG_RETURNS],
)
async def return_purchase_item(
    purchase_id: int,
    item_id: int,
    payload: PurchaseReturnRequest,
    member: StoreMember = Depends(require_permission("stock", "adjust")),
    db: AsyncSession = Depends(get_db),
):
    """
    একটা purchase item এর নির্দিষ্ট quantity সাপ্লায়ারকে ফেরত।
    Partial return সাপোর্ট করে (পুরো quantity ফেরত না দিয়ে অংশ দেওয়া যায়)।

    NOTE (unit): payload.quantity সবসময় base unit-এ (StockBatch.quantity / item.quantity এর মতোই)।
    StockAdjustment.unit_price = base unit-এর purchase price (batch.purchase_price) — item.unit_price
    নয়, কারণ সেটা purchase unit-এর (Box ইত্যাদি) দাম।

    Rule: এখনো stock এ যতটুকু বাকি আছে (StockBatch.quantity) তার বেশি ফেরত দেওয়া যাবে না
    — কারণ ইতিমধ্যে বিক্রি হয়ে যাওয়া quantity ফেরত দেওয়ার কোনো মানে নেই।

    Concurrency: batch row FOR UPDATE লক করা হয় already_returned গণনার আগেই —
    একই batch এ একসাথে দুইটা return/sale এলে দ্বিতীয়টা প্রথমটার commit পর্যন্ত
    wait করবে, ফলে stale quantity দিয়ে হিসাব হওয়ার সুযোগ থাকে না।
    """
    purchase = await db.scalar(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.store_id == member.store_id)
        .with_for_update()
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    assert_branch_access(member, purchase.branch_id)

    if payload.quantity <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "quantity must be greater than 0")

    item = await db.scalar(
        select(PurchaseItem).where(
            PurchaseItem.id == item_id, PurchaseItem.purchase_id == purchase.id
        )
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase item not found")

    if item.stock_batch_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This item has no linked stock batch")

    # --- FIX: row lock নেওয়া হচ্ছে এখানে, already_returned গণনার আগেই ---
    batch = await db.scalar(
        select(StockBatch)
        .where(StockBatch.id == item.stock_batch_id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock batch not found")

    # ইতিমধ্যে কতটুকু return হয়ে গেছে এই item থেকে — সেটা হিসাব করে বাকি returnable বের করা
    already_returned = await db.scalar(
        select(func.coalesce(func.sum(-StockAdjustment.quantity_change), 0)).where(
            StockAdjustment.purchase_item_id == item.id,
            StockAdjustment.reason == StockAdjustmentReason.purchase_return,
        )
    )
    max_returnable = min(batch.quantity, item.quantity - already_returned)

    if payload.quantity > max_returnable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot return {payload.quantity} — max returnable right now is {max_returnable} "
            f"(current batch stock: {batch.quantity}, already returned: {already_returned})",
        )

    batch.quantity -= payload.quantity

    # Supplier এর কাছে পাওনা কমাও: ফেরত পণ্যের মূল্য purchase.total থেকে বাদ, due = total - paid
    # (due negative হলে সেটা supplier এর কাছে আমাদের credit)। মূল্য = item মোটের আনুপাতিক অংশ।
    item_total = money(item.unit_price * item.quantity_in_unit)
    # cumulative rounding: সব partial return মিলে ঠিক item_total হয় (৩ বারে ১ পয়সা এদিক-ওদিক হবে না)
    returned_value = money(item_total * (already_returned + payload.quantity) / item.quantity) - money(
        item_total * already_returned / item.quantity
    )
    purchase.total_amount = (purchase.total_amount or Decimal("0")) - returned_value
    purchase.due_amount = purchase.total_amount - (purchase.paid_amount or Decimal("0"))

    adjustment = StockAdjustment(
        store_id=member.store_id,
        branch_id=purchase.branch_id,
        medicine_id=item.medicine_id,
        stock_batch_id=batch.id,
        reason=StockAdjustmentReason.purchase_return,
        quantity_change=-payload.quantity,
        unit_price=batch.purchase_price,
        purchase_id=purchase.id,
        purchase_item_id=item.id,
        notes=payload.notes,
        created_by=member.user_id,
    )
    db.add(adjustment)
    await db.commit()
    await db.refresh(adjustment)

    medicine_name = await db.scalar(select(Medicine.name).where(Medicine.id == item.medicine_id))

    return StockAdjustmentResponse(
        id=adjustment.id,
        branch_id=adjustment.branch_id,
        medicine_id=adjustment.medicine_id,
        medicine_name=medicine_name,
        stock_batch_id=adjustment.stock_batch_id,
        batch_no=batch.batch_no,
        reason=adjustment.reason,
        quantity_change=adjustment.quantity_change,
        unit_price=adjustment.unit_price,
        purchase_id=adjustment.purchase_id,
        purchase_item_id=adjustment.purchase_item_id,
        notes=adjustment.notes,
        created_by=adjustment.created_by,
        created_at=adjustment.created_at,
        batch_quantity_after=batch.quantity,
    )

# ---------------------------------------------------------------------------
# PAYMENTS — supplier due এর বিপরীতে নতুন payment রেকর্ড করা + payment history
# ---------------------------------------------------------------------------

def _serialize_payment(
    payment: SupplierPayment,
    supplier_name: Optional[str],
    invoice_no: Optional[str],
) -> SupplierPaymentResponse:
    method_value = payment.method.value if hasattr(payment.method, "value") else str(payment.method)
    return SupplierPaymentResponse(
        id=payment.id,
        store_id=payment.store_id,
        supplier_id=payment.supplier_id,
        supplier_name=supplier_name,
        purchase_id=payment.purchase_id,
        invoice_no=invoice_no,
        amount=payment.amount,
        method=method_value,
        reference_no=payment.reference_no,
        note=payment.note,
        paid_by=payment.paid_by,
        created_at=payment.created_at,
    )


@router.post(
    "/{purchase_id}/payments",
    response_model=SupplierPaymentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=[TAG_PAYMENTS],
)
async def add_purchase_payment(
    purchase_id: int,
    payload: SupplierPaymentCreateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "update")),
    db: AsyncSession = Depends(get_db),
):
    """
    কোনো purchase-এর বাকি due-এর বিপরীতে নতুন payment রেকর্ড করা।
    যেমন: purchase করার সময় partial payment করা হয়েছিল, এখন বাকি due partial/পুরোপুরি শোধ করা হচ্ছে।

    Rule: payment amount, বর্তমান due_amount থেকে বেশি হতে পারবে না।
    """
    purchase = await db.scalar(
        select(Purchase)
        .where(Purchase.id == purchase_id, Purchase.store_id == member.store_id)
        .with_for_update()  # parallel payment এ overpay ঠেকায়
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    assert_branch_access(member, purchase.branch_id)

    if payload.amount <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "amount must be greater than 0")

    if purchase.supplier_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This purchase has no supplier linked — cannot record a supplier payment against it",
        )

    current_due = purchase.due_amount or Decimal("0")
    if payload.amount > current_due:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Payment amount ({payload.amount}) is more than the current due ({current_due})",
        )

    method = DuePaymentMethod.cash
    if payload.method:
        try:
            method = DuePaymentMethod(payload.method.lower())
        except ValueError:
            method = DuePaymentMethod.other

    payment = SupplierPayment(
        store_id=member.store_id,
        supplier_id=purchase.supplier_id,
        purchase_id=purchase.id,
        amount=payload.amount,
        method=method,
        reference_no=payload.reference_no,
        note=payload.note,
        paid_by=member.user_id,
    )
    db.add(payment)

    purchase.paid_amount = (purchase.paid_amount or Decimal("0")) + payload.amount
    purchase.due_amount = current_due - payload.amount

    await db.commit()
    await db.refresh(payment)
    await db.refresh(purchase)

    supplier_name = await db.scalar(select(Supplier.name).where(Supplier.id == purchase.supplier_id))

    return _serialize_payment(payment, supplier_name, purchase.invoice_no)


@router.get(
    "/{purchase_id}/payments",
    response_model=list[SupplierPaymentResponse],
    tags=[TAG_PAYMENTS],
)
async def list_purchase_payments(
    purchase_id: int,
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """একটা নির্দিষ্ট purchase-এর বিপরীতে এখন পর্যন্ত যত payment হয়েছে, তার list।"""
    purchase = await db.scalar(
        select(Purchase).where(Purchase.id == purchase_id, Purchase.store_id == member.store_id)
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    assert_branch_access(member, purchase.branch_id)

    rows = await db.execute(
        select(SupplierPayment, Supplier.name.label("supplier_name"))
        .outerjoin(Supplier, Supplier.id == SupplierPayment.supplier_id)
        .where(SupplierPayment.purchase_id == purchase.id)
        .order_by(SupplierPayment.created_at.desc())
    )

    return [
        _serialize_payment(payment, supplier_name, purchase.invoice_no)
        for payment, supplier_name in rows.all()
    ]


@router.get(
    "/payments/history",
    response_model=list[SupplierPaymentResponse],
    tags=[TAG_PAYMENTS],
)
async def list_all_payments(
    supplier_id: Optional[int] = Query(default=None),
    date_from: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    date_to: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    পুরো store-এর সব supplier payment এর history — এক জায়গায়।
    supplier_id দিয়ে filter করলে "একজন সাপ্লায়ারকে এ পর্যন্ত কত/কবে কবে পেমেন্ট দিলাম" বের করা যায়,
    date_from/date_to দিয়ে "এই মাসে মোট কত পেমেন্ট দিলাম" জাতীয় প্রশ্নেরও উত্তর পাওয়া যায়।
    """
    query = (
        select(SupplierPayment, Supplier.name.label("supplier_name"), Purchase.invoice_no)
        .outerjoin(Supplier, Supplier.id == SupplierPayment.supplier_id)
        .outerjoin(Purchase, Purchase.id == SupplierPayment.purchase_id)
        .where(SupplierPayment.store_id == member.store_id)
        .order_by(SupplierPayment.created_at.desc())
    )
    if member.branch_id is not None:
        query = query.where(Purchase.branch_id == member.branch_id)
    if supplier_id is not None:
        query = query.where(SupplierPayment.supplier_id == supplier_id)
    query = apply_date_range(query, SupplierPayment.created_at, date_from, date_to)

    rows = await db.execute(query.offset(skip).limit(limit))

    return [
        _serialize_payment(payment, supplier_name, invoice_no)
        for payment, supplier_name, invoice_no in rows.all()
    ]


# ---------------------------------------------------------------------------
# REPORTS — overall summary + supplier-wise summary/history
# ---------------------------------------------------------------------------

@router.get(
    "/reports/summary",
    response_model=PurchaseSummaryResponse,
    tags=[TAG_REPORTS],
)
async def get_purchase_summary(
    period: Optional[str] = Query(
        default=None, description="Shortcut: today | week | month. দেওয়া থাকলে এবং date_from/date_to না দিলে সেটাই ব্যবহার হবে।"
    ),
    date_from: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    date_to: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    branch_id: Optional[int] = Query(default=None),
    supplier_id: Optional[int] = Query(default=None),
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    এক নজরে: নির্দিষ্ট সময়ে (আজ/এই সপ্তাহ/এই মাস/custom range) মোট কয়টা purchase হয়েছে,
    মোট টাকার purchase, মোট paid, মোট due — কত।

    উদাহরণ: /purchases/reports/summary?period=month
            /purchases/reports/summary?date_from=2026-09-01&date_to=2026-09-24
    """
    if date_from is None and date_to is None and period is not None:
        date_from, date_to = _resolve_period_range(period)

    # count শুধু আসল purchase; টাকার যোগফলে advance (SPI) ও ধরা হয় -> /suppliers/due এর সাথে মেলে
    query = select(
        func.count(Purchase.id).filter(_is_real_purchase()),
        func.coalesce(func.sum(Purchase.total_amount), 0),
        func.coalesce(func.sum(Purchase.paid_amount), 0),
        func.coalesce(func.sum(Purchase.due_amount), 0),
    ).where(Purchase.store_id == member.store_id)

    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        query = query.where(Purchase.branch_id == branch_id)
    if supplier_id is not None:
        query = query.where(Purchase.supplier_id == supplier_id)
    query = apply_date_range(query, Purchase.created_at, date_from, date_to)

    count, total_amount, total_paid, total_due = (await db.execute(query)).one()

    return PurchaseSummaryResponse(
        date_from=date_from,
        date_to=date_to,
        total_purchase_count=count,
        total_amount=total_amount,
        total_paid=total_paid,
        total_due=total_due,
    )


@router.get(
    "/suppliers/{supplier_id}/summary",
    response_model=SupplierPurchaseSummaryResponse,
    tags=[TAG_REPORTS],
)
async def get_supplier_purchase_summary(
    request: Request,
    supplier_id: int,
    date_from: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    date_to: Optional[date] = Query(default=None, description="YYYY-MM-DD (inclusive)"),
    branch_id: Optional[int] = Query(default=None),
    member: StoreMember = Depends(require_permission("purchases", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    একটা নির্দিষ্ট supplier থেকে এ পর্যন্ত কী কী purchase করা হয়েছে — full history,
    সাথে সেই supplier-এর total purchase / total paid / total due — একসাথে।
    """
    supplier = await db.scalar(
        select(Supplier).where(Supplier.id == supplier_id, Supplier.store_id == member.store_id)
    )
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found")

    item_count_subq = (
        select(func.count(PurchaseItem.id))
        .where(PurchaseItem.purchase_id == Purchase.id)
        .correlate(Purchase)
        .scalar_subquery()
    )

    query = (
        select(Purchase, item_count_subq.label("item_count"))
        .where(Purchase.store_id == member.store_id, Purchase.supplier_id == supplier_id)
        .order_by(Purchase.created_at.desc())
    )
    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        query = query.where(Purchase.branch_id == branch_id)
    query = apply_date_range(query, Purchase.created_at, date_from, date_to)

    rows = (await db.execute(query)).all()

    purchases = [
        PurchaseListItemResponse(
            id=row.Purchase.id,
            branch_id=row.Purchase.branch_id,
            supplier_id=row.Purchase.supplier_id,
            supplier_name=supplier.name,
            invoice_no=row.Purchase.invoice_no,
            total_amount=row.Purchase.total_amount,
            paid_amount=row.Purchase.paid_amount or Decimal("0"),
            due_amount=row.Purchase.due_amount or Decimal("0"),
            item_count=row.item_count,
            created_at=row.Purchase.created_at,
            pdf_url=purchase_invoice_pdf_url(request, row.Purchase),
        )
        for row in rows
    ]

    # Totals: advance-payment-only "fake" purchase রেকর্ড (দেখুন
    # ADVANCE_PAYMENT_INVOICE_PREFIX) বাদ দিয়ে হিসাব করা হচ্ছে, যাতে প্রকৃত
    # inventory purchase-এর total এর সাথে advance payment মিশে না যায়। তবে
    # ledger-এ (purchases list) সব রেকর্ডই দেখানো হচ্ছে, শুধু total বাদ পড়ছে।
    real_purchase_rows = [
        row for row in rows
        if not (row.Purchase.invoice_no or "").startswith(ADVANCE_PAYMENT_INVOICE_PREFIX)
    ]
    # টাকার যোগফলে সব row (advance/SPI সহ) -> supplier ledger/due এর সাথে মেলে; count শুধু আসল purchase
    total_amount = sum((row.Purchase.total_amount or Decimal("0") for row in rows), Decimal("0"))
    total_paid = sum((row.Purchase.paid_amount or Decimal("0") for row in rows), Decimal("0"))
    total_due = sum((row.Purchase.due_amount or Decimal("0") for row in rows), Decimal("0"))

    return SupplierPurchaseSummaryResponse(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        total_purchase_count=len(real_purchase_rows),
        total_amount=total_amount,
        total_paid=total_paid,
        total_due=total_due,
        purchases=purchases,
    )