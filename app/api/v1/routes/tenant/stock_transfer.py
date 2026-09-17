"""
app/routers/stock_transfer.py

Flow: pending -> in_transit -> received (অথবা pending/in_transit -> canceled)

- POST /stock-transfers                 -> request তৈরি (status=pending), stock এখনও কাটা হয় না
                                            শুধু feasibility check (batch এ যথেষ্ট quantity আছে কিনা)
- POST /stock-transfers/{id}/dispatch   -> status=in_transit, source branch এর batch থেকে quantity বিয়োগ
- POST /stock-transfers/{id}/receive    -> status=received, destination branch এ quantity যোগ
                                            (একই batch_no + expiry_date এর batch থাকলে merge, নাহলে নতুন StockBatch তৈরি)
- POST /stock-transfers/{id}/cancel     -> pending বা in_transit থেকে cancel করা যায়;
                                            in_transit ছিল হলে dispatch এ কাটা quantity source batch এ ফেরত যায়

Concurrency: state-changing প্রতিটা endpoint এ (dispatch/receive/cancel) দুই ধরনের
row lock নেওয়া হয় —
  1. StockTransfer row নিজেই (with_for_update) — যাতে একই transfer এ দুইটা concurrent
     request এসে দুইবার status-transition/stock-deduction না ঘটে (double-dispatch bug)
  2. যে StockBatch row touch হচ্ছে সেটাও (with_for_update) — যাতে একই batch এ
     concurrent sale/transfer/return থেকে lost-update না হয়
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
from app.models.all_models import (
    Branch,
    Medicine,
    StockBatch,
    StockTransfer,
    StockTransferStatus,
    StoreMember,
)
from app.schemas.stock_transfer import (
    StockTransferCreateRequest,
    StockTransferResponse,
)

router = APIRouter(prefix="/stock-transfers", tags=["Stock Transfers"])


async def _get_transfer_or_404(
    db: AsyncSession,
    transfer_id: int,
    store_id: int,
    *,
    for_update: bool = False,
) -> StockTransfer:
    query = select(StockTransfer).where(
        StockTransfer.id == transfer_id, StockTransfer.store_id == store_id
    )
    if for_update:
        query = query.with_for_update()
    transfer = await db.scalar(query)
    if transfer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock transfer not found")
    return transfer


async def _get_batch_for_update(db: AsyncSession, batch_id: int, store_id: int) -> StockBatch | None:
    return await db.scalar(
        select(StockBatch)
        .where(StockBatch.id == batch_id, StockBatch.store_id == store_id)
        .with_for_update()
    )


async def _build_response(db: AsyncSession, transfer: StockTransfer) -> StockTransferResponse:
    medicine_name = await db.scalar(select(Medicine.name).where(Medicine.id == transfer.medicine_id))
    from_branch_name = await db.scalar(select(Branch.name).where(Branch.id == transfer.from_branch_id))
    to_branch_name = await db.scalar(select(Branch.name).where(Branch.id == transfer.to_branch_id))

    batch_no = None
    if transfer.stock_batch_id is not None:
        batch_no = await db.scalar(
            select(StockBatch.batch_no).where(StockBatch.id == transfer.stock_batch_id)
        )

    return StockTransferResponse(
        id=transfer.id,
        store_id=transfer.store_id,
        from_branch_id=transfer.from_branch_id,
        from_branch_name=from_branch_name,
        to_branch_id=transfer.to_branch_id,
        to_branch_name=to_branch_name,
        medicine_id=transfer.medicine_id,
        medicine_name=medicine_name,
        stock_batch_id=transfer.stock_batch_id,
        batch_no=batch_no,
        quantity=transfer.quantity,
        status=transfer.status,
        requested_by=transfer.requested_by,
        received_by=transfer.received_by,
        requested_at=transfer.requested_at,
        received_at=transfer.received_at,
        notes=transfer.notes,
    )


# ---------------------------------------------------------------------------
# CREATE — request তৈরি, stock এখনও touch হয় না
# ---------------------------------------------------------------------------
@router.post("", response_model=StockTransferResponse, status_code=status.HTTP_201_CREATED)
async def create_stock_transfer(
    payload: StockTransferCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.from_branch_id == payload.to_branch_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "from_branch_id and to_branch_id cannot be same")

    from_branch = await db.scalar(
        select(Branch).where(
            Branch.id == payload.from_branch_id,
            Branch.store_id == member.store_id,
            Branch.is_active == True,
        )
    )
    if from_branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid from_branch_id for this store")

    to_branch = await db.scalar(
        select(Branch).where(
            Branch.id == payload.to_branch_id,
            Branch.store_id == member.store_id,
            Branch.is_active == True,
        )
    )
    if to_branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid to_branch_id for this store")

    # এখানে lock দরকার নেই — এটা শুধু feasibility check (snapshot read),
    # আসল deduction হবে dispatch এ, lock সহ। এখানে lock নিলেও dispatch পর্যন্ত
    # হোল্ড করে রাখা যাবে না (আলাদা request/transaction), তাই অর্থহীন।
    batch = await db.scalar(
        select(StockBatch).where(
            StockBatch.id == payload.stock_batch_id,
            StockBatch.store_id == member.store_id,
            StockBatch.branch_id == payload.from_branch_id,
            StockBatch.medicine_id == payload.medicine_id,
        )
    )
    if batch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid stock_batch_id for this medicine/branch")

    if batch.quantity < payload.quantity:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Insufficient stock in source branch (available: {batch.quantity}, requested: {payload.quantity})",
        )

    transfer = StockTransfer(
        store_id=member.store_id,
        from_branch_id=payload.from_branch_id,
        to_branch_id=payload.to_branch_id,
        medicine_id=payload.medicine_id,
        stock_batch_id=payload.stock_batch_id,
        quantity=payload.quantity,
        status=StockTransferStatus.pending,
        requested_by=member.user_id,
        notes=payload.notes,
    )
    db.add(transfer)
    await db.commit()
    await db.refresh(transfer)

    return await _build_response(db, transfer)


# ---------------------------------------------------------------------------
# DISPATCH — pending -> in_transit, source batch থেকে quantity কাটা
# ---------------------------------------------------------------------------
@router.post("/{transfer_id}/dispatch", response_model=StockTransferResponse)
async def dispatch_stock_transfer(
    transfer_id: int,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    # transfer row lock — একই transfer এ দুইটা concurrent dispatch call এলে
    # দ্বিতীয়টা প্রথমটার commit পর্যন্ত block হয়ে থাকবে, ফলে status check
    # সবসময় up-to-date state এর উপর হবে (double-dispatch ঠেকায়)
    transfer = await _get_transfer_or_404(db, transfer_id, member.store_id, for_update=True)

    if transfer.status != StockTransferStatus.pending:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only pending transfers can be dispatched (current: {transfer.status.value})",
        )

    batch = await _get_batch_for_update(db, transfer.stock_batch_id, member.store_id)
    if batch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source stock batch no longer exists")

    if batch.quantity < transfer.quantity:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Insufficient stock in source branch (available: {batch.quantity}, requested: {transfer.quantity})",
        )

    batch.quantity -= transfer.quantity
    transfer.status = StockTransferStatus.in_transit

    await db.commit()
    await db.refresh(transfer)
    return await _build_response(db, transfer)


# ---------------------------------------------------------------------------
# RECEIVE — in_transit -> received, destination branch এ quantity যোগ
# ---------------------------------------------------------------------------
@router.post("/{transfer_id}/receive", response_model=StockTransferResponse)
async def receive_stock_transfer(
    transfer_id: int,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    transfer = await _get_transfer_or_404(db, transfer_id, member.store_id, for_update=True)

    if transfer.status != StockTransferStatus.in_transit:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only in_transit transfers can be received (current: {transfer.status.value})",
        )

    source_batch = await db.scalar(
        select(StockBatch).where(
            StockBatch.id == transfer.stock_batch_id, StockBatch.store_id == member.store_id
        )
    )
    if source_batch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Source stock batch no longer exists")

    # destination এ existing matching batch থাকলে সেটাও lock করে নাও (merge write এর আগে)
    dest_batch = await db.scalar(
        select(StockBatch)
        .where(
            StockBatch.store_id == member.store_id,
            StockBatch.branch_id == transfer.to_branch_id,
            StockBatch.medicine_id == transfer.medicine_id,
            StockBatch.batch_no == source_batch.batch_no,
            StockBatch.expiry_date == source_batch.expiry_date,
        )
        .with_for_update()
    )
    if dest_batch is not None:
        dest_batch.quantity += transfer.quantity
    else:
        dest_batch = StockBatch(
            store_id=member.store_id,
            branch_id=transfer.to_branch_id,
            medicine_id=transfer.medicine_id,
            batch_no=source_batch.batch_no,
            quantity=transfer.quantity,
            purchase_price=source_batch.purchase_price,
            sale_price=source_batch.sale_price,
            expiry_date=source_batch.expiry_date,
        )
        db.add(dest_batch)

    transfer.status = StockTransferStatus.received
    transfer.received_by = member.user_id
    transfer.received_at = datetime.utcnow()

    await db.commit()
    await db.refresh(transfer)
    return await _build_response(db, transfer)


# ---------------------------------------------------------------------------
# CANCEL — pending/in_transit -> canceled, in_transit হলে source এ ফেরত
# ---------------------------------------------------------------------------
@router.post("/{transfer_id}/cancel", response_model=StockTransferResponse)
async def cancel_stock_transfer(
    transfer_id: int,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    transfer = await _get_transfer_or_404(db, transfer_id, member.store_id, for_update=True)

    if transfer.status not in (StockTransferStatus.pending, StockTransferStatus.in_transit):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cannot cancel a transfer that is already {transfer.status.value}",
        )

    if transfer.status == StockTransferStatus.in_transit:
        # dispatch এর সময় source batch থেকে quantity কাটা হয়েছিল, cancel করলে ফেরত দিতে হবে
        batch = await _get_batch_for_update(db, transfer.stock_batch_id, member.store_id)
        if batch is not None:
            batch.quantity += transfer.quantity

    transfer.status = StockTransferStatus.canceled

    await db.commit()
    await db.refresh(transfer)
    return await _build_response(db, transfer)


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
@router.get("", response_model=list[StockTransferResponse])
async def list_stock_transfers(
    branch_id: int | None = Query(default=None, description="from_branch_id অথবা to_branch_id হিসেবে filter করে"),
    medicine_id: int | None = Query(default=None),
    status_filter: StockTransferStatus | None = Query(default=None, alias="status"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    from_b = aliased(Branch)
    to_b = aliased(Branch)

    query = (
        select(StockTransfer, Medicine.name, StockBatch.batch_no, from_b.name, to_b.name)
        .join(Medicine, Medicine.id == StockTransfer.medicine_id)
        .outerjoin(StockBatch, StockBatch.id == StockTransfer.stock_batch_id)
        .join(from_b, from_b.id == StockTransfer.from_branch_id)
        .join(to_b, to_b.id == StockTransfer.to_branch_id)
        .where(StockTransfer.store_id == member.store_id)
        .order_by(StockTransfer.requested_at.desc())
    )

    if branch_id is not None:
        query = query.where(
            (StockTransfer.from_branch_id == branch_id) | (StockTransfer.to_branch_id == branch_id)
        )
    if medicine_id is not None:
        query = query.where(StockTransfer.medicine_id == medicine_id)
    if status_filter is not None:
        query = query.where(StockTransfer.status == status_filter)

    result = await db.execute(query)

    return [
        StockTransferResponse(
            id=t.id,
            store_id=t.store_id,
            from_branch_id=t.from_branch_id,
            from_branch_name=from_name,
            to_branch_id=t.to_branch_id,
            to_branch_name=to_name,
            medicine_id=t.medicine_id,
            medicine_name=medicine_name,
            stock_batch_id=t.stock_batch_id,
            batch_no=batch_no,
            quantity=t.quantity,
            status=t.status,
            requested_by=t.requested_by,
            received_by=t.received_by,
            requested_at=t.requested_at,
            received_at=t.received_at,
            notes=t.notes,
        )
        for t, medicine_name, batch_no, from_name, to_name in result.all()
    ]


# ---------------------------------------------------------------------------
# GET single
# ---------------------------------------------------------------------------
@router.get("/{transfer_id}", response_model=StockTransferResponse)
async def get_stock_transfer(
    transfer_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    transfer = await _get_transfer_or_404(db, transfer_id, member.store_id)
    return await _build_response(db, transfer)