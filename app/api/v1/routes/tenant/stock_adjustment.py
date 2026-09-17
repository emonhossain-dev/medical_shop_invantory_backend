"""
app/routers/stock_adjustment.py

Purchase return বাদে বাকি সব stock adjustment (damage, expiry, count_correction, other)।
Purchase return এর জন্য POST /purchases/{purchase_id}/items/{item_id}/return ব্যবহার করো
(app/routers/purchase.py এ)।
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
from app.models.all_models import (
    Medicine,
    StockAdjustment,
    StockAdjustmentReason,
    StockBatch,
    StoreMember,
)
from app.schemas.stock_adjustment import (
    StockAdjustmentCreateRequest,
    StockAdjustmentResponse,
)

router = APIRouter(prefix="/stock-adjustments", tags=["Stock Adjustments"])


@router.post("", response_model=StockAdjustmentResponse, status_code=status.HTTP_201_CREATED)
async def create_stock_adjustment(
    payload: StockAdjustmentCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    batch = await db.scalar(
        select(StockBatch).where(
            StockBatch.id == payload.stock_batch_id, StockBatch.store_id == member.store_id
        )
    )
    if batch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid stock_batch_id for this store")

    new_quantity = batch.quantity + payload.quantity_change
    if new_quantity < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Adjustment would make quantity negative (current: {batch.quantity}, "
            f"change: {payload.quantity_change})",
        )

    batch.quantity = new_quantity

    adjustment = StockAdjustment(
        store_id=member.store_id,
        branch_id=batch.branch_id,
        medicine_id=batch.medicine_id,
        stock_batch_id=batch.id,
        reason=payload.reason,
        quantity_change=payload.quantity_change,
        unit_price=batch.purchase_price,
        purchase_id=None,
        purchase_item_id=None,
        notes=payload.notes,
        created_by=member.user_id,
    )
    db.add(adjustment)
    await db.commit()
    await db.refresh(adjustment)

    medicine_name = await db.scalar(select(Medicine.name).where(Medicine.id == batch.medicine_id))

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


@router.get("", response_model=list[StockAdjustmentResponse])
async def list_stock_adjustments(
    branch_id: int | None = Query(default=None),
    medicine_id: int | None = Query(default=None),
    reason: StockAdjustmentReason | None = Query(default=None),
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

    result = await db.execute(query)

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