# app/routers/purchase_requisition.py
#
# Purchase Requisition workflow API
#
#   POST   /requisitions                      -> staff creates a requisition (pending)
#   GET    /requisitions                      -> list (filter by status/branch)
#   GET    /requisitions/{id}                 -> detail
#   POST   /requisitions/{id}/approve         -> admin: pending -> approved
#   POST   /requisitions/{id}/reject          -> admin: pending -> rejected
#   POST   /requisitions/{id}/order           -> admin: approved -> ordered
#   POST   /requisitions/{id}/complete        -> admin: ordered -> completed
#                                                  (stock receive -> আসল Purchase +
#                                                   PurchaseItem + StockBatch তৈরি হয়,
#                                                   stock quantity তখনই বাড়ে)
#   POST   /requisitions/{id}/cancel          -> admin: pending/approved/ordered -> cancelled
#
# admin-only endpoint গুলো এখন require_permission("purchase_requisitions", <action>) ব্যবহার করে —
# role slug ('manager') এর উপর নির্ভর করে না, তাই custom role দিয়ে privilege escalation হয় না।
# requisition তৈরি করা যেকোনো active store member করতে পারবে (staff/cashier/pharmacist সহ)
# -> সেজন্য get_current_store_member ব্যবহার করা হয়েছে create তে।

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import (
    Branch,
    Medicine,
    MasterMedicine,
    Purchase,
    PurchaseItem,
    PurchaseRequisition,
    PurchaseRequisitionItem,
    RequisitionStatus,
    StockBatch,
    Store,
    Supplier,
    SupplierPayment,
    DuePaymentMethod,
    StoreMember,
    User,
)
from app.schemas.purchase_requisition import (
    RequisitionApproveRequest,
    RequisitionCancelRequest,
    RequisitionCompleteRequest,
    RequisitionCreateRequest,
    RequisitionItemResponse,
    RequisitionListItemResponse,
    RequisitionOrderRequest,
    RequisitionRejectRequest,
    RequisitionResponse,
)
from app.services.branch_scope import assert_branch_access, effective_branch_filter
from app.services.medicine_resolver import resolve_item_medicines
from app.services.unit_service import (
    create_medicine_with_base_unit,
    load_medicine_units,
    load_unit_map,
    money,
    price_per_base,
    resolve_or_create_unit_for_receiving,
    to_base_quantity,
)


# router-লেভেল default tag বাদ দেওয়া হয়েছে (purchase.py এর মতোই) — প্রতিটা endpoint
# নিজের group অনুযায়ী tag পাবে, যাতে Swagger এ "Purchase Requisitions" (create/list/detail)
# আর "Purchase Requisitions - Workflow" (approve/reject/order/complete/cancel) আলাদা দেখায়।
router = APIRouter(prefix="/requisitions")

TAG_CORE = "Purchase Requisitions"
TAG_WORKFLOW = "Purchase Requisitions - Workflow"


def purchase_invoice_pdf_url(request: Request, purchase: Optional[Purchase]) -> Optional[str]:
    """Authenticated endpoint (purchase.py::download_purchase_invoice_pdf)। আগে public /static/ ছিল।"""
    if purchase is None:
        return None
    return str(request.url_for("download_purchase_invoice_pdf", purchase_id=purchase.id))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _get_requisition_or_404(
    db: AsyncSession,
    requisition_id: int,
    store_id: int,
    member: Optional[StoreMember] = None,
    *,
    for_update: bool = False,
) -> PurchaseRequisition:
    stmt = (
        select(PurchaseRequisition)
        .options(selectinload(PurchaseRequisition.items))
        .where(
            PurchaseRequisition.id == requisition_id,
            PurchaseRequisition.store_id == store_id,
        )
    )
    if for_update:
        # header row লক: double-click / parallel workflow call ঠেকায়
        stmt = stmt.with_for_update(of=PurchaseRequisition)
    requisition = await db.scalar(stmt)
    if requisition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requisition not found")
    if member is not None:
        assert_branch_access(member, requisition.branch_id)
    return requisition


