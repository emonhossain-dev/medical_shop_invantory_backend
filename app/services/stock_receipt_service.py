# app/services/stock_receipt_service.py
"""
Stock Receiving (GRN) business logic.

One call to `receive()` runs in ONE database transaction and, on success, has:
  * created a StockReceipt (+ StockReceiptItems)
  * created a Purchase (+ PurchaseItems)  -> keeps supplier dues / reports working
  * created one StockBatch per line       -> this is what actually adds stock
  * (optional) created a SupplierPayment  -> when paid_amount > 0
  * updated the requisition               -> received qty, and status -> completed when done
  * written an AuditLog row

Any failure rolls everything back, so stock and money can never get out of sync.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.all_models import (
    AuditAction,
    AuditLog,
    Branch,
    MasterMedicine,
    Medicine,
    Purchase,
    PurchaseItem,
    PurchaseRequisition,
    PurchaseRequisitionItem,
    RequisitionStatus,
    StockBatch,
    StockReceipt,
    StockReceiptItem,
    StoreMember,
    Supplier,
    SupplierPayment,
    User,
)
from app.schemas.stock_receipt import (
    MedicineSearchResult,
    OutstandingItemResponse,
    PaginatedStockReceipts,
    RequisitionOutstandingResponse,
    StockReceiptCreateRequest,
    StockReceiptItemResponse,
    StockReceiptListItemResponse,
    StockReceiptResponse,
)

TWO_PLACES = Decimal("0.01")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES)


def _approved_qty(item: PurchaseRequisitionItem) -> int:
    """Quantity we expect to receive: approved qty, falling back to requested qty."""
    return item.quantity_approved if item.quantity_approved is not None else item.quantity_requested


class StockReceiptService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @staticmethod
    def _guard_branch(member: StoreMember, branch_id: int) -> None:
        """Branch-bound staff can only work inside their own branch."""
        if member.branch_id is not None and member.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only receive stock for your own branch",
            )

    # ------------------------------------------------------------------
    # RECEIVE  (public entry point)
    # ------------------------------------------------------------------

    async def receive(
        self,
        payload: StockReceiptCreateRequest,
        *,
        member: StoreMember,
        user: User,
        idempotency_key: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> StockReceiptResponse:
        store_id = member.store_id

        # Replay of an already-processed request (client retried after a timeout)
        if idempotency_key:
            existing_id = await self._receipt_id_by_key(store_id, idempotency_key)
            if existing_id is not None:
                return await self.get(existing_id, member)

        try:
            receipt_id = await self._receive_in_transaction(
                payload, member=member, user=user,
                idempotency_key=idempotency_key, ip_address=ip_address,
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            # Two identical requests raced each other -> the loser returns the winner's receipt
            if idempotency_key:
                existing_id = await self._receipt_id_by_key(store_id, idempotency_key)
                if existing_id is not None:
                    return await self.get(existing_id, member)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not save the receipt because of a conflicting update. Please retry.",
            )
        except Exception:
            await self.db.rollback()
            raise

        return await self.get(receipt_id, member)

    async def _receipt_id_by_key(self, store_id: int, key: str) -> Optional[int]:
        return await self.db.scalar(
            select(StockReceipt.id).where(
                StockReceipt.store_id == store_id,
                StockReceipt.idempotency_key == key,
            )
        )

    # ------------------------------------------------------------------
    # RECEIVE  (transaction body)
    # ------------------------------------------------------------------

    async def _receive_in_transaction(
        self,
        payload: StockReceiptCreateRequest,
        *,
        member: StoreMember,
        user: User,
        idempotency_key: Optional[str],
        ip_address: Optional[str],
    ) -> int:
        store_id = member.store_id
        now = datetime.now(timezone.utc)

        # ---- 1. Resolve context: requisition mode vs direct mode ---------------
        requisition: Optional[PurchaseRequisition] = None
        req_items: dict[int, PurchaseRequisitionItem] = {}

        if payload.requisition_id is not None:
            # Row lock -> two people receiving the same requisition are serialized,
            # so over-receipt through a race is impossible.
            requisition = (
                await self.db.execute(
                    select(PurchaseRequisition)
                    .where(
                        PurchaseRequisition.id == payload.requisition_id,
                        PurchaseRequisition.store_id == store_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if requisition is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Requisition not found")

            self._guard_branch(member, requisition.branch_id)

            if requisition.status != RequisitionStatus.ordered:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Requisition is '{requisition.status.value}'. Only 'ordered' requisitions can be received.",
                )
            if payload.branch_id is not None and payload.branch_id != requisition.branch_id:
                raise _bad_request("branch_id does not match the requisition's branch")

            items = (
                await self.db.execute(
                    select(PurchaseRequisitionItem)
                    .where(PurchaseRequisitionItem.requisition_id == requisition.id)
                    .with_for_update()
                )
            ).scalars().all()
            req_items = {i.id: i for i in items}

            branch_id = requisition.branch_id
            supplier_id = payload.supplier_id or requisition.supplier_id
        else:
            branch_id = payload.branch_id
            self._guard_branch(member, branch_id)
            branch_ok = await self.db.scalar(
                select(Branch.id).where(
                    Branch.id == branch_id,
                    Branch.store_id == store_id,
                    Branch.is_active.is_(True),
                )
            )
            if branch_ok is None:
                raise _bad_request("Branch not found or inactive")
            supplier_id = payload.supplier_id

        if supplier_id is not None:
            supplier_ok = await self.db.scalar(
                select(Supplier.id).where(Supplier.id == supplier_id, Supplier.store_id == store_id)
            )
            if supplier_ok is None:
                raise _bad_request("Supplier not found")

        # ---- 2. Resolve every line to a medicine --------------------------------
        resolved: list[tuple] = []  # (line, medicine_id, requisition_item | None)
        incoming_per_item: dict[int, int] = defaultdict(int)
        last_line_for_item: dict[int, object] = {}
        seen_batches: set[tuple] = set()
        master_cache: dict[int, int] = {}  # master_medicine_id -> store medicine id

        for idx, line in enumerate(payload.items, start=1):
            req_item = None
            if requisition is not None:
                req_item = req_items.get(line.requisition_item_id)
                if req_item is None:
                    raise _bad_request(
                        f"Line {idx}: requisition_item_id {line.requisition_item_id} "
                        f"does not belong to this requisition"
                    )
                if line.medicine_id is not None and line.medicine_id != req_item.medicine_id:
                    raise _bad_request(f"Line {idx}: medicine_id does not match the requisition item")
                medicine_id = req_item.medicine_id
                incoming_per_item[req_item.id] += line.quantity
                last_line_for_item[req_item.id] = line
            elif line.master_medicine_id is not None:
                medicine_id = await self._medicine_from_master(
                    store_id, line.master_medicine_id, master_cache, idx
                )
            else:
                medicine_id = line.medicine_id

            # Same medicine + batch + expiry twice in one delivery is almost certainly a data-entry mistake
            if line.batch_no is not None:
                dup_key = (medicine_id, line.batch_no, line.expiry_date)
                if dup_key in seen_batches:
                    raise _bad_request(
                        f"Line {idx}: duplicate batch '{line.batch_no}' for the same medicine. "
                        f"Merge the quantities into one line."
                    )
                seen_batches.add(dup_key)

            resolved.append((line, medicine_id, req_item))

        # ---- 3. Validate medicines (store-scoped, active) -----------------------
        medicine_ids = {m for _, m, _ in resolved}
        med_rows = (
            await self.db.execute(
                select(Medicine.id, Medicine.name, Medicine.is_active).where(
                    Medicine.store_id == store_id, Medicine.id.in_(medicine_ids)
                )
            )
        ).all()
        medicines = {r.id: r for r in med_rows}
        for mid in medicine_ids:
            med = medicines.get(mid)
            if med is None:
                raise _bad_request(
                    f"Medicine {mid} not found in this store. "
                    f"If this id came from the master medicine list, send it as master_medicine_id instead of medicine_id."
                )
            if not med.is_active:
                raise _bad_request(f"Medicine '{med.name}' is inactive and cannot be received")

        # ---- 4. Over-receipt protection ----------------------------------------
        for item_id, qty in incoming_per_item.items():
            ri = req_items[item_id]
            outstanding = _approved_qty(ri) - (ri.received_quantity or 0)
            if qty > outstanding:
                raise _bad_request(
                    f"Over-receipt for '{medicines[ri.medicine_id].name}': "
                    f"outstanding {outstanding}, but {qty} was submitted"
                )

        # ---- 5. Money -----------------------------------------------------------
        line_totals = [_money(line.unit_price * line.quantity) for line, _, _ in resolved]
        total_amount = sum(line_totals, Decimal("0.00"))
        paid_amount = _money(payload.paid_amount)

        if paid_amount > total_amount:
            raise _bad_request(f"paid_amount ({paid_amount}) cannot exceed the receipt total ({total_amount})")
        if paid_amount > 0 and supplier_id is None:
            raise _bad_request("A supplier is required to record a payment")

        received_date = payload.received_date or date.today()

        # ---- 6. Purchase (financial record) -------------------------------------
        purchase = Purchase(
            store_id=store_id,
            branch_id=branch_id,
            supplier_id=supplier_id,
            total_amount=total_amount,
            paid_amount=paid_amount,
            due_amount=total_amount - paid_amount,
            created_by=user.id,
        )
        self.db.add(purchase)
        await self.db.flush()
        # Replace with your existing invoice-number helper if you already have one.
        purchase.invoice_no = f"PUR-{received_date:%Y%m%d}-{purchase.id:06d}"

        # ---- 7. Receipt header --------------------------------------------------
        receipt = StockReceipt(
            store_id=store_id,
            branch_id=branch_id,
            requisition_id=requisition.id if requisition else None,
            supplier_id=supplier_id,
            purchase_id=purchase.id,
            supplier_challan_no=payload.supplier_challan_no,
            received_date=received_date,
            total_amount=total_amount,
            notes=payload.notes,
            idempotency_key=idempotency_key,
            received_by=user.id,
        )
        self.db.add(receipt)
        await self.db.flush()  # may raise IntegrityError on a duplicate idempotency key
        receipt.receipt_no = f"GRN-{received_date:%Y%m}-{receipt.id:06d}"

        # ---- 8. StockBatch per line (this is what adds stock) -------------------
        batches: list[StockBatch] = []
        for line, medicine_id, _ in resolved:
            batch = StockBatch(
                store_id=store_id,
                branch_id=branch_id,
                medicine_id=medicine_id,
                batch_no=line.batch_no,
                quantity=line.quantity,
                purchase_price=line.unit_price,
                sale_price=line.sale_price,
                expiry_date=line.expiry_date,
            )
            self.db.add(batch)
            batches.append(batch)
        await self.db.flush()  # batch ids are needed below

        # ---- 9. PurchaseItem + StockReceiptItem per line -------------------------
        purchase_items: list[PurchaseItem] = []
        for (line, medicine_id, _), batch in zip(resolved, batches):
            p_item = PurchaseItem(
                purchase_id=purchase.id,
                store_id=store_id,
                medicine_id=medicine_id,
                stock_batch_id=batch.id,
                quantity=line.quantity,
                unit_price=line.unit_price,
            )
            self.db.add(p_item)
            purchase_items.append(p_item)
        await self.db.flush()

        for (line, medicine_id, req_item), batch, p_item, line_total in zip(
            resolved, batches, purchase_items, line_totals
        ):
            self.db.add(
                StockReceiptItem(
                    receipt_id=receipt.id,
                    store_id=store_id,
                    medicine_id=medicine_id,
                    requisition_item_id=req_item.id if req_item else None,
                    stock_batch_id=batch.id,
                    purchase_item_id=p_item.id,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    sale_price=line.sale_price,
                    batch_no=line.batch_no,
                    expiry_date=line.expiry_date,
                    line_total=line_total,
                )
            )

        # ---- 10. Supplier payment (optional) ------------------------------------
        if paid_amount > 0:
            self.db.add(
                SupplierPayment(
                    store_id=store_id,
                    supplier_id=supplier_id,
                    purchase_id=purchase.id,
                    amount=paid_amount,
                    method=payload.payment_method,
                    reference_no=payload.payment_reference,
                    note=payload.payment_note,
                    paid_by=user.id,
                )
            )

        # ---- 11. Update the requisition -----------------------------------------
        if requisition is not None:
            for item_id, qty in incoming_per_item.items():
                ri = req_items[item_id]
                ri.received_quantity = (ri.received_quantity or 0) + qty
                last = last_line_for_item[item_id]  # informational: most recent price/batch
                ri.unit_price = last.unit_price
                ri.sale_price = last.sale_price
                ri.batch_no = last.batch_no
                ri.expiry_date = last.expiry_date

            fully_received = all((i.received_quantity or 0) >= _approved_qty(i) for i in req_items.values())
            if fully_received or payload.close_requisition:
                requisition.status = RequisitionStatus.completed
                requisition.completed_by = user.id
                requisition.completed_at = now

            # Keep the existing "requisition -> purchase" link pointing at the first delivery's invoice
            if requisition.purchase_id is None:
                requisition.purchase_id = purchase.id

        # ---- 12. Audit trail -----------------------------------------------------
        self.db.add(
            AuditLog(
                store_id=store_id,
                user_id=user.id,
                action=AuditAction.create,
                table_name="stock_receipts",
                record_id=receipt.id,
                new_data={
                    "receipt_no": receipt.receipt_no,
                    "requisition_id": receipt.requisition_id,
                    "purchase_id": purchase.id,
                    "total_amount": str(total_amount),
                    "paid_amount": str(paid_amount),
                    "items": [
                        {"medicine_id": m, "quantity": l.quantity, "batch_no": l.batch_no}
                        for l, m, _ in resolved
                    ],
                },
                ip_address=ip_address,
            )
        )

        await self.db.flush()
        return receipt.id

    # ------------------------------------------------------------------
    # MASTER MEDICINE -> STORE MEDICINE (find or create)
    # ------------------------------------------------------------------

    async def _medicine_from_master(
        self, store_id: int, master_id: int, cache: dict[int, int], idx: int
    ) -> int:
        """
        Returns this store's medicine for a master medicine, creating it on first use.
        The advisory lock stops two simultaneous requests from creating duplicates.
        """
        if master_id in cache:
            return cache[master_id]

        await self.db.execute(select(func.pg_advisory_xact_lock(store_id, master_id)))

        medicine_id = await self.db.scalar(
            select(Medicine.id)
            .where(Medicine.store_id == store_id, Medicine.master_medicine_id == master_id)
            .order_by(Medicine.id)
            .limit(1)
        )
        if medicine_id is None:
            master = await self.db.scalar(
                select(MasterMedicine).where(
                    MasterMedicine.id == master_id, MasterMedicine.is_active.is_(True)
                )
            )
            if master is None:
                raise _bad_request(f"Line {idx}: master medicine {master_id} not found")

            name = f"{master.brand_name} {master.strength}".strip() if master.strength else master.brand_name
            medicine = Medicine(
                store_id=store_id,
                name=name[:200],
                generic_name=master.generic_name,
                master_medicine_id=master.id,
            )
            self.db.add(medicine)
            await self.db.flush()
            medicine_id = medicine.id

        cache[master_id] = medicine_id
        return medicine_id

    # ------------------------------------------------------------------
    # MEDICINE SEARCH (store + master)
    # ------------------------------------------------------------------

    async def search_medicines(self, store_id: int, q: str, limit: int = 20) -> list[MedicineSearchResult]:
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{term}%"

        # 1) master medicines that match
        masters = (
            await self.db.execute(
                select(MasterMedicine)
                .where(
                    MasterMedicine.is_active.is_(True),
                    or_(
                        MasterMedicine.brand_name.ilike(like, escape="\\"),
                        MasterMedicine.generic_name.ilike(like, escape="\\"),
                    ),
                )
                .order_by(MasterMedicine.brand_name)
                .limit(limit)
            )
        ).scalars().all()
        master_ids = [m.id for m in masters]

        # 2) store medicines: direct match, or already linked to one of the matching masters
        condition = or_(
            Medicine.name.ilike(like, escape="\\"),
            Medicine.generic_name.ilike(like, escape="\\"),
        )
        if master_ids:
            condition = or_(condition, Medicine.master_medicine_id.in_(master_ids))

        store_rows = (
            await self.db.execute(
                select(Medicine)
                .where(Medicine.store_id == store_id, Medicine.is_active.is_(True), condition)
                .order_by(Medicine.name)
                .limit(limit)
            )
        ).scalars().all()

        # masters already in this store (active or not) are hidden -> no duplicates in the list
        linked_master_ids: set[int] = set()
        if master_ids:
            linked_master_ids = set(
                (
                    await self.db.execute(
                        select(Medicine.master_medicine_id).where(
                            Medicine.store_id == store_id,
                            Medicine.master_medicine_id.in_(master_ids),
                        )
                    )
                ).scalars().all()
            )

        results: list[MedicineSearchResult] = [
            MedicineSearchResult(
                source="store",
                medicine_id=m.id,
                master_medicine_id=m.master_medicine_id,
                name=m.name,
                generic_name=m.generic_name,
                unit=m.unit,
            )
            for m in store_rows
        ]
        results += [
            MedicineSearchResult(
                source="master",
                master_medicine_id=m.id,
                name=f"{m.brand_name} {m.strength}".strip() if m.strength else m.brand_name,
                generic_name=m.generic_name,
                strength=m.strength,
                dosage_form=m.dosage_form,
                manufacturer=m.manufacturer,
                reference_sale_price=m.reference_sale_price,
            )
            for m in masters
            if m.id not in linked_master_ids
        ]
        return results[:limit]

    # ------------------------------------------------------------------
    # GET ONE
    # ------------------------------------------------------------------

    async def get(self, receipt_id: int, member: StoreMember) -> StockReceiptResponse:
        stmt = (
            select(StockReceipt)
            .options(selectinload(StockReceipt.items))
            .execution_options(populate_existing=True)
            .where(StockReceipt.id == receipt_id, StockReceipt.store_id == member.store_id)
        )
        if member.branch_id is not None:
            stmt = stmt.where(StockReceipt.branch_id == member.branch_id)

        receipt = (await self.db.execute(stmt)).scalar_one_or_none()
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock receipt not found")

        return await self._to_response(receipt)

    async def _to_response(self, receipt: StockReceipt) -> StockReceiptResponse:
        db = self.db

        med_ids = {i.medicine_id for i in receipt.items}
        med_names: dict[int, str] = {}
        if med_ids:
            rows = await db.execute(select(Medicine.id, Medicine.name).where(Medicine.id.in_(med_ids)))
            med_names = {r.id: r.name for r in rows}

        supplier_name = None
        if receipt.supplier_id:
            supplier_name = await db.scalar(select(Supplier.name).where(Supplier.id == receipt.supplier_id))

        received_by_name = None
        if receipt.received_by:
            received_by_name = await db.scalar(select(User.full_name).where(User.id == receipt.received_by))

        requisition_no = requisition_status = None
        if receipt.requisition_id:
            row = (
                await db.execute(
                    select(PurchaseRequisition.requisition_no, PurchaseRequisition.status).where(
                        PurchaseRequisition.id == receipt.requisition_id
                    )
                )
            ).first()
            if row:
                requisition_no, requisition_status = row.requisition_no, row.status

        invoice_no, paid_amount, due_amount = None, Decimal("0"), Decimal("0")
        if receipt.purchase_id:
            row = (
                await db.execute(
                    select(Purchase.invoice_no, Purchase.paid_amount, Purchase.due_amount).where(
                        Purchase.id == receipt.purchase_id
                    )
                )
            ).first()
            if row:
                invoice_no, paid_amount, due_amount = row.invoice_no, row.paid_amount, row.due_amount

        return StockReceiptResponse(
            id=receipt.id,
            store_id=receipt.store_id,
            branch_id=receipt.branch_id,
            receipt_no=receipt.receipt_no,
            requisition_id=receipt.requisition_id,
            requisition_no=requisition_no,
            requisition_status=requisition_status,
            supplier_id=receipt.supplier_id,
            supplier_name=supplier_name,
            supplier_challan_no=receipt.supplier_challan_no,
            purchase_id=receipt.purchase_id,
            invoice_no=invoice_no,
            total_amount=receipt.total_amount,
            paid_amount=paid_amount,
            due_amount=due_amount,
            received_date=receipt.received_date,
            notes=receipt.notes,
            received_by=receipt.received_by,
            received_by_name=received_by_name,
            created_at=receipt.created_at,
            items=[
                StockReceiptItemResponse(
                    id=i.id,
                    medicine_id=i.medicine_id,
                    medicine_name=med_names.get(i.medicine_id),
                    requisition_item_id=i.requisition_item_id,
                    stock_batch_id=i.stock_batch_id,
                    purchase_item_id=i.purchase_item_id,
                    batch_no=i.batch_no,
                    expiry_date=i.expiry_date,
                    quantity=i.quantity,
                    unit_price=i.unit_price,
                    sale_price=i.sale_price,
                    line_total=i.line_total,
                )
                for i in receipt.items
            ],
        )

    # ------------------------------------------------------------------
    # LIST
    # ------------------------------------------------------------------

    async def list_receipts(
        self,
        member: StoreMember,
        *,
        branch_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        requisition_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        q: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> PaginatedStockReceipts:
        conditions = [StockReceipt.store_id == member.store_id]

        # Branch-bound staff are always pinned to their own branch
        effective_branch = member.branch_id if member.branch_id is not None else branch_id
        if effective_branch is not None:
            conditions.append(StockReceipt.branch_id == effective_branch)
        if supplier_id is not None:
            conditions.append(StockReceipt.supplier_id == supplier_id)
        if requisition_id is not None:
            conditions.append(StockReceipt.requisition_id == requisition_id)
        if date_from is not None:
            conditions.append(StockReceipt.received_date >= date_from)
        if date_to is not None:
            conditions.append(StockReceipt.received_date <= date_to)
        if q:
            like = f"%{q.strip()}%"
            conditions.append(
                StockReceipt.receipt_no.ilike(like) | StockReceipt.supplier_challan_no.ilike(like)
            )

        total = await self.db.scalar(select(func.count()).select_from(StockReceipt).where(*conditions)) or 0

        item_count = (
            select(func.count(StockReceiptItem.id))
            .where(StockReceiptItem.receipt_id == StockReceipt.id)
            .correlate(StockReceipt)
            .scalar_subquery()
        )
        stmt = (
            select(
                StockReceipt,
                Supplier.name,
                PurchaseRequisition.requisition_no,
                User.full_name,
                item_count,
            )
            .outerjoin(Supplier, Supplier.id == StockReceipt.supplier_id)
            .outerjoin(PurchaseRequisition, PurchaseRequisition.id == StockReceipt.requisition_id)
            .outerjoin(User, User.id == StockReceipt.received_by)
            .where(*conditions)
            .order_by(StockReceipt.received_date.desc(), StockReceipt.id.desc())
            .offset(skip)
            .limit(limit)
        )
        rows = (await self.db.execute(stmt)).all()

        return PaginatedStockReceipts(
            items=[
                StockReceiptListItemResponse(
                    id=r.id,
                    branch_id=r.branch_id,
                    receipt_no=r.receipt_no,
                    requisition_id=r.requisition_id,
                    requisition_no=req_no,
                    supplier_id=r.supplier_id,
                    supplier_name=sup_name,
                    supplier_challan_no=r.supplier_challan_no,
                    received_date=r.received_date,
                    total_amount=r.total_amount,
                    item_count=count or 0,
                    received_by_name=user_name,
                    created_at=r.created_at,
                )
                for r, sup_name, req_no, user_name, count in rows
            ],
            total=total,
            skip=skip,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # OUTSTANDING (receiving screen: "what are we still waiting for?")
    # ------------------------------------------------------------------

    async def outstanding(self, requisition_id: int, member: StoreMember) -> RequisitionOutstandingResponse:
        requisition = (
            await self.db.execute(
                select(PurchaseRequisition).where(
                    PurchaseRequisition.id == requisition_id,
                    PurchaseRequisition.store_id == member.store_id,
                )
            )
        ).scalar_one_or_none()
        if requisition is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Requisition not found")
        self._guard_branch(member, requisition.branch_id)

        rows = (
            await self.db.execute(
                select(PurchaseRequisitionItem, Medicine.name)
                .join(Medicine, Medicine.id == PurchaseRequisitionItem.medicine_id)
                .where(PurchaseRequisitionItem.requisition_id == requisition.id)
                .order_by(PurchaseRequisitionItem.id)
            )
        ).all()

        supplier_name = None
        if requisition.supplier_id:
            supplier_name = await self.db.scalar(select(Supplier.name).where(Supplier.id == requisition.supplier_id))

        items = []
        for ri, med_name in rows:
            ordered = _approved_qty(ri)
            received = ri.received_quantity or 0
            items.append(
                OutstandingItemResponse(
                    requisition_item_id=ri.id,
                    medicine_id=ri.medicine_id,
                    medicine_name=med_name,
                    quantity_ordered=ordered,
                    quantity_received=received,
                    quantity_outstanding=max(ordered - received, 0),
                    estimated_unit_price=ri.estimated_unit_price,
                )
            )

        return RequisitionOutstandingResponse(
            requisition_id=requisition.id,
            requisition_no=requisition.requisition_no,
            status=requisition.status,
            can_receive=requisition.status == RequisitionStatus.ordered,
            branch_id=requisition.branch_id,
            supplier_id=requisition.supplier_id,
            supplier_name=supplier_name,
            items=items,
        )