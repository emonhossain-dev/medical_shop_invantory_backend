import io
import os
import re
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
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
    MasterMedicine,
    Medicine,
    Purchase,
    PurchaseItem,
    StockBatch,
    Store,
    StoreMember,
    Supplier,
    StockAdjustment,
    StockAdjustmentReason,
    SupplierPayment,
    DuePaymentMethod,
)
from app.schemas.purchase import (
    PurchaseCreateRequest,
    PurchaseItemResponse,
    PurchaseListItemResponse,
    PurchaseResponse,
)
from app.schemas.stock_adjustment import PurchaseReturnRequest, StockAdjustmentResponse


router = APIRouter(prefix="/purchases", tags=["Purchases"])


# ---------------------------------------------------------------------------
# INVOICE PDF (NEW) — supplier.py এর pattern অনুসরণ করে
# ---------------------------------------------------------------------------

# PDF file গুলো এখানে save হবে -> main.py তে এই ফোল্ডারটাই StaticFiles দিয়ে mount করা আছে
INVOICE_PDF_DIR = "static/invoices/purchases"


def _safe_purchase_invoice_filename(purchase: Purchase) -> str:
    """invoice_no না থাকলে বা special character থাকলে safe filename বানানো"""
    raw = purchase.invoice_no or f"PUR-{purchase.id}"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    return f"{slug}.pdf"


def purchase_invoice_pdf_url(request: Request, purchase: Optional[Purchase]) -> Optional[str]:
    """List/detail response-এ দেখানোর জন্য invoice PDF-এর URL বানানো (ফাইল save না করেই)।"""
    if purchase is None:
        return None
    base_url = str(request.base_url).rstrip("/")
    filename = _safe_purchase_invoice_filename(purchase)
    return f"{base_url}/static/invoices/purchases/{filename}"


def _save_purchase_invoice_pdf(request: Request, pdf_bytes: bytes, purchase: Purchase) -> str:
    """
    PDF disk-এ save করে full URL রিটার্ন করে।
    একই purchase আবার edit/regenerate হলে ফাইল overwrite হবে -> সবসময় up-to-date থাকবে।
    """
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
    items: list[PurchaseItemResponse],
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    # --- Header: Store info ---
    title_style = ParagraphStyle("StoreTitle", parent=styles["Title"], fontSize=18, spaceAfter=2)
    story.append(Paragraph(store.name if store else "Store", title_style))
    if store and store.address:
        story.append(Paragraph(store.address, styles["Normal"]))
    if store and store.phone:
        story.append(Paragraph(f"Phone: {store.phone}", styles["Normal"]))
    story.append(Spacer(1, 14))

    story.append(Paragraph("PURCHASE INVOICE", ParagraphStyle("H2", parent=styles["Heading2"])))
    story.append(Spacer(1, 8))

    # --- Meta info: invoice no, date, supplier ---
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

    # --- Items table ---
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

    # --- Summary ---
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
    story.append(Paragraph("This is a system-generated invoice.", ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey)))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

