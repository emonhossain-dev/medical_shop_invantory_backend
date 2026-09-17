import io
import os
import re
from datetime import datetime, date
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
from app.models.all_models import StoreMember, Supplier, Purchase, SupplierPayment, Store, DuePaymentMethod
from app.repositories.base import BaseRepository
from app.repositories.deps import get_repository
from app.schemas.supplier import SupplierCreateRequest, SupplierUpdateRequest, SupplierResponse
from app.schemas.supplier_payment import (
    SupplierPaymentCreateRequest,
    SupplierPaymentResponse,
    SupplierPaymentWithInvoiceResponse,
    SupplierPaymentListItem,
    SupplierLedgerSummary,
    SupplierInvoiceResponse,
    SupplierPaymentUpdateRequest
)

router = APIRouter(prefix="/suppliers", tags=["Supplier"])


@router.post("", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    data: SupplierCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    supplier = await repo.create(
        name=data.name,
        phone=data.phone,
        address=data.address,
    )
    await repo.db.commit()
    return SupplierResponse.model_validate(supplier)


@router.get("", response_model=list[SupplierResponse])
async def list_suppliers(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by supplier name"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = select(Supplier).where(Supplier.store_id == member.store_id)
    if search:
        query = query.where(Supplier.name.ilike(f"%{search}%"))
    query = query.order_by(Supplier.name).offset(skip).limit(limit)

    result = await db.execute(query)
    suppliers = result.scalars().all()
    return [SupplierResponse.model_validate(s) for s in suppliers]


@router.get("/{supplier_id}", response_model=SupplierResponse)
async def get_supplier(
    supplier_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    supplier = await repo.get_by_id_or_404(supplier_id)
    return SupplierResponse.model_validate(supplier)


@router.patch("/{supplier_id}", response_model=SupplierResponse)
async def update_supplier(
    supplier_id: int,
    data: SupplierUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
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


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_supplier(
    supplier_id: int,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Supplier] = Depends(get_repository(Supplier)),
):
    deleted = await repo.delete(supplier_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    await repo.db.commit()
    return "Successfully Deleted Supplier"


# ---------------------------------------------------------------------------
# SUPPLIER PAYMENT + LEDGER (NEW)
# ---------------------------------------------------------------------------

def _generate_invoice_no(supplier_id: int) -> str:
    """
    Payment দেওয়ার সময় purchase_id না দিলে auto-generated invoice number।
    চাইলে এখানে store-wise sequence বা আপনার নিজস্ব pattern বসাতে পারেন।
    """
    ts = int(datetime.utcnow().timestamp())
    return f"SPI-{supplier_id}-{ts}"


@router.post(
    "/{supplier_id}/payments",
    response_model=SupplierPaymentWithInvoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def pay_supplier(
    request: Request,
    supplier_id: int,
    data: SupplierPaymentCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Supplier-কে payment দেওয়ার API।

    - `purchase_id` দিলে: সেই নির্দিষ্ট purchase-এর due amount থেকে কমবে (over-payment হলে error)।
    - `purchase_id` না দিলে: automatically একটা নতুন Purchase (invoice_no সহ) তৈরি হবে,
      যেখানে total_amount = paid_amount = payment amount, due_amount = 0 —
      অর্থাৎ payment দেওয়া মাত্রই invoice তৈরি হয়ে যাবে।
    - Payment successful হলে automatically PDF invoice generate হয়ে disk-এ save হবে,
      response-এ `pdf_url` দিয়ে সেটা সরাসরি browser-এ খুলে দেখা যাবে।
    """
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    purchase: Optional[Purchase] = None

    if data.purchase_id:
        purchase = await db.get(Purchase, data.purchase_id)
        if not purchase or purchase.store_id != member.store_id or purchase.supplier_id != supplier_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="এই supplier-এর জন্য এই purchase পাওয়া যায়নি",
            )

        if data.amount > purchase.due_amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Payment amount ({data.amount}) due amount ({purchase.due_amount}) থেকে বেশি হতে পারবে না",
            )

        purchase.paid_amount = purchase.paid_amount + data.amount
        purchase.due_amount = purchase.due_amount - data.amount

    else:
        # purchase_id দেওয়া হয়নি -> নতুন invoice/purchase auto তৈরি হবে
        branch_id = data.branch_id or member.branch_id
        if not branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="branch_id দিতে হবে (আপনার account-এ কোনো default branch সেট করা নেই)",
            )

        purchase = Purchase(
            store_id=member.store_id,
            branch_id=branch_id,
            supplier_id=supplier_id,
            invoice_no=_generate_invoice_no(supplier_id),
            total_amount=data.amount,
            paid_amount=data.amount,
            due_amount=0,
            created_by=member.user_id,
        )
        db.add(purchase)
        await db.flush()  # purchase.id পাওয়ার জন্য commit-এর আগে flush

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

    # --- এই purchase-এর বিপরীতে হওয়া সব payment নিয়ে PDF-এ full history দেখানো ---
    payments_query = (
        select(SupplierPayment)
        .where(SupplierPayment.purchase_id == purchase.id, SupplierPayment.store_id == member.store_id)
        .order_by(SupplierPayment.created_at)
    )
    payments_result = await db.execute(payments_query)
    all_payments = payments_result.scalars().all()

    pdf_bytes = _build_invoice_pdf(
        store=await db.get(Store, member.store_id),
        supplier=supplier,
        purchase=purchase,
        payments=all_payments,
    )
    pdf_url = _save_invoice_pdf(request, pdf_bytes, purchase)

    return SupplierPaymentWithInvoiceResponse(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no,
        purchase_total_amount=purchase.total_amount,
        purchase_paid_amount=purchase.paid_amount,
        purchase_due_amount=purchase.due_amount,
        pdf_url=pdf_url,
    )


@router.get("/{supplier_id}/payments", response_model=list[SupplierPaymentWithInvoiceResponse])
async def list_supplier_payments(
    request: Request,
    supplier_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    member: StoreMember = Depends(get_current_store_member),
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
        )
        .order_by(SupplierPayment.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.all()

    base_url = str(request.base_url).rstrip("/")

    return [
        SupplierPaymentWithInvoiceResponse(
            **SupplierPaymentResponse.model_validate(payment).model_dump(),
            invoice_no=purchase.invoice_no if purchase else None,
            purchase_total_amount=purchase.total_amount if purchase else None,
            purchase_paid_amount=purchase.paid_amount if purchase else None,
            purchase_due_amount=purchase.due_amount if purchase else None,
            pdf_url=f"{base_url}/static/invoices/{_safe_invoice_filename(purchase)}" if purchase else None,
        )
        for payment, purchase in rows
    ]


@router.get("/{supplier_id}/invoices/{purchase_id}", response_model=SupplierInvoiceResponse)
async def get_supplier_invoice(
    supplier_id: int,
    purchase_id: int,
    member: StoreMember = Depends(get_current_store_member),
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


@router.get("/{supplier_id}/ledger", response_model=SupplierLedgerSummary)
async def supplier_ledger(
    supplier_id: int,
    member: StoreMember = Depends(get_current_store_member),
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
# INVOICE PDF (NEW)
# ---------------------------------------------------------------------------

# PDF file গুলো এখানে save হবে -> main.py তে এই ফোল্ডারটাই StaticFiles দিয়ে mount করতে হবে
INVOICE_PDF_DIR = "static/invoices"


def _safe_invoice_filename(purchase: Purchase) -> str:
    """invoice_no না থাকলে বা special character থাকলে safe filename বানানো"""
    raw = purchase.invoice_no or f"PUR-{purchase.id}"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    return f"{slug}.pdf"


def _save_invoice_pdf(request: Request, pdf_bytes: bytes, purchase: Purchase) -> str:
    """
    PDF disk-এ save করে full URL রিটার্ন করে।
    একই purchase-এর বিপরীতে আবার payment হলে ফাইল overwrite হবে -> সবসময় up-to-date থাকবে।
    """
    os.makedirs(INVOICE_PDF_DIR, exist_ok=True)
    filename = _safe_invoice_filename(purchase)
    file_path = os.path.join(INVOICE_PDF_DIR, filename)

    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/static/invoices/{filename}"


def _build_invoice_pdf(store: Optional[Store], supplier: Supplier, purchase: Purchase, payments: list[SupplierPayment]) -> bytes:
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

    story.append(Paragraph("SUPPLIER PAYMENT INVOICE", ParagraphStyle("H2", parent=styles["Heading2"])))
    story.append(Spacer(1, 8))

    # --- Meta info: invoice no, date, supplier ---
    meta_data = [
        ["Invoice No:", purchase.invoice_no or f"PUR-{purchase.id}", "Date:", purchase.created_at.strftime("%d-%m-%Y")],
        ["Supplier:", supplier.name, "Phone:", supplier.phone or "-"],
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

    # --- Payments table ---
    data = [["#", "Date", "Method", "Reference", "Amount"]]
    for i, p in enumerate(payments, 1):
        method_label = p.method.value if hasattr(p.method, "value") else str(p.method)
        data.append([
            str(i),
            p.created_at.strftime("%d-%m-%Y %H:%M"),
            method_label.title(),
            p.reference_no or "-",
            f"{p.amount:.2f}",
        ])

    table = Table(data, colWidths=[25, 110, 70, 130, 80])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (4, 0), (4, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 16))

    # --- Summary ---
    summary_data = [
        ["Total Amount:", f"{purchase.total_amount:.2f}"],
        ["Paid Amount:", f"{purchase.paid_amount:.2f}"],
        ["Due Amount:", f"{purchase.due_amount:.2f}"],
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


@router.get("/{supplier_id}/invoices/{purchase_id}/pdf")
async def download_supplier_invoice_pdf(
    supplier_id: int,
    purchase_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """নির্দিষ্ট supplier invoice-এর PDF download।"""
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.store_id != member.store_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")

    purchase = await db.get(Purchase, purchase_id)
    if not purchase or purchase.store_id != member.store_id or purchase.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice পাওয়া যায়নি")

    store = await db.get(Store, member.store_id)

    query = (
        select(SupplierPayment)
        .where(SupplierPayment.purchase_id == purchase_id, SupplierPayment.store_id == member.store_id)
        .order_by(SupplierPayment.created_at)
    )
    result = await db.execute(query)
    payments = result.scalars().all()

    pdf_bytes = _build_invoice_pdf(store, supplier, purchase, payments)

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

payments_router = APIRouter(prefix="/payments", tags=["Supplier Payments"])


def _build_payment_list_item(payment: SupplierPayment, purchase: Optional[Purchase], supplier: Supplier, base_url: str) -> SupplierPaymentListItem:
    """SupplierPayment + Purchase + Supplier row থেকে response item বানানোর helper (কোড ডুপ্লিকেশন এড়াতে)।"""
    return SupplierPaymentListItem(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no if purchase else None,
        purchase_total_amount=purchase.total_amount if purchase else None,
        purchase_paid_amount=purchase.paid_amount if purchase else None,
        purchase_due_amount=purchase.due_amount if purchase else None,
        pdf_url=f"{base_url}/static/invoices/{_safe_invoice_filename(purchase)}" if purchase else None,
        supplier_name=supplier.name,
    )

@router.get("Payment", response_model=list[SupplierPaymentListItem])
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
    member: StoreMember = Depends(get_current_store_member),
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
    if date_from:
        query = query.where(SupplierPayment.created_at >= date_from)
    if date_to:
        query = query.where(SupplierPayment.created_at <= date_to)

    if only_mine:
        query = query.where(SupplierPayment.paid_by == member.user_id)
    elif paid_by:
        query = query.where(SupplierPayment.paid_by == paid_by)

    query = query.order_by(SupplierPayment.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    base_url = str(request.base_url).rstrip("/")

    return [_build_payment_list_item(payment, purchase, supplier, base_url) for payment, purchase, supplier in rows]





@router.patch(
    "/{supplier_id}/payments/{payment_id}",
    response_model=SupplierPaymentWithInvoiceResponse,
)
async def update_supplier_payment(
    request: Request,
    supplier_id: int,
    payment_id: int,
    data: SupplierPaymentUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
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

    payment = await db.get(SupplierPayment, payment_id)
    if not payment or payment.store_id != member.store_id or payment.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment পাওয়া যায়নি")

    purchase: Optional[Purchase] = None
    if payment.purchase_id:
        purchase = await db.get(Purchase, payment.purchase_id)

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

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

    await db.commit()
    await db.refresh(payment)
    if purchase is not None:
        await db.refresh(purchase)

    pdf_url = None
    if purchase is not None:
        payments_query = (
            select(SupplierPayment)
            .where(SupplierPayment.purchase_id == purchase.id, SupplierPayment.store_id == member.store_id)
            .order_by(SupplierPayment.created_at)
        )
        payments_result = await db.execute(payments_query)
        all_payments = payments_result.scalars().all()

        pdf_bytes = _build_invoice_pdf(
            store=await db.get(Store, member.store_id),
            supplier=supplier,
            purchase=purchase,
            payments=all_payments,
        )
        pdf_url = _save_invoice_pdf(request, pdf_bytes, purchase)

    return SupplierPaymentWithInvoiceResponse(
        **SupplierPaymentResponse.model_validate(payment).model_dump(),
        invoice_no=purchase.invoice_no if purchase else None,
        purchase_total_amount=purchase.total_amount if purchase else None,
        purchase_paid_amount=purchase.paid_amount if purchase else None,
        purchase_due_amount=purchase.due_amount if purchase else None,
        pdf_url=pdf_url,
    )