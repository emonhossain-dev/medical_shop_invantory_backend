from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member
from app.models.all_models import (
    Medicine,
    StockAdjustment,
    StockAdjustmentReason,
    StockBatch,
    StoreMember,
)
from app.schemas.stock_adjustment import StockAdjustmentResponse

router = APIRouter(prefix="/stock-adjustments", tags=["Stock Adjustments"])


@router.post(
    "",
    status_code=status.HTTP_410_GONE,
    deprecated=True,
    include_in_schema=True,
)
async def create_stock_adjustment_deprecated():
    """
    এই endpoint দিয়ে আর নতুন stock adjustment তৈরি করা যাবে না।

    damage / expiry / count_correction এর জন্য POST /stock-releases ব্যবহার করো
    (app/routers/stock_release.py)। purchase_return এর জন্য
    POST /purchases/{purchase_id}/items/{item_id}/return ব্যবহার করো।
    """
    raise HTTPException(
        status.HTTP_410_GONE,
        "এই endpoint deprecated। damage/expiry/count_correction এর জন্য এখন "
        "POST /stock-releases ব্যবহার করো; purchase_return এর জন্য "
        "POST /purchases/{purchase_id}/items/{item_id}/return।",
    )


@router.get("", response_model=list[StockAdjustmentResponse], deprecated=True)
async def list_stock_adjustments(
    branch_id: int | None = Query(default=None),
    medicine_id: int | None = Query(default=None),
    reason: StockAdjustmentReason | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(StockAdjustment, Medicine.name, StockBatch.batch_no, StockBatch.quantity)
        .join(Medicine, Medicine.id == StockAdjustment.medicine_id)
        .join(StockBatch, StockBatch.id == StockAdjustment.stock_batch_id)
        .where(StockAdjustment.store_id == member.store_id)
        .order_by(StockAdjustment.created_at.desc())
    )

    if branch_id is not None:
        query = query.where(StockAdjustment.branch_id == branch_id)
    if medicine_id is not None:
        query = query.where(StockAdjustment.medicine_id == medicine_id)
    if reason is not None:
        query = query.where(StockAdjustment.reason == reason)

    result = await db.execute(query.offset(skip).limit(limit))

    return [
        StockAdjustmentResponse(
            id=adj.id,
            branch_id=adj.branch_id,
            medicine_id=adj.medicine_id,
            medicine_name=medicine_name,
            stock_batch_id=adj.stock_batch_id,
            batch_no=batch_no,
            reason=adj.reason,
            quantity_change=adj.quantity_change,
            unit_price=adj.unit_price,
            purchase_id=adj.purchase_id,
            purchase_item_id=adj.purchase_item_id,
            notes=adj.notes,
            created_by=adj.created_by,
            created_at=adj.created_at,
            batch_quantity_after=current_batch_qty,
        )
        for adj, medicine_name, batch_no, current_batch_qty in result.all()
    ]