async def _build_response(
    db: AsyncSession, requisition: PurchaseRequisition, request: Optional[Request] = None
) -> RequisitionResponse:
    supplier_name = None
    if requisition.supplier_id is not None:
        supplier_name = await db.scalar(
            select(Supplier.name).where(Supplier.id == requisition.supplier_id)
        )

    requested_by_name = await db.scalar(
        select(User.full_name).where(User.id == requisition.requested_by)
    )

    medicine_ids = [it.medicine_id for it in requisition.items]
    medicine_names: dict[int, str] = {}
    if medicine_ids:
        rows = await db.execute(
            select(Medicine.id, Medicine.name).where(Medicine.id.in_(medicine_ids))
        )
        medicine_names = {row.id: row.name for row in rows.all()}

    unit_map = await load_unit_map(db, requisition.store_id, [it.unit_id for it in requisition.items])

    item_responses = [
        RequisitionItemResponse(
            id=it.id,
            medicine_id=it.medicine_id,
            medicine_name=medicine_names.get(it.medicine_id),
            unit_id=it.unit_id,
            unit_name=unit_map[it.unit_id].name if it.unit_id in unit_map else None,
            quantity_requested=it.quantity_requested,
            quantity_approved=it.quantity_approved,
            estimated_unit_price=it.estimated_unit_price,
            received_quantity=it.received_quantity,
            unit_price=it.unit_price,
            sale_price=it.sale_price,
            batch_no=it.batch_no,
            expiry_date=it.expiry_date,
            notes=it.notes,
        )
        for it in requisition.items
    ]

    # completed হয়ে গেলে linked Purchase থেকে invoice PDF এর url বসিয়ে দেওয়া
    pdf_url = None
    if request is not None and requisition.purchase_id is not None:
        linked_purchase = await db.get(Purchase, requisition.purchase_id)
        pdf_url = purchase_invoice_pdf_url(request, linked_purchase)

    return RequisitionResponse(
        id=requisition.id,
        store_id=requisition.store_id,
        branch_id=requisition.branch_id,
        supplier_id=requisition.supplier_id,
        supplier_name=supplier_name,
        status=requisition.status,
        requisition_no=requisition.requisition_no,
        notes=requisition.notes,
        requested_by=requisition.requested_by,
        requested_by_name=requested_by_name,
        requested_at=requisition.requested_at,
        approved_by=requisition.approved_by,
        approved_at=requisition.approved_at,
        rejected_by=requisition.rejected_by,
        rejected_at=requisition.rejected_at,
        rejection_reason=requisition.rejection_reason,
        ordered_by=requisition.ordered_by,
        ordered_at=requisition.ordered_at,
        completed_by=requisition.completed_by,
        completed_at=requisition.completed_at,
        purchase_id=requisition.purchase_id,
        pdf_url=pdf_url,
        cancelled_by=requisition.cancelled_by,
        cancelled_at=requisition.cancelled_at,
        items=item_responses,
    )


# ---------------------------------------------------------------------------
# CREATE — staff requisition পাঠায়
# ---------------------------------------------------------------------------

