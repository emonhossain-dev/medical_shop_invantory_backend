"""
app/routers/sale_return.py

Customer return — POST /sales/{sale_id}/items/{item_id}/return

Refund হিসাব (আগে ভুল ছিল):
  1. bill-level discount বাদ দিয়ে item এর net মূল্য:  item_net = line_total × (sale.total / sale.subtotal)
  2. refund = item_net এর আনুপাতিক অংশ (শেষ return এ বাকি পুরোটা -- paisa drift এড়াতে)
  3. sale এ due থাকলে refund আগে due থেকে কাটে (due_adjusted); বাকিটা cash refund।
       cash_refund = refund_amount − due_adjusted
SaleReturn.refund_amount = ফেরত পণ্যের net মূল্য (net-sales রিপোর্টের জন্য)।
due_adjusted / cash_refund response এ আছে এবং notes এ marker হিসেবে সেভ হয়
(কারণ SaleReturn model এ আলাদা column এখনো নেই)।

Mূল Sale/SaleItem এর total/paid অপরিবর্তিত থাকে (invoice history অক্ষত)।
"""
import re
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import Medicine, Sale, SaleItem, SaleReturn, StockBatch, StoreMember
from app.schemas.sale_return import SaleReturnCreateRequest, SaleReturnResponse
from app.services.branch_scope import assert_branch_access
from app.services.unit_service import money

router = APIRouter(prefix="/sales", tags=["Sale Returns"])

_MARKER = re.compile(r"\[due_adjusted=(?P<due>-?\d+(?:\.\d+)?); cash_refund=(?P<cash>-?\d+(?:\.\d+)?)\]")


class SaleReturnResult(SaleReturnResponse):
    due_adjusted: Optional[Decimal] = None  # due থেকে কাটা অংশ
    cash_refund: Optional[Decimal] = None  # customer কে আসলে নগদ ফেরত


def _split_from_notes(notes: Optional[str], refund_amount: Decimal):
    m = _MARKER.search(notes or "")
    if m:
        return Decimal(m.group("due")), Decimal(m.group("cash"))
    return None, refund_amount  # পুরনো record: পুরোটাই cash ধরা


