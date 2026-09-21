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
# require_store_admin এর মধ্যে already owner/manager/super_admin check করা আছে
# (app/depends/deps.py দেখুন), তাই সব admin-only endpoint এ ওটাই ব্যবহার হয়েছে।
# requisition তৈরি করা যেকোনো active store member করতে পারবে (staff/cashier/pharmacist সহ)
# -> সেজন্য get_current_store_member ব্যবহার করা হয়েছে create তে।

import io
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
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


router = APIRouter(prefix="/requisitions", tags=["Purchase Requisitions"])


# ---------------------------------------------------------------------------
# INVOICE PDF — purchase.py এর create_purchase এর pattern অনুসরণ করে
# ---------------------------------------------------------------------------

# purchase.py যে ফোল্ডারে invoice save করে, এখানেও সেই একই ফোল্ডার ব্যবহার করা হচ্ছে
# (কারণ এখানে যেটা তৈরি হয় সেটা একটা আসল Purchase-ই, শুধু requisition দিয়ে এসেছে)
INVOICE_PDF_DIR = "static/invoices/purchases"


@dataclass
class _InvoiceLineItem:
    medicine_name: Optional[str]
    batch_no: Optional[str]
    quantity: int
    unit_price: Decimal
    line_total: Decimal


def _safe_purchase_invoice_filename(purchase: Purchase) -> str:
    """invoice_no না থাকলে বা special character থাকলে safe filename বানানো"""
    raw = purchase.invoice_no or f"PUR-{purchase.id}"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    return f"{slug}.pdf"


def purchase_invoice_pdf_url(request: Request, purchase: Optional[Purchase]) -> Optional[str]:
    if purchase is None:
        return None
    base_url = str(request.base_url).rstrip("/")
    filename = _safe_purchase_invoice_filename(purchase)
    return f"{base_url}/static/invoices/purchases/{filename}"


def _save_purchase_invoice_pdf(request: Request, pdf_bytes: bytes, purchase: Purchase) -> str:
    os.makedirs(INVOICE_PDF_DIR, exist_ok=True)
    filename = _safe_purchase_invoice_filename(purchase)
    file_path = os.path.join(INVOICE_PDF_DIR, filename)

    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/static/invoices/purchases/{filename}"