@router.post("", response_model=PurchaseResponse, status_code=status.HTTP_201_CREATED)
async def create_purchase(
    request: Request,
    payload: PurchaseCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    store_id = member.store_id

    # --- branch validate ---
    branch = await db.scalar(
        select(Branch).where(Branch.id == payload.branch_id, Branch.store_id == store_id)
    )
    if branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid branch_id for this store")

    # --- supplier validate ---
    supplier = None
    if payload.supplier_id is not None:
        supplier = await db.scalar(
            select(Supplier).where(
                Supplier.id == payload.supplier_id, Supplier.store_id == store_id
            )
        )
        if supplier is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid supplier_id for this store")

    # --- medicine validate + auto-create from master if needed ---
    medicine_ids = [item.medicine_id for item in payload.items]

    result = await db.execute(
        select(Medicine).where(Medicine.id.in_(medicine_ids))
    )
    medicines_by_id = {m.id: m for m in result.scalars().all()}

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
                select(Medicine).where(Medicine.master_medicine_id == master_id)
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

    # --- 1) Purchase header ---
    purchase = Purchase(
        store_id=store_id,
        branch_id=payload.branch_id,
        supplier_id=payload.supplier_id,
        total_amount=Decimal("0"),
        paid_amount=Decimal("0"),
        due_amount=Decimal("0"),
        created_by=member.user_id,
    )
    db.add(purchase)
    await db.flush()

    purchase.invoice_no = f"PUR-{purchase.id:06d}"

    total_amount = Decimal("0")
    item_responses: list[PurchaseItemResponse] = []

    # --- 2 & 3) Items + StockBatch ---
    for item_in in payload.items:
        medicine = medicines_by_id[item_in.medicine_id]

        batch = StockBatch(
            store_id=store_id,
            branch_id=payload.branch_id,
            medicine_id=medicine.id,
            batch_no=item_in.batch_no,
            quantity=item_in.quantity,
            purchase_price=item_in.unit_price,
            sale_price=item_in.sale_price,
            expiry_date=item_in.expiry_date,
        )
        db.add(batch)
        await db.flush()

        line_total = item_in.unit_price * item_in.quantity
        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            store_id=store_id,
            medicine_id=medicine.id,
            stock_batch_id=batch.id,
            quantity=item_in.quantity,
            unit_price=item_in.unit_price,
        )
        db.add(purchase_item)
        await db.flush()

        total_amount += line_total

        item_responses.append(
            PurchaseItemResponse(
                id=purchase_item.id,
                medicine_id=medicine.id,
                medicine_name=medicine.name,
                stock_batch_id=batch.id,
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                quantity=item_in.quantity,
                unit_price=item_in.unit_price,
                sale_price=item_in.sale_price,
                line_total=line_total,
            )
        )

    # --- 4) total + payment হিসাব ---
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

    # --- 5) যদি payment করা হয়, SupplierPayment তৈরি করো ---
    if paid_amount > 0 and payload.supplier_id is not None:
        from app.models.all_models import DuePaymentMethod  # যদি উপরে import না থাকে

        method = DuePaymentMethod.cash
        if payload.payment_method:
            try:
                method = DuePaymentMethod(payload.payment_method.lower())
            except ValueError:
                method = DuePaymentMethod.other

        supplier_payment = SupplierPayment(
            store_id=store_id,
            supplier_id=payload.supplier_id,
            purchase_id=purchase.id,
            amount=paid_amount,
            method=method,
            reference_no=payload.payment_reference,
            note=payload.payment_note,
            paid_by=member.user_id,
        )
        db.add(supplier_payment)

    # --- 6) Commit ---
    await db.commit()
    await db.refresh(purchase)

    # --- PDF generate ---
    store = await db.get(Store, store_id)
    pdf_bytes = _build_purchase_invoice_pdf(store, supplier, purchase, item_responses)
    pdf_url = _save_purchase_invoice_pdf(request, pdf_bytes, purchase)

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
        pdf_url=pdf_url,
    )

# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

