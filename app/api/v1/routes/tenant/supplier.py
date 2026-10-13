import io
import random
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import (
    Branch, DuePaymentMethod, Purchase, PurchaseRequisition, Store, StockReceipt,
    StoreMember, Supplier, SupplierPayment,
)
from app.services.branch_scope import assert_branch_access
from app.services.date_utils import apply_date_range, to_dhaka_date
from app.services.invoice_pdf import build_supplier_payment_pdf
from app.repositories.base import BaseRepository
from app.repositories.deps import get_repository
from app.schemas.supplier import SupplierCreateRequest, SupplierUpdateRequest, SupplierResponse
from app.schemas.supplier_payment import (
    SupplierPaymentCreateRequest,
    SupplierPaymentResponse,
    SupplierPaymentWithInvoiceResponse,
    SupplierPaymentListItem,
    SupplierLedgerSummary,
    SupplierLedgerEntry,
    SupplierLedgerHistoryResponse,
    SupplierInvoiceResponse,
    SupplierPaymentUpdateRequest,
    SupplierDueItem,
    SupplierDueListResponse,
    SupplierDueSummary,
    SupplierDueAgingItem,
    SupplierDueAgingResponse,
)

# Swagger-e ei order-ei tag gulo dekhabe (main.py-r openapi_tags-e-o ei order rakho)
TAG_SUPPLIERS = "Suppliers"
TAG_PAYMENTS = "Supplier Payments"
TAG_INVOICES = "Supplier Invoices"
TAG_DUE = "Supplier Due & Aging"
TAG_LEDGER = "Supplier Ledger"

router = APIRouter(prefix="/suppliers")


def _like(search: str) -> str:
    """% _ \\ কে literal ধরে LIKE pattern (search="50%" যেন সব row না আনে)।"""
    esc = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _purchase_join(member: StoreMember):
    """Supplier ↔ Purchase join; branch-bound staff শুধু নিজের branch-এর purchase দেখবে।"""
    cond = (Purchase.supplier_id == Supplier.id) & (Purchase.store_id == Supplier.store_id)
    if member.branch_id is not None:
        cond = cond & (Purchase.branch_id == member.branch_id)
    return cond


@router.post("", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED,
             tags=[TAG_SUPPLIERS], summary="Create supplier")
async def create_supplier(
    data: SupplierCreateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "create")),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    supplier = await repo.create(
        name=data.name,
        phone=data.phone,
        address=data.address,
    )
    await repo.db.commit()
    return SupplierResponse.model_validate(supplier)