@router.post("", response_model=RequisitionResponse, status_code=status.HTTP_201_CREATED, tags=[TAG_CORE])
async def create_requisition(
    payload: RequisitionCreateRequest,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    store_id = member.store_id
    assert_branch_access(member, payload.branch_id)

    branch = await db.scalar(
        select(Branch).where(Branch.id == payload.branch_id, Branch.store_id == store_id)
    )
    if branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid branch_id for this store")

    if payload.supplier_id is not None:
        supplier = await db.scalar(
            select(Supplier).where(
                Supplier.id == payload.supplier_id, Supplier.store_id == store_id
            )
        )
        if supplier is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid supplier_id for this store")

    # --- medicine resolve: `medicine_id` = এই store এর Medicine.id, `master_medicine_id` = catalog ---
    # (দুটো id আর মেশানো হয় না। ⚠ request item schema তে optional master_medicine_id যোগ করতে হবে)
    resolved_meds = await resolve_item_medicines(db, store_id, payload.items)

    # --- unit validate (unit_id না দিলে medicine-এর base unit) ---
    unit_ids = [
        i.unit_id if i.unit_id is not None else m.base_unit_id
        for i, m in zip(payload.items, resolved_meds)
    ]
    unit_map = await load_unit_map(db, store_id, unit_ids)
    mu_map = await load_medicine_units(db, [m.id for m in resolved_meds])
    resolved_units = []
    for i, m, uid in zip(payload.items, resolved_meds, unit_ids):
        ru = await resolve_or_create_unit_for_receiving(
            db,
            store_id,
            m,
            uid,
            unit_map,
            mu_map,
            conversion_factor=i.conversion_factor,
        )
        resolved_units.append(ru)

    requisition = PurchaseRequisition(
        store_id=store_id,
        branch_id=payload.branch_id,
        supplier_id=payload.supplier_id,
        notes=payload.notes,
        status=RequisitionStatus.pending,
        requested_by=member.user_id,
    )
    db.add(requisition)
    await db.flush()

    requisition.requisition_no = f"REQ-{requisition.id:06d}"

    for item_in, ru, resolved_medicine in zip(payload.items, resolved_units, resolved_meds):
        db.add(
            PurchaseRequisitionItem(
                requisition_id=requisition.id,
                store_id=store_id,
                medicine_id=resolved_medicine.id,
                unit_id=ru.unit_id,
                quantity_requested=item_in.quantity_requested,
                notes=item_in.notes,
            )
        )

    await db.commit()

    requisition = await _get_requisition_or_404(db, requisition.id, store_id, member)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

@router.get("", response_model=list[RequisitionListItemResponse], tags=[TAG_CORE])
async def list_requisitions(
    branch_id: Optional[int] = Query(default=None),
    req_status: Optional[RequisitionStatus] = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    item_count_subq = (
        select(func.count(PurchaseRequisitionItem.id))
        .where(PurchaseRequisitionItem.requisition_id == PurchaseRequisition.id)
        .correlate(PurchaseRequisition)
        .scalar_subquery()
    )

    query = (
        select(
            PurchaseRequisition,
            Supplier.name.label("supplier_name"),
            User.full_name.label("requested_by_name"),
            item_count_subq.label("item_count"),
        )
        .outerjoin(Supplier, Supplier.id == PurchaseRequisition.supplier_id)
        .outerjoin(User, User.id == PurchaseRequisition.requested_by)
        .where(PurchaseRequisition.store_id == member.store_id)
        .order_by(PurchaseRequisition.requested_at.desc())
    )

    branch_id = effective_branch_filter(member, branch_id)
    if branch_id is not None:
        query = query.where(PurchaseRequisition.branch_id == branch_id)
    if req_status is not None:
        query = query.where(PurchaseRequisition.status == req_status)

    result = await db.execute(query.offset(skip).limit(limit))

    return [
        RequisitionListItemResponse(
            id=row.PurchaseRequisition.id,
            branch_id=row.PurchaseRequisition.branch_id,
            status=row.PurchaseRequisition.status,
            requisition_no=row.PurchaseRequisition.requisition_no,
            supplier_id=row.PurchaseRequisition.supplier_id,
            supplier_name=row.supplier_name,
            requested_by_name=row.requested_by_name,
            item_count=row.item_count,
            requested_at=row.PurchaseRequisition.requested_at,
        )
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------

@router.get("/{requisition_id}", response_model=RequisitionResponse, tags=[TAG_CORE])
async def get_requisition(
    request: Request,
    requisition_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member)
    return await _build_response(db, requisition, request)


# ---------------------------------------------------------------------------
# APPROVE — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/approve", response_model=RequisitionResponse, tags=[TAG_WORKFLOW])
async def approve_requisition(
    requisition_id: int,
    payload: RequisitionApproveRequest,
    member: StoreMember = Depends(require_permission("purchase_requisitions", "approve")),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member, for_update=True)

    if requisition.status != RequisitionStatus.pending:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only a pending requisition can be approved (current status: {requisition.status.value})",
        )

    adjustments = {a.item_id: a.quantity_approved for a in (payload.item_adjustments or [])}
    for item in requisition.items:
        item.quantity_approved = adjustments.get(item.id, item.quantity_requested)

    requisition.status = RequisitionStatus.approved
    requisition.approved_by = member.user_id
    requisition.approved_at = datetime.now(timezone.utc)
    if payload.notes:
        requisition.notes = payload.notes

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# REJECT — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/reject", response_model=RequisitionResponse, tags=[TAG_WORKFLOW])
async def reject_requisition(
    requisition_id: int,
    payload: RequisitionRejectRequest,
    member: StoreMember = Depends(require_permission("purchase_requisitions", "reject")),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member, for_update=True)

    if requisition.status != RequisitionStatus.pending:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only a pending requisition can be rejected (current status: {requisition.status.value})",
        )

    requisition.status = RequisitionStatus.rejected
    requisition.rejected_by = member.user_id
    requisition.rejected_at = datetime.now(timezone.utc)
    requisition.rejection_reason = payload.reason

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# ORDER — admin marks it ordered (supplier কে order দেওয়া হলো)
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/order", response_model=RequisitionResponse, tags=[TAG_WORKFLOW])
async def mark_requisition_ordered(
    requisition_id: int,
    payload: RequisitionOrderRequest,
    member: StoreMember = Depends(require_permission("purchase_requisitions", "order")),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member, for_update=True)

    if requisition.status != RequisitionStatus.approved:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only an approved requisition can be marked as ordered (current status: {requisition.status.value})",
        )

    if payload.supplier_id is not None:
        supplier = await db.scalar(
            select(Supplier).where(
                Supplier.id == payload.supplier_id, Supplier.store_id == member.store_id
            )
        )
        if supplier is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid supplier_id for this store")
        requisition.supplier_id = payload.supplier_id

    price_map = {p.item_id: p.estimated_unit_price for p in (payload.item_prices or [])}
    items_by_id = {it.id: it for it in requisition.items}
    for item_id, price in price_map.items():
        if item_id in items_by_id:
            items_by_id[item_id].estimated_unit_price = price

    requisition.status = RequisitionStatus.ordered
    requisition.ordered_by = member.user_id
    requisition.ordered_at = datetime.now(timezone.utc)
    if payload.notes:
        requisition.notes = payload.notes

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# COMPLETE — stock receive হয়েছে -> আসল Purchase + StockBatch তৈরি হয়
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/complete", response_model=RequisitionResponse, tags=[TAG_WORKFLOW])
async def complete_requisition(
    request: Request,
    requisition_id: int,
    payload: RequisitionCompleteRequest,
    member: StoreMember = Depends(require_permission("purchase_requisitions", "complete")),
    db: AsyncSession = Depends(get_db),
):
    """
    এখানেই আসল stock-in হয়:
      - নতুন Purchase (invoice) তৈরি হয়
      - প্রতিটা item এর জন্য নতুন StockBatch (quantity সহ) তৈরি হয় -> stock এ যোগ হয়ে যায়
      - প্রতিটা item এর জন্য PurchaseItem তৈরি হয়
      - invoice PDF auto-generate ও save হয়ে যায় (purchase.py এর create_purchase এর মতোই)
      - requisition.status = completed, purchase_id link হয়ে যায়

    Rule: requisition এর সবগুলো item এর জন্য receive info (quantity, batch, price)
    একসাথে দিতে হবে — partial completion সাপোর্ট করা হচ্ছে না, কারণ requisition
    এর সব item একসাথেই এক চালানে receive হচ্ছে বলে ধরে নেওয়া হয়েছে।
    """
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member, for_update=True)

    if requisition.status != RequisitionStatus.ordered:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only an ordered requisition can be completed (current status: {requisition.status.value})",
        )

    # /stock-receipts দিয়ে আংশিক receive শুরু হয়ে গেলে এই (সব-একসাথে) path বন্ধ -- নাহলে
    # একই মাল দুইবার stock-এ ঢুকবে। বাকি receive /stock-receipts দিয়েই শেষ করুন।
    if any((getattr(it, "received_quantity", None) or 0) > 0 for it in requisition.items):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "এই requisition এ ইতিমধ্যে stock-receipt দিয়ে মাল receive শুরু হয়েছে। "
            "বাকি অংশ /stock-receipts দিয়ে receive করুন।",
        )

    items_by_id = {it.id: it for it in requisition.items}

    payload_item_ids = {ci.item_id for ci in payload.items}
    requisition_item_ids = set(items_by_id.keys())
    if payload_item_ids != requisition_item_ids:
        missing = requisition_item_ids - payload_item_ids
        unknown = payload_item_ids - requisition_item_ids
        detail = []
        if missing:
            detail.append(f"missing item_id(s): {sorted(missing)}")
        if unknown:
            detail.append(f"unknown item_id(s): {sorted(unknown)}")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(detail))

    store_id = member.store_id
    branch_id = requisition.branch_id

    # invoice PDF এর নাম + unit resolve এর জন্য medicine আগেই load করা হচ্ছে
    all_medicine_ids = [it.medicine_id for it in requisition.items]
    medicine_rows = await db.execute(select(Medicine).where(Medicine.id.in_(all_medicine_ids)))
    medicines_by_id = {m.id: m for m in medicine_rows.scalars().all()}
    medicine_names = {mid: m.name for mid, m in medicines_by_id.items()}

    # --- unit resolve + base conversion — কিছু তৈরির আগেই, যাতে ভুল হলে কিছুই লেখা না হয় ---
    # unit_id: request > requisition item এর unit > medicine.base_unit
    def _unit_for(ci) -> int:
        req_item = items_by_id[ci.item_id]
        return ci.unit_id or req_item.unit_id or medicines_by_id[req_item.medicine_id].base_unit_id

    unit_map = await load_unit_map(db, store_id, [_unit_for(ci) for ci in payload.items])
    mu_map = await load_medicine_units(db, all_medicine_ids)
    resolved_lines = []
    for ci in payload.items:
        req_item = items_by_id[ci.item_id]
        medicine = medicines_by_id[req_item.medicine_id]
        ru = await resolve_or_create_unit_for_receiving(
            db, store_id, medicine, _unit_for(ci), unit_map, mu_map,
            conversion_factor=ci.conversion_factor,
        )
        base_qty = to_base_quantity(ci.received_quantity, ru.factor)
        resolved_lines.append((ci, req_item, ru, base_qty))

    purchase = Purchase(
        store_id=store_id,
        branch_id=branch_id,
        supplier_id=requisition.supplier_id,
        total_amount=Decimal("0"),
        paid_amount=Decimal("0"),
        due_amount=Decimal("0"),
        created_by=member.user_id,
    )
    db.add(purchase)
    await db.flush()

    # invoice_no সবসময় auto-generate হয় (purchase.py এর create_purchase এর মতোই) —
    # manual input নেওয়া হয় না, যাতে duplicate/ভুল invoice_no এর ঝুঁকি না থাকে
    purchase.invoice_no = f"PUR-{purchase.id:06d}"

    total_amount = Decimal("0")

    for ci, req_item, ru, base_qty in resolved_lines:
        # StockBatch base unit-এ: quantity ও দাম দুটোই। ci.unit_price / ci.sale_price "ঐ unit-এর" দাম।
        batch = StockBatch(
            store_id=store_id,
            branch_id=branch_id,
            medicine_id=req_item.medicine_id,
            batch_no=ci.batch_no,
            quantity=base_qty,
            purchase_price=price_per_base(ci.unit_price, ru.factor),
            sale_price=price_per_base(ci.sale_price, ru.factor),
            expiry_date=ci.expiry_date,
        )
        db.add(batch)
        await db.flush()

        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            store_id=store_id,
            medicine_id=req_item.medicine_id,
            stock_batch_id=batch.id,
            unit_id=ru.unit_id,
            quantity_in_unit=ci.received_quantity,
            quantity=base_qty,
            unit_price=ci.unit_price,
        )
        db.add(purchase_item)
        await db.flush()

        line_total = money(ci.unit_price * ci.received_quantity)
        total_amount += line_total


        # requisition item আপডেট — received_quantity / unit_price / sale_price ru.unit_id এর হিসাবে
        req_item.unit_id = ru.unit_id
        req_item.received_quantity = ci.received_quantity
        req_item.unit_price = ci.unit_price
        req_item.sale_price = ci.sale_price
        req_item.batch_no = ci.batch_no
        req_item.expiry_date = ci.expiry_date
        req_item.purchase_item_id = purchase_item.id

    paid_amount = payload.paid_amount or Decimal("0")
    if paid_amount < 0 or paid_amount > total_amount:
        await db.rollback()  # এই পর্যন্ত লেখা batch/purchase ফেলে দাও
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"paid_amount ({paid_amount}) must be between 0 and total_amount ({total_amount})",
        )
    due_amount = total_amount - paid_amount
    if due_amount > 0 and requisition.supplier_id is None:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A supplier is required to complete a requisition with due amount"
        )

    purchase.total_amount = total_amount
    purchase.paid_amount = paid_amount
    purchase.due_amount = due_amount

    if paid_amount > 0 and requisition.supplier_id is not None:
        method = DuePaymentMethod.cash
        if payload.payment_method:
            try:
                method = DuePaymentMethod(payload.payment_method.lower())
            except ValueError:
                method = DuePaymentMethod.other

        db.add(
            SupplierPayment(
                store_id=store_id,
                supplier_id=requisition.supplier_id,
                purchase_id=purchase.id,
                amount=paid_amount,
                method=method,
                reference_no=payload.payment_reference,
                note=payload.payment_note,
                paid_by=member.user_id,
            )
        )

    requisition.status = RequisitionStatus.completed
    requisition.completed_by = member.user_id
    requisition.completed_at = datetime.now(timezone.utc)
    requisition.purchase_id = purchase.id

    await db.commit()
    await db.refresh(purchase)

    # PDF আর save হয় না: on-demand GET /purchases/{id}/invoice/pdf (response এ pdf_url থাকে)
    requisition = await _get_requisition_or_404(db, requisition_id, store_id, member)
    return await _build_response(db, requisition, request)


# ---------------------------------------------------------------------------
# CANCEL — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/cancel", response_model=RequisitionResponse, tags=[TAG_WORKFLOW])
async def cancel_requisition(
    requisition_id: int,
    payload: RequisitionCancelRequest,
    member: StoreMember = Depends(require_permission("purchase_requisitions", "cancel")),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id, member, for_update=True)

    if requisition.status not in (
        RequisitionStatus.pending,
        RequisitionStatus.approved,
        RequisitionStatus.ordered,
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot cancel a requisition with status: {requisition.status.value}",
        )

    requisition.status = RequisitionStatus.cancelled
    requisition.cancelled_by = member.user_id
    requisition.cancelled_at = datetime.now(timezone.utc)
    if payload.reason:
        requisition.notes = f"{requisition.notes or ''}\n[Cancelled] {payload.reason}".strip()

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)