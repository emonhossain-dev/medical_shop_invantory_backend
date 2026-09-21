# app/services/stock_release_service.py
"""
Stock Release (write-off / adjustment) business logic.

One call to `release()` runs in ONE database transaction and, on success, has:
  * created a StockRelease (+ StockReleaseItems)
  * locked and updated each StockBatch quantity
  * written a StockAdjustment ledger row per line (keeps existing reports working)
  * written an AuditLog row

Any failure rolls everything back, so stock and the voucher can never get out of sync.
"""

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.all_models import (
    AuditAction,
    AuditLog,
    Branch,
    Medicine,
    Purchase,
    PurchaseItem,
    StockAdjustment,
    StockAdjustmentReason,
    StockBatch,
    StockRelease,
    StockReleaseItem,
    StockReleaseReason,
    StoreMember,
    User,
)
from app.schemas.stock_release import (
    PaginatedStockReleases,
    ReleasableBatchResponse,
    StockReleaseCreateRequest,
    StockReleaseItemCreate,
    StockReleaseItemResponse,
    StockReleaseListItemResponse,
    StockReleaseResponse,
)

TWO_PLACES = Decimal("0.01")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES)


def _ledger_reason(voucher_reason: StockReleaseReason, has_purchase_item: bool) -> StockAdjustmentReason:
    if voucher_reason == StockReleaseReason.damage:
        return StockAdjustmentReason.damage
    if voucher_reason == StockReleaseReason.expiry:
        return StockAdjustmentReason.expiry
    if voucher_reason == StockReleaseReason.count_correction:
        return StockAdjustmentReason.count_correction
    # supplier_return
    return StockAdjustmentReason.purchase_return if has_purchase_item else StockAdjustmentReason.other