def _build_purchase_invoice_pdf(
    store: Optional[Store],
    supplier: Optional[Supplier],
    purchase: Purchase,
    items: list[_InvoiceLineItem],
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle("StoreTitle", parent=styles["Title"], fontSize=18, spaceAfter=2)
    story.append(Paragraph(store.name if store else "Store", title_style))
    if store and store.address:
        story.append(Paragraph(store.address, styles["Normal"]))
    if store and store.phone:
        story.append(Paragraph(f"Phone: {store.phone}", styles["Normal"]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("PURCHASE INVOICE", ParagraphStyle("H2", parent=styles["Heading2"])))
    story.append(Spacer(1, 8))

    meta_data = [
        ["Invoice No:", purchase.invoice_no or f"PUR-{purchase.id}", "Date:", purchase.created_at.strftime("%d-%m-%Y")],
        ["Supplier:", supplier.name if supplier else "-", "Phone:", (supplier.phone if supplier else None) or "-"],
    ]
    meta_table = Table(meta_data, colWidths=[70, 170, 50, 150])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 16))

    data = [["#", "Medicine", "Batch", "Qty", "Unit Price", "Line Total"]]
    for i, it in enumerate(items, 1):
        data.append([
            str(i),
            it.medicine_name or "-",
            it.batch_no or "-",
            str(it.quantity),
            f"{it.unit_price:.2f}",
            f"{it.line_total:.2f}",
        ])

    table = Table(data, colWidths=[20, 150, 70, 45, 75, 80])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (3, 0), (5, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 16))

    summary_data = [
        ["Total Amount:", f"{purchase.total_amount:.2f}"],
        ["Paid Amount:", f"{(purchase.paid_amount or 0):.2f}"],
        ["Due Amount:", f"{(purchase.due_amount or 0):.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[120, 90], hAlign="RIGHT")
    summary_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("LINEABOVE", (0, 2), (-1, 2), 0.75, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 24))
    story.append(Paragraph(
        "This is a system-generated invoice (from purchase requisition).",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey),
    ))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _get_requisition_or_404(
    db: AsyncSession, requisition_id: int, store_id: int
) -> PurchaseRequisition:
    from sqlalchemy.orm import selectinload

    requisition = await db.scalar(
        select(PurchaseRequisition)
        .options(selectinload(PurchaseRequisition.items))
        .where(
            PurchaseRequisition.id == requisition_id,
            PurchaseRequisition.store_id == store_id,
        )
    )
    if requisition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requisition not found")
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

    item_responses = [
        RequisitionItemResponse(
            id=it.id,
            medicine_id=it.medicine_id,
            medicine_name=medicine_names.get(it.medicine_id),
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

@router.post("", response_model=RequisitionResponse, status_code=status.HTTP_201_CREATED)
async def create_requisition(
    payload: RequisitionCreateRequest,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    store_id = member.store_id

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

    # --- medicine validate + auto-create from master if needed ---
    # (purchase.py এর create_purchase এর মতো একই fallback: app এর medicine picker
    #  থেকে MasterMedicine এর id আসতে পারে, যেটা এখনো এই store এর নিজস্ব
    #  Medicine টেবিলে যোগ হয়নি — সেক্ষেত্রে এখানে auto-create করে নেওয়া হয়)
    medicine_ids = [item.medicine_id for item in payload.items]

    result = await db.execute(
        select(Medicine).where(
            Medicine.id.in_(medicine_ids), Medicine.store_id == store_id
        )
    )
    medicines_by_id: dict[int, Medicine] = {m.id: m for m in result.scalars().all()}

    missing_ids = set(medicine_ids) - medicines_by_id.keys()

    if missing_ids:
        master_result = await db.execute(
            select(MasterMedicine).where(
                MasterMedicine.id.in_(missing_ids),
                MasterMedicine.is_active.is_(True),
            )
        )
        masters = {m.id: m for m in master_result.scalars().all()}

        still_missing = missing_ids - masters.keys()
        if still_missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid medicine_id(s): {sorted(still_missing)}",
            )

        for master_id, master in masters.items():
            existing = await db.scalar(
                select(Medicine).where(
                    Medicine.master_medicine_id == master_id, Medicine.store_id == store_id
                )
            )
            if existing:
                medicines_by_id[master_id] = existing
            else:
                new_med = Medicine(
                    store_id=store_id,
                    name=master.brand_name,
                    generic_name=master.generic_name,
                    category=None,
                    unit="pcs",
                    reorder_level=10,
                    master_medicine_id=master.id,
                    is_active=True,
                )
                db.add(new_med)
                await db.flush()
                medicines_by_id[master_id] = new_med

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

    for item_in in payload.items:
        # item_in.medicine_id হয় সরাসরি Medicine.id, নয়তো MasterMedicine.id ছিল —
        # medicines_by_id সবসময় আসল (store-এর নিজস্ব) Medicine.id তে resolve করে দেয়
        resolved_medicine = medicines_by_id[item_in.medicine_id]
        db.add(
            PurchaseRequisitionItem(
                requisition_id=requisition.id,
                store_id=store_id,
                medicine_id=resolved_medicine.id,
                quantity_requested=item_in.quantity_requested,
                notes=item_in.notes,
            )
        )

    await db.commit()

    requisition = await _get_requisition_or_404(db, requisition.id, store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

@router.get("", response_model=list[RequisitionListItemResponse])
async def list_requisitions(
    branch_id: Optional[int] = Query(default=None),
    req_status: Optional[RequisitionStatus] = Query(default=None, alias="status"),
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

    if branch_id is not None:
        query = query.where(PurchaseRequisition.branch_id == branch_id)
    if req_status is not None:
        query = query.where(PurchaseRequisition.status == req_status)

    result = await db.execute(query)

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

@router.get("/{requisition_id}", response_model=RequisitionResponse)
async def get_requisition(
    request: Request,
    requisition_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition, request)


# ---------------------------------------------------------------------------
# APPROVE — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/approve", response_model=RequisitionResponse)
async def approve_requisition(
    requisition_id: int,
    payload: RequisitionApproveRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)

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
    requisition.approved_at = datetime.utcnow()
    if payload.notes:
        requisition.notes = payload.notes

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# REJECT — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/reject", response_model=RequisitionResponse)
async def reject_requisition(
    requisition_id: int,
    payload: RequisitionRejectRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)

    if requisition.status != RequisitionStatus.pending:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only a pending requisition can be rejected (current status: {requisition.status.value})",
        )

    requisition.status = RequisitionStatus.rejected
    requisition.rejected_by = member.user_id
    requisition.rejected_at = datetime.utcnow()
    requisition.rejection_reason = payload.reason

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# ORDER — admin marks it ordered (supplier কে order দেওয়া হলো)
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/order", response_model=RequisitionResponse)
async def mark_requisition_ordered(
    requisition_id: int,
    payload: RequisitionOrderRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)

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
    requisition.ordered_at = datetime.utcnow()
    if payload.notes:
        requisition.notes = payload.notes

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)


# ---------------------------------------------------------------------------
# COMPLETE — stock receive হয়েছে -> আসল Purchase + StockBatch তৈরি হয়
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/complete", response_model=RequisitionResponse)
async def complete_requisition(
    request: Request,
    requisition_id: int,
    payload: RequisitionCompleteRequest,
    member: StoreMember = Depends(require_store_admin),
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
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)

    if requisition.status != RequisitionStatus.ordered:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only an ordered requisition can be completed (current status: {requisition.status.value})",
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

    # invoice PDF এর জন্য medicine নাম আগেই lookup করে রাখা হচ্ছে
    all_medicine_ids = [it.medicine_id for it in requisition.items]
    medicine_name_rows = await db.execute(
        select(Medicine.id, Medicine.name).where(Medicine.id.in_(all_medicine_ids))
    )
    medicine_names = {row.id: row.name for row in medicine_name_rows.all()}

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
    invoice_line_items: list[_InvoiceLineItem] = []

    for ci in payload.items:
        req_item = items_by_id[ci.item_id]

        batch = StockBatch(
            store_id=store_id,
            branch_id=branch_id,
            medicine_id=req_item.medicine_id,
            batch_no=ci.batch_no,
            quantity=ci.received_quantity,
            purchase_price=ci.unit_price,
            sale_price=ci.sale_price,
            expiry_date=ci.expiry_date,
        )
        db.add(batch)
        await db.flush()

        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            store_id=store_id,
            medicine_id=req_item.medicine_id,
            stock_batch_id=batch.id,
            quantity=ci.received_quantity,
            unit_price=ci.unit_price,
        )
        db.add(purchase_item)
        await db.flush()

        line_total = ci.unit_price * ci.received_quantity
        total_amount += line_total

        invoice_line_items.append(
            _InvoiceLineItem(
                medicine_name=medicine_names.get(req_item.medicine_id),
                batch_no=ci.batch_no,
                quantity=ci.received_quantity,
                unit_price=ci.unit_price,
                line_total=line_total,
            )
        )

        # requisition item আপডেট
        req_item.received_quantity = ci.received_quantity
        req_item.unit_price = ci.unit_price
        req_item.sale_price = ci.sale_price
        req_item.batch_no = ci.batch_no
        req_item.expiry_date = ci.expiry_date
        req_item.purchase_item_id = purchase_item.id

    paid_amount = payload.paid_amount or Decimal("0")
    if paid_amount > total_amount:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"paid_amount ({paid_amount}) cannot be greater than total_amount ({total_amount})",
        )
    due_amount = total_amount - paid_amount

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
    requisition.completed_at = datetime.utcnow()
    requisition.purchase_id = purchase.id

    await db.commit()
    await db.refresh(purchase)

    # --- invoice PDF auto-generate + save ---
    store = await db.get(Store, store_id)
    supplier = None
    if requisition.supplier_id is not None:
        supplier = await db.get(Supplier, requisition.supplier_id)

    pdf_bytes = _build_purchase_invoice_pdf(store, supplier, purchase, invoice_line_items)
    _save_purchase_invoice_pdf(request, pdf_bytes, purchase)

    requisition = await _get_requisition_or_404(db, requisition_id, store_id)
    return await _build_response(db, requisition, request)


# ---------------------------------------------------------------------------
# CANCEL — admin
# ---------------------------------------------------------------------------

@router.post("/{requisition_id}/cancel", response_model=RequisitionResponse)
async def cancel_requisition(
    requisition_id: int,
    payload: RequisitionCancelRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)

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
    requisition.cancelled_at = datetime.utcnow()
    if payload.reason:
        requisition.notes = f"{requisition.notes or ''}\n[Cancelled] {payload.reason}".strip()

    await db.commit()
    requisition = await _get_requisition_or_404(db, requisition_id, member.store_id)
    return await _build_response(db, requisition)