@router.post(
    "/{sale_id}/items/{item_id}/return",
    response_model=SaleReturnResult,
    status_code=status.HTTP_201_CREATED,
)
async def return_sale_item(
    sale_id: int,
    item_id: int,
    payload: SaleReturnCreateRequest,
    member: StoreMember = Depends(require_permission("sales", "void")),
    db: AsyncSession = Depends(get_db),
):
    # sale_item ও sale দুটোই লক -- parallel return/due-payment এ race ঠেকায়
    row = (
        await db.execute(
            select(SaleItem, Sale)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .where(
                SaleItem.id == item_id,
                SaleItem.sale_id == sale_id,
                Sale.store_id == member.store_id,
            )
            .with_for_update(of=[SaleItem, Sale])
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale item not found for this store")
    sale_item, sale = row
    assert_branch_access(member, sale.branch_id)

    if payload.quantity <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "quantity must be greater than 0")

    already_returned = await db.scalar(
        select(func.coalesce(func.sum(SaleReturn.quantity), 0)).where(
            SaleReturn.sale_item_id == sale_item.id
        )
    )
    already_refunded = await db.scalar(
        select(func.coalesce(func.sum(SaleReturn.refund_amount), 0)).where(
            SaleReturn.sale_item_id == sale_item.id
        )
    )
    available = sale_item.quantity - already_returned
    if payload.quantity > available:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"এই item এ সর্বোচ্চ {available} টা (base unit) return করা যাবে "
            f"(বিক্রি হয়েছিল {sale_item.quantity}, আগেই return হয়েছে {already_returned})",
        )

    # ---- refund হিসাব (discount-aware) ----
    if sale.subtotal and sale.subtotal > 0:
        item_net_total = money(sale_item.line_total * sale.total / sale.subtotal)
    else:
        item_net_total = money(sale_item.line_total)
    remaining_value = max(item_net_total - already_refunded, Decimal("0"))

    if payload.quantity == available:
        refund_amount = remaining_value
    else:
        refund_amount = min(money(item_net_total * payload.quantity / sale_item.quantity), remaining_value)
    base_unit_price = money(item_net_total / sale_item.quantity)

    # ---- due আগে, বাকি cash ----
    due_adjusted = Decimal("0")
    if sale.due_amount and sale.due_amount > 0:
        due_adjusted = min(sale.due_amount, refund_amount)
        sale.due_amount = sale.due_amount - due_adjusted
    cash_refund = refund_amount - due_adjusted

    # ---- restock ----
    batch = None
    batch_id_to_store = None
    if payload.restock:
        target_batch_id = payload.stock_batch_id or sale_item.stock_batch_id
        if target_batch_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "restock=true কিন্তু কোনো stock_batch_id পাওয়া যায়নি — request এ stock_batch_id দাও",
            )
        batch = await db.scalar(
            select(StockBatch)
            .where(
                StockBatch.id == target_batch_id,
                StockBatch.store_id == member.store_id,
                StockBatch.branch_id == sale.branch_id,
                StockBatch.medicine_id == sale_item.medicine_id,  # অন্য medicine এর batch এ নয়
            )
            .with_for_update()
        )
        if batch is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Invalid stock_batch_id (same branch ও same medicine এর batch হতে হবে)",
            )
        batch.quantity += payload.quantity
        batch_id_to_store = batch.id

    marker = f"[due_adjusted={due_adjusted}; cash_refund={cash_refund}]"
    notes = f"{(payload.notes or '').strip()} {marker}".strip()

    sale_return = SaleReturn(
        store_id=member.store_id,
        branch_id=sale.branch_id,
        sale_id=sale.id,
        sale_item_id=sale_item.id,
        medicine_id=sale_item.medicine_id,
        stock_batch_id=batch_id_to_store,
        quantity=payload.quantity,
        unit_price=base_unit_price,
        refund_amount=refund_amount,
        refund_method="cash",
        restocked=payload.restock,
        notes=notes,
        created_by=member.user_id,
    )
    db.add(sale_return)
    await db.commit()
    await db.refresh(sale_return)

    medicine_name = await db.scalar(select(Medicine.name).where(Medicine.id == sale_item.medicine_id))

    return SaleReturnResult(
        id=sale_return.id,
        sale_id=sale_return.sale_id,
        sale_item_id=sale_return.sale_item_id,
        medicine_id=sale_return.medicine_id,
        medicine_name=medicine_name,
        stock_batch_id=sale_return.stock_batch_id,
        quantity=sale_return.quantity,
        unit_price=sale_return.unit_price,
        refund_amount=sale_return.refund_amount,
        refund_method=sale_return.refund_method,
        restocked=sale_return.restocked,
        notes=sale_return.notes,
        created_by=sale_return.created_by,
        created_at=sale_return.created_at,
        batch_quantity_after=batch.quantity if batch is not None else None,
        due_adjusted=due_adjusted,
        cash_refund=cash_refund,
    )


@router.get("/{sale_id}/returns", response_model=list[SaleReturnResult])
async def list_sale_returns(
    sale_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    sale = await db.scalar(select(Sale).where(Sale.id == sale_id, Sale.store_id == member.store_id))
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found")
    assert_branch_access(member, sale.branch_id)

    result = await db.execute(
        select(SaleReturn, Medicine.name)
        .join(Medicine, Medicine.id == SaleReturn.medicine_id)
        .where(SaleReturn.sale_id == sale_id, SaleReturn.store_id == member.store_id)
        .order_by(SaleReturn.created_at.desc())
    )
    out = []
    for sr, medicine_name in result.all():
        due_adj, cash = _split_from_notes(sr.notes, sr.refund_amount)
        out.append(
            SaleReturnResult(
                id=sr.id,
                sale_id=sr.sale_id,
                sale_item_id=sr.sale_item_id,
                medicine_id=sr.medicine_id,
                medicine_name=medicine_name,
                stock_batch_id=sr.stock_batch_id,
                quantity=sr.quantity,
                unit_price=sr.unit_price,
                refund_amount=sr.refund_amount,
                refund_method=sr.refund_method,
                restocked=sr.restocked,
                notes=sr.notes,
                created_by=sr.created_by,
                created_at=sr.created_at,
                batch_quantity_after=None,
                due_adjusted=due_adj,
                cash_refund=cash,
            )
        )
    return out