@router.get("", response_model=list[SupplierResponse], tags=[TAG_SUPPLIERS], summary="List suppliers")
async def list_suppliers(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by supplier name"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = select(Supplier).where(Supplier.store_id == member.store_id)
    if search:
        query = query.where(Supplier.name.ilike(_like(search), escape="\\"))
    query = query.order_by(Supplier.name).offset(skip).limit(limit)

    result = await db.execute(query)
    suppliers = result.scalars().all()
    return [SupplierResponse.model_validate(s) for s in suppliers]


# ---------------------------------------------------------------------------
# SUPPLIER DUE — list / total / aging (NEW)
# ---------------------------------------------------------------------------
# NOTE: এই ৩টা route কে "/{supplier_id}" এর আগে রাখা হয়েছে (যদিও supplier_id
# int টাইপ হওয়ায় "due" ওখানে ম্যাচ করবে না, তবু clarity-র জন্য আগে রাখা ভালো)।

@router.get("/due", response_model=SupplierDueListResponse, tags=[TAG_DUE], summary="Supplier-wise due list")
async def list_supplier_dues(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Supplier name দিয়ে search"),
    only_due: bool = Query(True, description="True (default) দিলে শুধু যেসব supplier-এর due > 0 তারাই আসবে"),
    sort: str = Query(
        "due_desc",
        description="due_desc (বেশি due আগে) / due_asc / name",
    ),
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    কোন supplier-এর কাছে কত টাকা due — সব supplier একসাথে, এক call-এ।

    প্রতিটা supplier-এর total_purchase / total_paid / total_due, সাথে সবচেয়ে
    পুরনো unpaid invoice কবে হয়েছিল সেটাও (oldest_due_date / oldest_due_days) থাকে।
    সাথে overall total_due_amount ও supplier_count (quick summary হিসেবে)।
    """
    oldest_due_expr = func.min(
        case((Purchase.due_amount > 0, Purchase.created_at), else_=None)
    )

    query = (
        select(
            Supplier.id,
            Supplier.name,
            Supplier.phone,
            func.coalesce(func.sum(Purchase.total_amount), 0).label("total_purchase"),
            func.coalesce(func.sum(Purchase.paid_amount), 0).label("total_paid"),
            func.coalesce(func.sum(Purchase.due_amount), 0).label("total_due"),
            oldest_due_expr.label("oldest_due_date"),
        )
        .select_from(Supplier)
        .outerjoin(Purchase, _purchase_join(member))
        .where(Supplier.store_id == member.store_id)
        .group_by(Supplier.id, Supplier.name, Supplier.phone)
    )

    if search:
        query = query.where(Supplier.name.ilike(_like(search), escape="\\"))
    if only_due:
        query = query.having(func.coalesce(func.sum(Purchase.due_amount), 0) > 0)

    if sort == "due_asc":
        query = query.order_by(func.coalesce(func.sum(Purchase.due_amount), 0).asc())
    elif sort == "name":
        query = query.order_by(Supplier.name.asc())
    else:  # due_desc (default)
        query = query.order_by(func.coalesce(func.sum(Purchase.due_amount), 0).desc())

    # overall total (filter ছাড়া, store-wide সব due-এর যোগফল; advance/credit ঋণাত্মক due সহ)
    total_query = select(func.coalesce(func.sum(Purchase.due_amount), 0)).where(
        Purchase.store_id == member.store_id
    )
    if member.branch_id is not None:
        total_query = total_query.where(Purchase.branch_id == member.branch_id)
    total_result = await db.execute(total_query)
    total_due_amount = total_result.scalar_one()

    # filter অনুযায়ী মোট supplier সংখ্যা (আগে শুধু এই page এর সংখ্যা ছিল)
    supplier_total = (
        await db.execute(select(func.count()).select_from(query.order_by(None).subquery()))
    ).scalar_one()

    paged_query = query.offset(skip).limit(limit)
    result = await db.execute(paged_query)
    rows = result.all()

    now = datetime.now(timezone.utc)
    items = [
        SupplierDueItem(
            supplier_id=row.id,
            supplier_name=row.name,
            phone=row.phone,
            total_purchase_amount=row.total_purchase,
            total_paid_amount=row.total_paid,
            total_due_amount=row.total_due,
            oldest_due_date=row.oldest_due_date,
            oldest_due_days=(now - row.oldest_due_date).days if row.oldest_due_date else None,
        )
        for row in rows
    ]

    return SupplierDueListResponse(
        total_due_amount=total_due_amount,
        supplier_count=supplier_total,
        items=items,
    )


@router.get("/due/summary", response_model=SupplierDueSummary, tags=[TAG_DUE], summary="Total due summary")
async def supplier_due_summary(
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    পুরো store-এর সব supplier মিলিয়ে মোট due — এক নম্বরে overview
    (dashboard card-এ বসানোর জন্য)।
    """
    totals_query = select(
        func.coalesce(func.sum(Purchase.total_amount), 0),
        func.coalesce(func.sum(Purchase.paid_amount), 0),
        func.coalesce(func.sum(Purchase.due_amount), 0),
    ).where(Purchase.store_id == member.store_id)
    if member.branch_id is not None:
        totals_query = totals_query.where(Purchase.branch_id == member.branch_id)
    totals_result = await db.execute(totals_query)
    total_purchase, total_paid, total_due = totals_result.one()

    total_suppliers_query = select(func.count(Supplier.id)).where(
        Supplier.store_id == member.store_id
    )
    total_suppliers = (await db.execute(total_suppliers_query)).scalar_one()

    suppliers_with_due_query = (
        select(func.count(func.distinct(Purchase.supplier_id)))
        .where(Purchase.store_id == member.store_id, Purchase.due_amount > 0)
    )
    if member.branch_id is not None:
        suppliers_with_due_query = suppliers_with_due_query.where(Purchase.branch_id == member.branch_id)
    suppliers_with_due = (await db.execute(suppliers_with_due_query)).scalar_one()

    return SupplierDueSummary(
        total_suppliers=total_suppliers,
        suppliers_with_due=suppliers_with_due,
        total_purchase_amount=total_purchase,
        total_paid_amount=total_paid,
        total_due_amount=total_due,
        generated_at=datetime.now(timezone.utc),
    )


@router.get("/due/aging", response_model=SupplierDueAgingResponse, tags=[TAG_DUE], summary="Due aging report")
async def supplier_due_aging(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Supplier name দিয়ে search"),
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Due aging report — কোন supplier-এর due কত দিন ধরে বকেয়া আছে, bucket অনুযায়ী:
    0-30 দিন / 31-60 দিন / 61-90 দিন / 90+ দিন। প্রতিটা bucket-এ ওই সময়ের
    unpaid purchase-গুলোর due_amount যোগ করে দেখানো হয় (per supplier)।
    """
    age_days = func.extract("day", func.now() - Purchase.created_at)

    bucket_0_30 = func.coalesce(
        func.sum(case((age_days <= 30, Purchase.due_amount), else_=0)), 0
    )
    bucket_31_60 = func.coalesce(
        func.sum(case(((age_days > 30) & (age_days <= 60), Purchase.due_amount), else_=0)), 0
    )
    bucket_61_90 = func.coalesce(
        func.sum(case(((age_days > 60) & (age_days <= 90), Purchase.due_amount), else_=0)), 0
    )
    bucket_90_plus = func.coalesce(
        func.sum(case((age_days > 90, Purchase.due_amount), else_=0)), 0
    )
    oldest_due_expr = func.min(Purchase.created_at)

    query = (
        select(
            Supplier.id,
            Supplier.name,
            Supplier.phone,
            bucket_0_30.label("b_0_30"),
            bucket_31_60.label("b_31_60"),
            bucket_61_90.label("b_61_90"),
            bucket_90_plus.label("b_90_plus"),
            func.coalesce(func.sum(Purchase.due_amount), 0).label("total_due"),
            oldest_due_expr.label("oldest_due_date"),
        )
        .select_from(Supplier)
        .join(Purchase, _purchase_join(member))
        .where(Supplier.store_id == member.store_id, Purchase.due_amount > 0)
        .group_by(Supplier.id, Supplier.name, Supplier.phone)
        .order_by(func.coalesce(func.sum(Purchase.due_amount), 0).desc())
    )

    if search:
        query = query.where(Supplier.name.ilike(_like(search), escape="\\"))

    # items শুধু due > 0 purchase ধরে, তাই total ও একই শর্তে (আগে credit সহ ধরায় items এর যোগফলের সাথে মিলত না)
    total_query = select(func.coalesce(func.sum(Purchase.due_amount), 0)).where(
        Purchase.store_id == member.store_id, Purchase.due_amount > 0
    )
    if member.branch_id is not None:
        total_query = total_query.where(Purchase.branch_id == member.branch_id)
    total_due_amount = (await db.execute(total_query)).scalar_one()

    result = await db.execute(query.offset(skip).limit(limit))
    rows = result.all()

    now = datetime.now(timezone.utc)
    items = [
        SupplierDueAgingItem(
            supplier_id=row.id,
            supplier_name=row.name,
            phone=row.phone,
            current_0_30=row.b_0_30,
            days_31_60=row.b_31_60,
            days_61_90=row.b_61_90,
            days_90_plus=row.b_90_plus,
            total_due_amount=row.total_due,
            oldest_due_days=(now - row.oldest_due_date).days if row.oldest_due_date else None,
        )
        for row in rows
    ]

    return SupplierDueAgingResponse(
        as_of=now,
        total_due_amount=total_due_amount,
        items=items,
    )


@router.get("/{supplier_id}", response_model=SupplierResponse, tags=[TAG_SUPPLIERS], summary="Get supplier")
async def get_supplier(
    supplier_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    supplier = await repo.get_by_id_or_404(supplier_id)
    return SupplierResponse.model_validate(supplier)


@router.patch("/{supplier_id}", response_model=SupplierResponse, tags=[TAG_SUPPLIERS], summary="Update supplier")
async def update_supplier(
    supplier_id: int,
    data: SupplierUpdateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "update")),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    supplier = await repo.update(supplier_id, **update_data)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    await repo.db.commit()
    return SupplierResponse.model_validate(supplier)


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT, tags=[TAG_SUPPLIERS], summary="Delete supplier")
async def delete_supplier(
    supplier_id: int,
    member: StoreMember = Depends(require_permission("suppliers", "delete")),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    has_history = await repo.db.scalar(
        select(Purchase.id)
        .where(Purchase.supplier_id == supplier_id, Purchase.store_id == member.store_id)
        .limit(1)
    )
    if has_history is None:
        has_history = await repo.db.scalar(
            select(SupplierPayment.id)
            .where(SupplierPayment.supplier_id == supplier_id, SupplierPayment.store_id == member.store_id)
            .limit(1)
        )
    if has_history is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="এই supplier এর purchase/payment history আছে, তাই delete করা যাবে না",
        )

    for linked_model in (PurchaseRequisition, StockReceipt):
        linked = await repo.db.scalar(
            select(linked_model.id)
            .where(linked_model.supplier_id == supplier_id, linked_model.store_id == member.store_id)
            .limit(1)
        )
        if linked is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="এই supplier এর requisition/stock-receipt history আছে, তাই delete করা যাবে না",
            )

    deleted = await repo.delete(supplier_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    await repo.db.commit()
    return None


# ---------------------------------------------------------------------------
# SUPPLIER PAYMENT + LEDGER (NEW)
# ---------------------------------------------------------------------------

def _generate_invoice_no(supplier_id: int) -> str:
    """
    Payment দেওয়ার সময় purchase_id না দিলে auto-generated invoice number।
    চাইলে এখানে store-wise sequence বা আপনার নিজস্ব pattern বসাতে পারেন।
    """
    ts = int(datetime.now(timezone.utc).timestamp())
    return f"SPI-{supplier_id}-{ts}-{random.randint(100, 999)}"


def _supplier_invoice_pdf_url(request: Request, purchase: Optional[Purchase]) -> Optional[str]:
    """Authenticated download URL (আগে public /static/ ছিল)।"""
    if purchase is None:
        return None
    return str(
        request.url_for(
            "download_supplier_invoice_pdf",
            supplier_id=purchase.supplier_id,
            purchase_id=purchase.id,
        )
    )


class SupplierAllocatedPaymentResponse(BaseModel):
    total_amount: Decimal
    allocated_amount: Decimal  # পুরনো due শোধে গেছে
    advance_amount: Decimal  # due এর বেশি দেওয়া অংশ (supplier credit)
    payments: list[SupplierPaymentWithInvoiceResponse]


def _method_of(data) -> DuePaymentMethod:
    m = data.method
    return m if isinstance(m, DuePaymentMethod) else DuePaymentMethod(str(m).lower())


def _payment_response(request, payment: SupplierPayment, purchase: Purchase) -> SupplierPaymentWithInvoiceResponse:
    return SupplierPaymentWithInvoiceResponse(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no,
        purchase_total_amount=purchase.total_amount,
        purchase_paid_amount=purchase.paid_amount,
        purchase_due_amount=purchase.due_amount,
        pdf_url=_supplier_invoice_pdf_url(request, purchase),
    )


@router.post(
    "/{supplier_id}/payments",
    response_model=SupplierPaymentWithInvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=[TAG_PAYMENTS],
    summary="Pay a specific purchase invoice",
)
async def pay_supplier(
    request: Request,
    supplier_id: int,
    data: SupplierPaymentCreateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "update")),
    db: AsyncSession = Depends(get_db),
):
    """
    নির্দিষ্ট purchase (invoice) এর due শোধ। `purchase_id` বাধ্যতামূলক।
    (invoice ছাড়া সাধারণ payment এর জন্য: POST /suppliers/{id}/payments/allocate)
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    if not data.purchase_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="purchase_id দিতে হবে। Invoice ছাড়া payment এর জন্য /suppliers/{id}/payments/allocate ব্যবহার করুন",
        )
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must be greater than 0")

    purchase = (
        await db.execute(
            select(Purchase).where(Purchase.id == data.purchase_id).with_for_update()  # race ঠেকায়
        )
    ).scalar_one_or_none()
    if not purchase or purchase.store_id != member.store_id or purchase.supplier_id != supplier_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="এই supplier-এর জন্য এই purchase পাওয়া যায়নি",
        )
    assert_branch_access(member, purchase.branch_id)

    if data.amount > purchase.due_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment amount ({data.amount}) due amount ({purchase.due_amount}) থেকে বেশি হতে পারবে না",
        )

    purchase.paid_amount = purchase.paid_amount + data.amount
    purchase.due_amount = purchase.due_amount - data.amount

    payment = SupplierPayment(
        store_id=member.store_id,
        supplier_id=supplier_id,
        purchase_id=purchase.id,
        amount=data.amount,
        method=data.method,
        reference_no=data.reference_no,
        note=data.note,
        paid_by=member.user_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    await db.refresh(purchase)

    return _payment_response(request, payment, purchase)


@router.post(
    "/{supplier_id}/payments/allocate",
    response_model=SupplierAllocatedPaymentResponse,
    status_code=status.HTTP_201_CREATED,
    tags=[TAG_PAYMENTS],
    summary="Pay supplier without choosing an invoice (oldest due first)",
)
async def pay_supplier_allocate(
    request: Request,
    supplier_id: int,
    data: SupplierPaymentCreateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "update")),
    db: AsyncSession = Depends(get_db),
):
    """
    Invoice বাছাই ছাড়া payment: সবচেয়ে পুরনো due invoice থেকে শুরু করে শোধ হয় (FIFO)।
    প্রতিটা invoice এর জন্য আলাদা SupplierPayment row। due এর চেয়ে বেশি দিলে বাড়তি অংশ
    advance হিসেবে "SPI-" pseudo purchase এ credit (total=0, paid=advance, due=-advance) হয়ে থাকে।
    (আগের আচরণে due একটুও কমত না -- সেটা ভুল ছিল।)
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
    if data.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must be greater than 0")

    due_q = (
        select(Purchase)
        .where(
            Purchase.store_id == member.store_id,
            Purchase.supplier_id == supplier_id,
            Purchase.due_amount > 0,
        )
        .order_by(Purchase.created_at.asc(), Purchase.id.asc())
        .with_for_update()
    )
    if member.branch_id is not None:
        due_q = due_q.where(Purchase.branch_id == member.branch_id)
    due_purchases = (await db.execute(due_q)).scalars().all()

    remaining = data.amount
    payments: list[tuple[SupplierPayment, Purchase]] = []
    for purchase in due_purchases:
        if remaining <= 0:
            break
        pay = min(remaining, purchase.due_amount)
        purchase.paid_amount = purchase.paid_amount + pay
        purchase.due_amount = purchase.due_amount - pay
        payment = SupplierPayment(
            store_id=member.store_id,
            supplier_id=supplier_id,
            purchase_id=purchase.id,
            amount=pay,
            method=data.method,
            reference_no=data.reference_no,
            note=data.note,
            paid_by=member.user_id,
        )
        db.add(payment)
        payments.append((payment, purchase))
        remaining -= pay

    advance = remaining
    if advance > 0:
        branch_id = data.branch_id or member.branch_id
        if not branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{advance} টাকা due এর চেয়ে বেশি; advance রাখতে branch_id দিন (অথবা amount কমান)",
            )
        assert_branch_access(member, branch_id)
        if await db.scalar(
            select(Branch.id).where(Branch.id == branch_id, Branch.store_id == member.store_id)
        ) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid branch_id for this store")
        adv = Purchase(
            store_id=member.store_id,
            branch_id=branch_id,
            supplier_id=supplier_id,
            invoice_no=_generate_invoice_no(supplier_id),
            total_amount=Decimal("0"),
            paid_amount=advance,
            due_amount=-advance,
            created_by=member.user_id,
        )
        db.add(adv)
        await db.flush()
        payment = SupplierPayment(
            store_id=member.store_id,
            supplier_id=supplier_id,
            purchase_id=adv.id,
            amount=advance,
            method=data.method,
            reference_no=data.reference_no,
            note=data.note or "Advance payment",
            paid_by=member.user_id,
        )
        db.add(payment)
        payments.append((payment, adv))

    await db.commit()
    for payment, purchase in payments:
        await db.refresh(payment)
        await db.refresh(purchase)

    return SupplierAllocatedPaymentResponse(
        total_amount=data.amount,
        allocated_amount=data.amount - advance,
        advance_amount=advance,
        payments=[_payment_response(request, p, pu) for p, pu in payments],
    )


@router.get("/{supplier_id}/payments", response_model=list[SupplierPaymentWithInvoiceResponse],
            tags=[TAG_PAYMENTS], summary="List payments of one supplier")
async def list_supplier_payments(
    request: Request,
    supplier_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Supplier-এর সব payment history — প্রতিটা payment-এর সাথে যে invoice
    (Purchase) linked আছে তার invoice_no, amount এবং pdf_url সহ।
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    query = (
        select(SupplierPayment, Purchase)
        .join(Purchase, Purchase.id == SupplierPayment.purchase_id, isouter=True)
        .where(
            SupplierPayment.supplier_id == supplier_id,
            SupplierPayment.store_id == member.store_id,
            *([Purchase.branch_id == member.branch_id] if member.branch_id is not None else []),
        )
        .order_by(SupplierPayment.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.all()

    return [
        SupplierPaymentWithInvoiceResponse(
            **SupplierPaymentResponse.model_validate(payment).model_dump(),
            invoice_no=purchase.invoice_no if purchase else None,
            purchase_total_amount=purchase.total_amount if purchase else None,
            purchase_paid_amount=purchase.paid_amount if purchase else None,
            purchase_due_amount=purchase.due_amount if purchase else None,
            pdf_url=_supplier_invoice_pdf_url(request, purchase),
        )
        for payment, purchase in rows
    ]


@router.get("/{supplier_id}/invoices/{purchase_id}", response_model=SupplierInvoiceResponse,
            tags=[TAG_INVOICES], summary="Invoice detail")
async def get_supplier_invoice(
    supplier_id: int,
    purchase_id: int,
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    একটা নির্দিষ্ট invoice (Purchase)-এর পুরো detail দেখার জন্য —
    invoice_no, total/paid/due amount, এবং সেই invoice-এর বিপরীতে হওয়া সব payment।
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    purchase = await db.get(Purchase, purchase_id)
    if not purchase or purchase.store_id != member.store_id or purchase.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice পাওয়া যায়নি")
    assert_branch_access(member, purchase.branch_id)

    query = (
        select(SupplierPayment)
        .where(SupplierPayment.purchase_id == purchase_id, SupplierPayment.store_id == member.store_id)
        .order_by(SupplierPayment.created_at)
    )
    result = await db.execute(query)
    payments = result.scalars().all()

    return SupplierInvoiceResponse(
        purchase_id=purchase.id,
        invoice_no=purchase.invoice_no,
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        total_amount=purchase.total_amount,
        paid_amount=purchase.paid_amount,
        due_amount=purchase.due_amount,
        created_at=purchase.created_at,
        payments=[SupplierPaymentResponse.model_validate(p) for p in payments],
    )


@router.get("/{supplier_id}/ledger", response_model=SupplierLedgerSummary,
            tags=[TAG_LEDGER], summary="Ledger summary (total purchase / paid / due)")
async def supplier_ledger(
    supplier_id: int,
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """Supplier-এর total purchase, total paid, total due — এক নজরে ledger summary।"""
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    query = select(
        func.coalesce(func.sum(Purchase.total_amount), 0),
        func.coalesce(func.sum(Purchase.paid_amount), 0),
        func.coalesce(func.sum(Purchase.due_amount), 0),
    ).where(Purchase.supplier_id == supplier_id, Purchase.store_id == member.store_id)

    if member.branch_id is not None:
        query = query.where(Purchase.branch_id == member.branch_id)
    result = await db.execute(query)
    total, paid, due = result.one()

    return SupplierLedgerSummary(
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        total_purchase_amount=total,
        total_paid_amount=paid,
        total_due_amount=due,
    )


# ---------------------------------------------------------------------------
# SUPPLIER LEDGER HISTORY (NEW) - purchase + payment, date onujayi, running balance shoho
# ---------------------------------------------------------------------------

@router.get(
    "/{supplier_id}/ledger/history",
    response_model=SupplierLedgerHistoryResponse,
    tags=[TAG_LEDGER],
    summary="Supplier lenden history (running balance shoho)",
)
async def supplier_ledger_history(
    supplier_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    date_from: Optional[date] = Query(None, description="YYYY-MM-DD, ei tarikh theke"),
    date_to: Optional[date] = Query(None, description="YYYY-MM-DD, ei tarikh porjonto"),
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    Ekta supplier-er sob lenden purana theke notun order-e:
    - purchase (invoice)      -> due baray
    - payment                 -> due komay
    - initial_payment         -> purchase korar somoy-i dewa taka (jeta alada SupplierPayment row hishebe nei)

    Proti line-e running `balance` thake (positive = supplier-ke amra dibo, negative = advance).
    date_from dile opening_balance hishab hoye jay. Sob somoy closing_balance == /due API-r total_due.
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    purchases = (await db.execute(
        select(Purchase).where(Purchase.supplier_id == supplier_id, Purchase.store_id == member.store_id)
    )).scalars().all()
    payments = (await db.execute(
        select(SupplierPayment).where(SupplierPayment.supplier_id == supplier_id, SupplierPayment.store_id == member.store_id)
    )).scalars().all()

    if member.branch_id is not None:
        # branch-bound staff: শুধু নিজের branch-এর purchase ও সেগুলোর payment (balance ও সেই branch-এর)
        purchases = [p for p in purchases if p.branch_id == member.branch_id]
        own_ids = {p.id for p in purchases}
        payments = [p for p in payments if p.purchase_id in own_ids]

    zero = Decimal("0")
    purchase_map = {p.id: p for p in purchases}
    paid_via_payments: dict[int, Decimal] = {}
    for pay in payments:
        if pay.purchase_id:
            paid_via_payments[pay.purchase_id] = paid_via_payments.get(pay.purchase_id, zero) + pay.amount

    # (datetime, order, id, fields) -- order: purchase(0) -> initial_payment(1) -> payment(2)
    events = []
    for pur in purchases:
        events.append((pur.created_at, 0, pur.id, dict(
            type="purchase", invoice_no=pur.invoice_no, purchase_id=pur.id,
            purchase_amount=pur.total_amount,
        )))
        initial_paid = pur.paid_amount - paid_via_payments.get(pur.id, zero)
        if initial_paid > 0:
            events.append((pur.created_at, 1, pur.id, dict(
                type="initial_payment", invoice_no=pur.invoice_no, purchase_id=pur.id,
                paid_amount=initial_paid, note="Purchase-er somoy dewa taka",
            )))
    for pay in payments:
        pur = purchase_map.get(pay.purchase_id)
        events.append((pay.created_at, 2, pay.id, dict(
            type="payment", invoice_no=pur.invoice_no if pur else None, purchase_id=pay.purchase_id,
            payment_id=pay.id, method=pay.method, reference_no=pay.reference_no, note=pay.note,
            paid_amount=pay.amount,
        )))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    balance = zero
    opening = zero
    closing = zero
    total_purchase = zero
    total_paid = zero
    entries: list[SupplierLedgerEntry] = []

    for dt, _, _, fields in events:
        balance += fields.get("purchase_amount", zero) - fields.get("paid_amount", zero)
        day = to_dhaka_date(dt)
        if date_from and day < date_from:
            opening = balance
            closing = balance
            continue
        if date_to and day > date_to:
            break
        total_purchase += fields.get("purchase_amount", zero)
        total_paid += fields.get("paid_amount", zero)
        closing = balance
        entries.append(SupplierLedgerEntry(transaction_date=dt, balance=balance, **fields))

    return SupplierLedgerHistoryResponse(
        supplier_id=supplier_id,
        supplier_name=supplier.name,
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening,
        total_purchase_amount=total_purchase,
        total_paid_amount=total_paid,
        closing_balance=closing,
        total_entries=len(entries),
        entries=entries[skip: skip + limit],
    )


@router.get("/{supplier_id}/invoices/{purchase_id}/pdf", tags=[TAG_INVOICES], summary="Download invoice PDF")
async def download_supplier_invoice_pdf(
    supplier_id: int,
    purchase_id: int,
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """নির্দিষ্ট supplier invoice-এর PDF download।"""
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    purchase = await db.get(Purchase, purchase_id)
    if not purchase or purchase.store_id != member.store_id or purchase.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice পাওয়া যায়নি")
    assert_branch_access(member, purchase.branch_id)

    store = await db.get(Store, member.store_id)

    query = (
        select(SupplierPayment)
        .where(SupplierPayment.purchase_id == purchase_id, SupplierPayment.store_id == member.store_id)
        .order_by(SupplierPayment.created_at)
    )
    result = await db.execute(query)
    payments = result.scalars().all()

    pdf_bytes = build_supplier_payment_pdf(store, supplier, purchase, payments)

    filename = f"invoice-{purchase.invoice_no or purchase.id}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# ALL SUPPLIER PAYMENTS (across every supplier) (NEW)
# ---------------------------------------------------------------------------
# আলাদা router, prefix "/payments" -> route conflict এড়ানোর জন্য /suppliers/{supplier_id}
# এর ভেতরে না বসিয়ে top-level এ রাখা হলো। main.py-তে এটাও আলাদাভাবে include করতে হবে।

payments_router = APIRouter(prefix="/payments", tags=[TAG_PAYMENTS])


def _build_payment_list_item(payment: SupplierPayment, purchase: Optional[Purchase], supplier: Supplier, request: Request) -> SupplierPaymentListItem:
    """SupplierPayment + Purchase + Supplier row থেকে response item বানানোর helper (কোড ডুপ্লিকেশন এড়াতে)।"""
    return SupplierPaymentListItem(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no if purchase else None,
        purchase_total_amount=purchase.total_amount if purchase else None,
        purchase_paid_amount=purchase.paid_amount if purchase else None,
        purchase_due_amount=purchase.due_amount if purchase else None,
        pdf_url=_supplier_invoice_pdf_url(request, purchase),
        supplier_name=supplier.name,
    )

@payments_router.get("", response_model=list[SupplierPaymentListItem], summary="List payments of all suppliers")
async def list_all_supplier_payments(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    supplier_id: Optional[int] = Query(None, description="নির্দিষ্ট supplier-এর payment গুলো filter করতে চাইলে"),
    paid_by: Optional[int] = Query(None, description="নির্দিষ্ট user/member এর দেওয়া payment দেখতে চাইলে তার user_id দিন"),
    only_mine: bool = Query(False, description="True দিলে শুধু আপনার (login করা user-এর) করা payment গুলো দেখাবে"),
    method: Optional[DuePaymentMethod] = Query(None, description="cash / bkash / nagad / bank / other দিয়ে filter"),
    date_from: Optional[date] = Query(None, description="YYYY-MM-DD, এই তারিখ থেকে"),
    date_to: Optional[date] = Query(None, description="YYYY-MM-DD, এই তারিখ পর্যন্ত"),
    member: StoreMember = Depends(require_permission("suppliers", "read")),
    db: AsyncSession = Depends(get_db),
):
    """
    আপনার store-এর সব supplier-কে কখন, কত, কীভাবে payment করা হয়েছে —
    এক জায়গায় পুরো history। supplier_id / paid_by / only_mine / method / date range
    দিয়ে filter করা যাবে (সবগুলোই optional)।
    """
    query = (
        select(SupplierPayment, Purchase, Supplier)
        .join(Purchase, Purchase.id == SupplierPayment.purchase_id, isouter=True)
        .join(Supplier, Supplier.id == SupplierPayment.supplier_id)
        .where(SupplierPayment.store_id == member.store_id)
    )

    if supplier_id:
        query = query.where(SupplierPayment.supplier_id == supplier_id)
    if method:
        query = query.where(SupplierPayment.method == method)
    query = apply_date_range(query, SupplierPayment.created_at, date_from, date_to)
    if member.branch_id is not None:
        query = query.where(Purchase.branch_id == member.branch_id)

    if only_mine:
        query = query.where(SupplierPayment.paid_by == member.user_id)
    elif paid_by:
        query = query.where(SupplierPayment.paid_by == paid_by)

    query = query.order_by(SupplierPayment.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    return [_build_payment_list_item(payment, purchase, supplier, request) for payment, purchase, supplier in rows]





@router.patch(
    "/{supplier_id}/payments/{payment_id}",
    response_model=SupplierPaymentWithInvoiceResponse,
    tags=[TAG_PAYMENTS],
    summary="Update a supplier payment",
)
async def update_supplier_payment(
    request: Request,
    supplier_id: int,
    payment_id: int,
    data: SupplierPaymentUpdateRequest,
    member: StoreMember = Depends(require_permission("suppliers", "update")),
    db: AsyncSession = Depends(get_db),
):
    """
    আগে দেওয়া কোনো supplier payment এডিট করার API।

    - amount পরিবর্তন করলে, সেই payment যে Purchase-এর সাথে linked আছে
      তার paid_amount / due_amount ঠিক অনুযায়ী adjust হয়ে যাবে।
      Over-payment allowed -- due_amount negative হলে সেটা supplier-কে advance/credit
      হিসেবে ধরা হয়, কোনো error দেখাবে না।
    - method, reference_no, note -- এগুলোও চাইলে একসাথে বা আলাদাভাবে update করা যাবে।
    - Update সফল হলে ওই payment-এর purchase-এর সব payment history দিয়ে
      PDF আবার generate হয়ে আগের ফাইলটাই overwrite করে দেবে (invoice_no একই থাকায়
      filename ও একই থাকে), তাই pdf_url অপরিবর্তিত থাকলেও ভেতরের content আপডেট থাকবে।
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    # payment row আগে lock + fresh read: দুই PATCH একসাথে এলে দ্বিতীয়টা stale old_amount দিয়ে diff করত
    payment = (
        await db.execute(
            select(SupplierPayment)
            .where(SupplierPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not payment or payment.store_id != member.store_id or payment.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment পাওয়া যায়নি")
    old_amount_for_audit = payment.amount

    purchase: Optional[Purchase] = None
    if payment.purchase_id:
        purchase = (
            await db.execute(select(Purchase).where(Purchase.id == payment.purchase_id).with_for_update())
        ).scalar_one_or_none()
        if purchase is not None:
            assert_branch_access(member, purchase.branch_id)

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    if "amount" in update_data and (update_data["amount"] is None or update_data["amount"] <= 0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must be greater than 0")

    # --- amount change হলে purchase.paid_amount / due_amount adjust করা ---
    # note: over-payment allowed। due_amount negative হলে সেটা supplier-কে advance/credit
    # দেওয়া হয়েছে বলে ধরা হয় (business decision) -- তাই এখানে আর কোনো upper-limit check নেই।
    if "amount" in update_data and purchase is not None:
        new_amount = update_data["amount"]
        old_amount = payment.amount
        diff = new_amount - old_amount  # positive হলে আরো বেশি paid হচ্ছে

        new_due = purchase.due_amount - diff
        new_paid = purchase.paid_amount + diff

        purchase.due_amount = new_due
        purchase.paid_amount = new_paid

    # --- payment এর fields update ---
    for field, value in update_data.items():
        setattr(payment, field, value)

    if "amount" in update_data and update_data["amount"] != old_amount_for_audit:
        # টাকার অঙ্ক চুপচাপ বদলানো যাবে না — note এ trail থাকে
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        trail = f"[edited {stamp} by user {member.user_id}: {old_amount_for_audit} -> {update_data['amount']}]"
        max_len = getattr(SupplierPayment.note.type, "length", None) or 255
        base = (payment.note or "").strip()[: max(max_len - len(trail) - 1, 0)]
        payment.note = f"{base} {trail}".strip()

    await db.commit()
    await db.refresh(payment)
    if purchase is not None:
        await db.refresh(purchase)

    pdf_url = _supplier_invoice_pdf_url(request, purchase)

    return SupplierPaymentWithInvoiceResponse(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no if purchase else None,
        purchase_total_amount=purchase.total_amount if purchase else None,
        purchase_paid_amount=purchase.paid_amount if purchase else None,
        purchase_due_amount=purchase.due_amount if purchase else None,
        pdf_url=pdf_url,
    )