@router.get("", response_model=list[PurchaseListItemResponse])
async def list_purchases(
    request: Request,
    branch_id: int | None = Query(default=None),
    supplier_id: int | None = Query(default=None),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
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

    if branch_id is not None:
        query = query.where(Purchase.branch_id == branch_id)
    if supplier_id is not None:
        query = query.where(Purchase.supplier_id == supplier_id)

    result = await db.execute(query)

    return [
        PurchaseListItemResponse(
            id=row.Purchase.id,
            branch_id=row.Purchase.branch_id,
            supplier_id=row.Purchase.supplier_id,
            supplier_name=row.supplier_name,
            invoice_no=row.Purchase.invoice_no,
            total_amount=row.Purchase.total_amount,
            item_count=row.item_count,
            created_at=row.Purchase.created_at,
            pdf_url=purchase_invoice_pdf_url(request, row.Purchase),
        )
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------

@router.get("/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase(
    request: Request,
    purchase_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.id == purchase_id, Purchase.store_id == member.store_id
        )
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")

    supplier_name = None
    if purchase.supplier_id is not None:
        supplier_name = await db.scalar(
            select(Supplier.name).where(Supplier.id == purchase.supplier_id)
        )

    rows = await db.execute(
        select(PurchaseItem, Medicine.name, StockBatch.batch_no, StockBatch.expiry_date, StockBatch.sale_price)
        .join(Medicine, Medicine.id == PurchaseItem.medicine_id)
        .outerjoin(StockBatch, StockBatch.id == PurchaseItem.stock_batch_id)
        .where(PurchaseItem.purchase_id == purchase.id)
    )

    items = [
        PurchaseItemResponse(
            id=pi.id,
            medicine_id=pi.medicine_id,
            medicine_name=medicine_name,
            stock_batch_id=pi.stock_batch_id,
            batch_no=batch_no,
            expiry_date=expiry_date,
            quantity=pi.quantity,
            unit_price=pi.unit_price,
            sale_price=sale_price,
            line_total=pi.unit_price * pi.quantity,
        )
        for pi, medicine_name, batch_no, expiry_date, sale_price in rows.all()
    ]

    return PurchaseResponse(
        id=purchase.id,
        store_id=purchase.store_id,
        branch_id=purchase.branch_id,
        supplier_id=purchase.supplier_id,
        supplier_name=supplier_name,
        invoice_no=purchase.invoice_no,
        total_amount=purchase.total_amount,
        created_by=purchase.created_by,
        created_at=purchase.created_at,
        items=items,
        pdf_url=purchase_invoice_pdf_url(request, purchase),
    )


# ---------------------------------------------------------------------------
# INVOICE PDF DOWNLOAD
# ---------------------------------------------------------------------------

@router.get("/{purchase_id}/invoice/pdf")
async def download_purchase_invoice_pdf(
    request: Request,
    purchase_id: int,
    member: StoreMember = Depends(get_current_store_member),
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

    supplier = None
    if purchase.supplier_id is not None:
        supplier = await db.get(Supplier, purchase.supplier_id)

    store = await db.get(Store, member.store_id)

    rows = await db.execute(
        select(PurchaseItem, Medicine.name, StockBatch.batch_no, StockBatch.expiry_date, StockBatch.sale_price)
        .join(Medicine, Medicine.id == PurchaseItem.medicine_id)
        .outerjoin(StockBatch, StockBatch.id == PurchaseItem.stock_batch_id)
        .where(PurchaseItem.purchase_id == purchase.id)
    )
    items = [
        PurchaseItemResponse(
            id=pi.id,
            medicine_id=pi.medicine_id,
            medicine_name=medicine_name,
            stock_batch_id=pi.stock_batch_id,
            batch_no=batch_no,
            expiry_date=expiry_date,
            quantity=pi.quantity,
            unit_price=pi.unit_price,
            sale_price=sale_price,
            line_total=pi.unit_price * pi.quantity,
        )
        for pi, medicine_name, batch_no, expiry_date, sale_price in rows.all()
    ]

    pdf_bytes = _build_purchase_invoice_pdf(store, supplier, purchase, items)

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
)
async def return_purchase_item(
    purchase_id: int,
    item_id: int,
    payload: PurchaseReturnRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    একটা purchase item এর নির্দিষ্ট quantity সাপ্লায়ারকে ফেরত।
    Partial return সাপোর্ট করে (পুরো quantity ফেরত না দিয়ে অংশ দেওয়া যায়)।

    Rule: এখনো stock এ যতটুকু বাকি আছে (StockBatch.quantity) তার বেশি ফেরত দেওয়া যাবে না
    — কারণ ইতিমধ্যে বিক্রি হয়ে যাওয়া quantity ফেরত দেওয়ার কোনো মানে নেই।

    Concurrency: batch row FOR UPDATE লক করা হয় already_returned গণনার আগেই —
    একই batch এ একসাথে দুইটা return/sale এলে দ্বিতীয়টা প্রথমটার commit পর্যন্ত
    wait করবে, ফলে stale quantity দিয়ে হিসাব হওয়ার সুযোগ থাকে না।
    """
    purchase = await db.scalar(
        select(Purchase).where(
            Purchase.id == purchase_id, Purchase.store_id == member.store_id
        )
    )
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")

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

    adjustment = StockAdjustment(
        store_id=member.store_id,
        branch_id=purchase.branch_id,
        medicine_id=item.medicine_id,
        stock_batch_id=batch.id,
        reason=StockAdjustmentReason.purchase_return,
        quantity_change=-payload.quantity,
        unit_price=item.unit_price,
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