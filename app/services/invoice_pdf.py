# app/services/invoice_pdf.py
"""Purchase / supplier-payment invoice PDF (আগে purchase.py, supplier.py,
purchase_requisition.py -- তিন জায়গায় copy ছিল)। PDF এখন disk-এ save হয় না,
authenticated endpoint থেকে on-demand তৈরি হয়ে stream হয়।

Bengali: reportlab এর default Helvetica এ বাংলা glyph নেই। Latin + বাংলা দুটোই আছে
এমন TTF (যেমন Kalpurush / SolaimanLipi) এই path এ রাখুন (বা env দিয়ে বদলান):
    INVOICE_FONT_PATH       default static/fonts/Kalpurush.ttf
    INVOICE_FONT_BOLD_PATH  default (regular-ই ব্যবহার হবে)
সীমাবদ্ধতা: reportlab Indic shaping (যুক্তাক্ষর reordering) করে না, তাই কিছু যুক্তাক্ষর
ভাঙা দেখাতে পারে। পুরোপুরি ঠিক রাখতে HTML->PDF (WeasyPrint) লাগবে।"""
import io
import logging
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


def _register_fonts() -> None:
    global FONT, FONT_BOLD
    regular = os.getenv("INVOICE_FONT_PATH", "static/fonts/Kalpurush.ttf")
    bold = os.getenv("INVOICE_FONT_BOLD_PATH", "")
    try:
        if os.path.exists(regular):
            pdfmetrics.registerFont(TTFont("InvoiceFont", regular))
            FONT = FONT_BOLD = "InvoiceFont"
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont("InvoiceFont-Bold", bold))
                FONT_BOLD = "InvoiceFont-Bold"
        else:
            logger.warning("Invoice font %s not found; Bengali text will not render", regular)
    except Exception:  # pragma: no cover
        logger.exception("Could not register invoice font")


_register_fonts()


def _p(text, size=9, bold=False, color=colors.black) -> Paragraph:
    style = ParagraphStyle(
        "cell", fontName=FONT_BOLD if bold else FONT, fontSize=size, leading=size + 3, textColor=color
    )
    safe = str(text if text is not None else "-").replace("&", "&amp;").replace("<", "&lt;")
    return Paragraph(safe, style)


def _header(story, store, title: str) -> None:
    story.append(_p(store.name if store else "Store", size=18, bold=True))
    if store and store.address:
        story.append(_p(store.address))
    if store and store.phone:
        story.append(_p(f"Phone: {store.phone}"))
    story.append(Spacer(1, 14))
    story.append(_p(title, size=13, bold=True))
    story.append(Spacer(1, 8))


def _meta(story, purchase, supplier) -> None:
    created = purchase.created_at.strftime("%d-%m-%Y") if purchase.created_at else "-"
    rows = [
        [_p("Invoice No:", bold=True), _p(purchase.invoice_no or f"PUR-{purchase.id}"), _p("Date:", bold=True), _p(created)],
        [_p("Supplier:", bold=True), _p(supplier.name if supplier else "-"), _p("Phone:", bold=True), _p((supplier.phone if supplier else None) or "-")],
    ]
    t = Table(rows, colWidths=[70, 170, 50, 150])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(t)
    story.append(Spacer(1, 16))


def _grid(data, widths, right_cols) -> Table:
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (right_cols[0], 0), (right_cols[-1], -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _summary(story, purchase) -> None:
    rows = [
        [_p("Total Amount:", 10), _p(f"{purchase.total_amount:.2f}", 10)],
        [_p("Paid Amount:", 10), _p(f"{(purchase.paid_amount or 0):.2f}", 10)],
        [_p("Due Amount:", 10, bold=True), _p(f"{(purchase.due_amount or 0):.2f}", 10, bold=True)],
    ]
    t = Table(rows, colWidths=[120, 90], hAlign="RIGHT")
    t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, 2), (-1, 2), 0.75, colors.black)]))
    story.append(t)
    story.append(Spacer(1, 24))
    story.append(_p("This is a system-generated invoice.", 8, color=colors.grey))


def _doc():
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    return buf, doc


def build_purchase_invoice_pdf(store, supplier, purchase, items) -> bytes:
    """items: .medicine_name .batch_no .quantity_in_unit .unit_name .unit_price .line_total"""
    buf, doc = _doc()
    story: list = []
    _header(story, store, "PURCHASE INVOICE")
    _meta(story, purchase, supplier)
    h = lambda s: _p(s, bold=True, color=colors.white)  # noqa: E731
    data = [[h("#"), h("Medicine"), h("Batch"), h("Qty"), h("Unit Price"), h("Line Total")]]
    for i, it in enumerate(items, 1):
        data.append([
            _p(i), _p(it.medicine_name), _p(it.batch_no),
            _p(f"{it.quantity_in_unit} {it.unit_name or ''}".strip()),
            _p(f"{it.unit_price:.2f}"), _p(f"{it.line_total:.2f}"),
        ])
    story.append(_grid(data, [20, 135, 65, 65, 75, 80], (3, 5)))
    story.append(Spacer(1, 16))
    _summary(story, purchase)
    doc.build(story)
    return buf.getvalue()


def build_supplier_payment_pdf(store, supplier, purchase, payments) -> bytes:
    buf, doc = _doc()
    story: list = []
    _header(story, store, "SUPPLIER PAYMENT INVOICE")
    _meta(story, purchase, supplier)
    h = lambda s: _p(s, bold=True, color=colors.white)  # noqa: E731
    data = [[h("#"), h("Date"), h("Method"), h("Reference"), h("Amount")]]
    for i, p in enumerate(payments, 1):
        method = p.method.value if hasattr(p.method, "value") else str(p.method)
        data.append([
            _p(i), _p(p.created_at.strftime("%d-%m-%Y %H:%M")), _p(method.title()),
            _p(p.reference_no or "-"), _p(f"{p.amount:.2f}"),
        ])
    story.append(_grid(data, [25, 110, 70, 130, 80], (4, 4)))
    story.append(Spacer(1, 16))
    _summary(story, purchase)
    doc.build(story)
    return buf.getvalue()