class StockReleaseService:
    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _guard_branch(member: StoreMember, branch_id: int) -> None:
        if member.branch_id is not None and member.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only release stock for your own branch",
            )

    # ------------------------------------------------------------------
    # RELEASE  (public entry)
    # ------------------------------------------------------------------

    async def release(
        self,
        payload: StockReleaseCreateRequest,
        *,
        member: StoreMember,
        user: User,
        idempotency_key: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> StockReleaseResponse:
        store_id = member.store_id
        key = (idempotency_key or "").strip() or None

        if key:
            existing_id = await self._release_id_by_key(store_id, key)
            if existing_id is not None:
                return await self.get(existing_id, member)

        try:
            release_id = await self._release_in_transaction(
                payload, member=member, user=user, idempotency_key=key, ip_address=ip_address
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            if key:
                existing_id = await self._release_id_by_key(store_id, key)
                if existing_id is not None:
                    return await self.get(existing_id, member)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not save the release because of a conflicting update. Please retry.",
            )
        except Exception:
            await self.db.rollback()
            raise

        return await self.get(release_id, member)

    async def _release_id_by_key(self, store_id: int, key: str) -> Optional[int]:
        return await self.db.scalar(
            select(StockRelease.id).where(
                StockRelease.store_id == store_id,
                StockRelease.idempotency_key == key,
            )
        )

    # ------------------------------------------------------------------
    # RELEASE  (transaction body)
    # ------------------------------------------------------------------

    async def _release_in_transaction(
        self,
        payload: StockReleaseCreateRequest,
        *,
        member: StoreMember,
        user: User,
        idempotency_key: Optional[str],
        ip_address: Optional[str],
    ) -> int:
        store_id = member.store_id
        self._guard_branch(member, payload.branch_id)

        branch = await self.db.scalar(
            select(Branch).where(
                Branch.id == payload.branch_id,
                Branch.store_id == store_id,
                Branch.is_active.is_(True),
            )
        )
        if branch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Branch not found")

        # Lock batches in id order to avoid deadlocks with concurrent sales/returns.
        batch_ids = sorted({line.stock_batch_id for line in payload.items})
        batches = (
            await self.db.execute(
                select(StockBatch)
                .where(
                    StockBatch.id.in_(batch_ids),
                    StockBatch.store_id == store_id,
                )
                .with_for_update()
            )
        ).scalars().all()
        batch_map: dict[int, StockBatch] = {b.id: b for b in batches}

        missing = [bid for bid in batch_ids if bid not in batch_map]
        if missing:
            raise _bad_request(f"Invalid stock_batch_id(s) for this store: {missing}")

        for bid, batch in batch_map.items():
            if batch.branch_id != payload.branch_id:
                raise _bad_request(
                    f"stock_batch_id {bid} belongs to another branch (expected branch_id={payload.branch_id})"
                )

        medicine_ids = {b.medicine_id for b in batches}
        medicines = {
            m.id: m
            for m in (
                await self.db.execute(select(Medicine).where(Medicine.id.in_(medicine_ids)))
            ).scalars().all()
        }

        released_date = payload.released_date or date.today()
        total_out = 0
        total_in = 0
        total_value = Decimal("0.00")
        prepared: list[tuple[StockReleaseItemCreate, StockBatch, Optional[int], Decimal]] = []

        for idx, line in enumerate(payload.items, start=1):
            batch = batch_map[line.stock_batch_id]
            qty_change = line.quantity if line.direction == "in" else -line.quantity
            new_qty = batch.quantity + qty_change
            if new_qty < 0:
                name = medicines[batch.medicine_id].name if batch.medicine_id in medicines else str(batch.medicine_id)
                raise _bad_request(
                    f"Line {idx} ({name}, batch {batch.batch_no or batch.id}): "
                    f"cannot release {line.quantity}; available quantity is {batch.quantity}"
                )

            purchase_id = None
            if payload.reason == StockReleaseReason.supplier_return and line.purchase_item_id:
                purchase_id = await self._validate_supplier_return(line, batch, store_id)

            unit_price = _money(batch.purchase_price)
            line_value = _money(unit_price * line.quantity)
            prepared.append((line, batch, purchase_id, line_value))

            if line.direction == "in":
                total_in += line.quantity
            else:
                total_out += line.quantity
            total_value += line_value

        release = StockRelease(
            store_id=store_id,
            branch_id=payload.branch_id,
            reason=payload.reason.value,
            released_date=released_date,
            reference_no=payload.reference_no,
            notes=payload.notes,
            total_quantity_out=total_out,
            total_quantity_in=total_in,
            total_value=total_value,
            idempotency_key=idempotency_key,
            released_by=user.id,
        )
        self.db.add(release)
        await self.db.flush()
        release.release_no = f"REL-{released_date:%Y%m}-{release.id:06d}"

        for line, batch, purchase_id, line_value in prepared:
            qty_change = line.quantity if line.direction == "in" else -line.quantity
            qty_before = batch.quantity
            batch.quantity = qty_before + qty_change

            ledger = StockAdjustment(
                store_id=store_id,
                branch_id=payload.branch_id,
                medicine_id=batch.medicine_id,
                stock_batch_id=batch.id,
                reason=_ledger_reason(payload.reason, purchase_id is not None),
                quantity_change=qty_change,
                unit_price=_money(batch.purchase_price),
                purchase_id=purchase_id,
                purchase_item_id=line.purchase_item_id,
                notes=line.notes or payload.notes,
                created_by=user.id,
            )
            self.db.add(ledger)
            await self.db.flush()

            self.db.add(
                StockReleaseItem(
                    release_id=release.id,
                    store_id=store_id,
                    medicine_id=batch.medicine_id,
                    stock_batch_id=batch.id,
                    quantity=line.quantity,
                    direction=line.direction,
                    quantity_change=qty_change,
                    quantity_before=qty_before,
                    quantity_after=batch.quantity,
                    unit_price=_money(batch.purchase_price),
                    line_value=line_value,
                    batch_no=batch.batch_no,
                    expiry_date=batch.expiry_date,
                    purchase_id=purchase_id,
                    purchase_item_id=line.purchase_item_id,
                    stock_adjustment_id=ledger.id,
                    notes=line.notes,
                )
            )

        self.db.add(
            AuditLog(
                store_id=store_id,
                user_id=user.id,
                action=AuditAction.create,
                table_name="stock_releases",
                record_id=release.id,
                new_data={
                    "release_no": release.release_no,
                    "reason": payload.reason.value,
                    "branch_id": payload.branch_id,
                    "total_quantity_out": total_out,
                    "total_quantity_in": total_in,
                    "total_value": str(total_value),
                    "items": [
                        {
                            "stock_batch_id": line.stock_batch_id,
                            "quantity": line.quantity,
                            "direction": line.direction,
                        }
                        for line, _, _, _ in prepared
                    ],
                },
                ip_address=ip_address,
            )
        )
        await self.db.flush()
        return release.id

    async def _validate_supplier_return(
        self,
        line: StockReleaseItemCreate,
        batch: StockBatch,
        store_id: int,
    ) -> int:
        item = await self.db.scalar(
            select(PurchaseItem).where(
                PurchaseItem.id == line.purchase_item_id,
                PurchaseItem.store_id == store_id,
            )
        )
        if item is None:
            raise _bad_request(f"purchase_item_id {line.purchase_item_id} was not found in this store")
        if item.stock_batch_id != batch.id:
            raise _bad_request(
                f"purchase_item_id {item.id} is linked to stock_batch_id {item.stock_batch_id}, "
                f"not {batch.id}"
            )

        purchase = await self.db.scalar(select(Purchase).where(Purchase.id == item.purchase_id))
        if purchase is None or purchase.store_id != store_id:
            raise _bad_request(f"Purchase for item {item.id} was not found")

        already_returned = await self.db.scalar(
            select(func.coalesce(func.sum(-StockAdjustment.quantity_change), 0)).where(
                StockAdjustment.purchase_item_id == item.id,
                StockAdjustment.reason == StockAdjustmentReason.purchase_return,
            )
        ) or 0
        max_returnable = min(batch.quantity, item.quantity - already_returned)
        if line.quantity > max_returnable:
            raise _bad_request(
                f"Cannot return {line.quantity} of purchase_item_id {item.id} — "
                f"max returnable is {max_returnable} "
                f"(batch stock: {batch.quantity}, already returned: {already_returned})"
            )
        return purchase.id

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------

    async def get(self, release_id: int, member: StoreMember) -> StockReleaseResponse:
        stmt = (
            select(StockRelease)
            .options(selectinload(StockRelease.items))
            .where(StockRelease.id == release_id, StockRelease.store_id == member.store_id)
        )
        if member.branch_id is not None:
            stmt = stmt.where(StockRelease.branch_id == member.branch_id)

        release = (await self.db.execute(stmt)).scalar_one_or_none()
        if release is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock release not found")
        return await self._to_response(release)

    async def _to_response(self, release: StockRelease) -> StockReleaseResponse:
        medicine_ids = {i.medicine_id for i in release.items}
        names: dict[int, str] = {}
        if medicine_ids:
            rows = (
                await self.db.execute(select(Medicine.id, Medicine.name).where(Medicine.id.in_(medicine_ids)))
            ).all()
            names = {rid: name for rid, name in rows}

        branch_name = await self.db.scalar(select(Branch.name).where(Branch.id == release.branch_id))
        released_by_name = None
        if release.released_by:
            released_by_name = await self.db.scalar(select(User.full_name).where(User.id == release.released_by))

        return StockReleaseResponse(
            id=release.id,
            store_id=release.store_id,
            branch_id=release.branch_id,
            branch_name=branch_name,
            release_no=release.release_no,
            reason=StockReleaseReason(release.reason),
            released_date=release.released_date,
            reference_no=release.reference_no,
            notes=release.notes,
            total_quantity_out=release.total_quantity_out,
            total_quantity_in=release.total_quantity_in,
            total_value=release.total_value,
            released_by=release.released_by,
            released_by_name=released_by_name,
            created_at=release.created_at,
            items=[
                StockReleaseItemResponse(
                    id=item.id,
                    medicine_id=item.medicine_id,
                    medicine_name=names.get(item.medicine_id),
                    stock_batch_id=item.stock_batch_id,
                    batch_no=item.batch_no,
                    expiry_date=item.expiry_date,
                    quantity=item.quantity,
                    direction=item.direction,  # type: ignore[arg-type]
                    quantity_change=item.quantity_change,
                    quantity_before=item.quantity_before,
                    quantity_after=item.quantity_after,
                    unit_price=item.unit_price,
                    line_value=item.line_value,
                    purchase_id=item.purchase_id,
                    purchase_item_id=item.purchase_item_id,
                    stock_adjustment_id=item.stock_adjustment_id,
                    notes=item.notes,
                )
                for item in release.items
            ],
        )

    async def list_releases(
        self,
        member: StoreMember,
        *,
        branch_id: Optional[int] = None,
        reason: Optional[StockReleaseReason] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        q: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> PaginatedStockReleases:
        conditions = [StockRelease.store_id == member.store_id]
        effective_branch = member.branch_id if member.branch_id is not None else branch_id
        if effective_branch is not None:
            conditions.append(StockRelease.branch_id == effective_branch)
        if reason is not None:
            conditions.append(StockRelease.reason == reason.value)
        if date_from is not None:
            conditions.append(StockRelease.released_date >= date_from)
        if date_to is not None:
            conditions.append(StockRelease.released_date <= date_to)
        if q:
            like = f"%{q.strip()}%"
            conditions.append(
                or_(StockRelease.release_no.ilike(like), StockRelease.reference_no.ilike(like))
            )

        total = await self.db.scalar(
            select(func.count()).select_from(StockRelease).where(*conditions)
        ) or 0

        item_count = (
            select(func.count(StockReleaseItem.id))
            .where(StockReleaseItem.release_id == StockRelease.id)
            .correlate(StockRelease)
            .scalar_subquery()
        )

        rows = (
            await self.db.execute(
                select(StockRelease, item_count.label("item_count"), Branch.name, User.full_name)
                .outerjoin(Branch, Branch.id == StockRelease.branch_id)
                .outerjoin(User, User.id == StockRelease.released_by)
                .where(*conditions)
                .order_by(StockRelease.released_date.desc(), StockRelease.id.desc())
                .offset(skip)
                .limit(limit)
            )
        ).all()

        return PaginatedStockReleases(
            items=[
                StockReleaseListItemResponse(
                    id=rel.id,
                    branch_id=rel.branch_id,
                    branch_name=branch_name,
                    release_no=rel.release_no,
                    reason=StockReleaseReason(rel.reason),
                    released_date=rel.released_date,
                    reference_no=rel.reference_no,
                    total_quantity_out=rel.total_quantity_out,
                    total_quantity_in=rel.total_quantity_in,
                    total_value=rel.total_value,
                    item_count=item_count_val or 0,
                    released_by_name=user_name,
                    created_at=rel.created_at,
                )
                for rel, item_count_val, branch_name, user_name in rows
            ],
            total=total,
            skip=skip,
            limit=limit,
        )

    async def list_batches(
        self,
        member: StoreMember,
        *,
        branch_id: int,
        medicine_id: Optional[int] = None,
        q: Optional[str] = None,
        expired_only: bool = False,
        in_stock_only: bool = True,
        limit: int = 50,
    ) -> list[ReleasableBatchResponse]:
        self._guard_branch(member, branch_id)
        today = date.today()

        stmt = (
            select(StockBatch, Medicine.name)
            .join(Medicine, Medicine.id == StockBatch.medicine_id)
            .where(
                StockBatch.store_id == member.store_id,
                StockBatch.branch_id == branch_id,
            )
            .order_by(nulls_last(StockBatch.expiry_date.asc()), StockBatch.id.asc())
            .limit(limit)
        )
        if in_stock_only:
            stmt = stmt.where(StockBatch.quantity > 0)
        if medicine_id is not None:
            stmt = stmt.where(StockBatch.medicine_id == medicine_id)
        if expired_only:
            stmt = stmt.where(StockBatch.expiry_date.is_not(None), StockBatch.expiry_date <= today)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Medicine.name.ilike(like), StockBatch.batch_no.ilike(like)))

        rows = (await self.db.execute(stmt)).all()
        return [
            ReleasableBatchResponse(
                stock_batch_id=batch.id,
                branch_id=batch.branch_id,
                medicine_id=batch.medicine_id,
                medicine_name=name,
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                quantity=batch.quantity,
                purchase_price=batch.purchase_price,
                sale_price=batch.sale_price,
                is_expired=bool(batch.expiry_date and batch.expiry_date <= today),
            )
            for batch, name in rows
        ]
