# app/services/stock_service.py
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.all_models import Medicine, StockBatch
from app.schemas.current_stock import (
    BatchStatus,
    StockBatchOut,
    StockDetailOut,
    StockItemOut,
    StockListOut,
    StockStatus,
    StockSummaryOut,
)

EXPIRY_ALERT_DAYS = 30
ZERO = Decimal("0.00")


def derive_stock_status(sellable_qty: int, reorder_level: int) -> StockStatus:
    if sellable_qty <= 0:
        return StockStatus.out_of_stock
    if sellable_qty <= reorder_level:
        return StockStatus.low_stock
    return StockStatus.in_stock


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class StockService:
    """
    Read-only current-stock queries.

    Always scoped by store_id. If branch_id is set (branch-bound staff, or an
    explicit ?branch_id= from a store-wide role) everything is scoped to that
    branch too; otherwise stock is summed across all branches of the store.
    """

    def __init__(
        self,
        db: AsyncSession,
        store_id: int,
        branch_id: Optional[int] = None,
        expiry_alert_days: int = EXPIRY_ALERT_DAYS,
    ):
        self.db = db
        self.store_id = store_id
        self.branch_id = branch_id
        self.expiry_alert_days = expiry_alert_days

    # ------------------------------------------------------------------ #
    # internal query builders
    # ------------------------------------------------------------------ #
    def _batch_join(self):
        conds = [
            StockBatch.medicine_id == Medicine.id,
            StockBatch.store_id == self.store_id,
            StockBatch.quantity > 0,
        ]
        if self.branch_id is not None:
            conds.append(StockBatch.branch_id == self.branch_id)
        return and_(*conds)

    def _grouped_stmt(
        self,
        *,
        search: Optional[str] = None,
        category: Optional[str] = None,
        include_inactive: bool = False,
    ):
        """One row per medicine (LEFT JOIN so zero-stock medicines are included)."""
        today = date.today()
        not_expired = or_(StockBatch.expiry_date.is_(None), StockBatch.expiry_date >= today)

        total_qty = func.coalesce(func.sum(StockBatch.quantity), 0)
        sellable_qty = func.coalesce(
            func.sum(case((not_expired, StockBatch.quantity), else_=0)), 0
        )
        purchase_value = func.coalesce(func.sum(StockBatch.quantity * StockBatch.purchase_price), 0)
        sale_value = func.coalesce(func.sum(StockBatch.quantity * StockBatch.sale_price), 0)
        nearest_expiry = func.min(case((StockBatch.expiry_date >= today, StockBatch.expiry_date)))

        stmt = (
            select(
                Medicine.id.label("medicine_id"),
                Medicine.name.label("name"),
                Medicine.generic_name.label("generic_name"),
                Medicine.category.label("category"),
                Medicine.unit.label("unit"),
                Medicine.reorder_level.label("reorder_level"),
                Medicine.is_active.label("is_active"),
                total_qty.label("total_quantity"),
                sellable_qty.label("sellable_quantity"),
                func.count(StockBatch.id).label("batch_count"),
                purchase_value.label("purchase_value"),
                sale_value.label("sale_value"),
                nearest_expiry.label("nearest_expiry"),
            )
            .select_from(Medicine)
            .outerjoin(StockBatch, self._batch_join())
            .where(Medicine.store_id == self.store_id)
            .group_by(Medicine.id)
        )

        if not include_inactive:
            stmt = stmt.where(Medicine.is_active.is_(True))
        if category:
            stmt = stmt.where(Medicine.category == category)
        if search:
            pattern = f"%{_escape_like(search.strip())}%"
            stmt = stmt.where(
                or_(
                    Medicine.name.ilike(pattern, escape="\\"),
                    Medicine.generic_name.ilike(pattern, escape="\\"),
                )
            )

        exprs = {
            "sellable": sellable_qty,
            "purchase_value": purchase_value,
            "nearest_expiry": nearest_expiry,
        }
        return stmt, exprs

    @staticmethod
    def _row_to_item(m) -> StockItemOut:
        total = int(m["total_quantity"])
        sellable = int(m["sellable_quantity"])
        return StockItemOut(
            medicine_id=m["medicine_id"],
            name=m["name"],
            generic_name=m["generic_name"],
            category=m["category"],
            unit=m["unit"],
            reorder_level=m["reorder_level"],
            is_active=m["is_active"],
            total_quantity=total,
            sellable_quantity=sellable,
            expired_quantity=total - sellable,
            batch_count=int(m["batch_count"]),
            purchase_value=m["purchase_value"] or ZERO,
            sale_value=m["sale_value"] or ZERO,
            nearest_expiry=m["nearest_expiry"],
            status=derive_stock_status(sellable, m["reorder_level"]),
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def list_stock(
        self,
        *,
        search: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[StockStatus] = None,
        include_inactive: bool = False,
        sort_by: str = "name",
        sort_order: str = "asc",
        skip: int = 0,
        limit: int = 50,
    ) -> StockListOut:
        stmt, x = self._grouped_stmt(
            search=search, category=category, include_inactive=include_inactive
        )

        if status == StockStatus.out_of_stock:
            stmt = stmt.having(x["sellable"] <= 0)
        elif status == StockStatus.low_stock:
            stmt = stmt.having(and_(x["sellable"] > 0, x["sellable"] <= Medicine.reorder_level))
        elif status == StockStatus.in_stock:
            stmt = stmt.having(x["sellable"] > Medicine.reorder_level)

        total = (
            await self.db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        sort_map = {
            "name": Medicine.name,
            "quantity": x["sellable"],
            "stock_value": x["purchase_value"],
            "expiry": x["nearest_expiry"],
        }
        sort_col = sort_map.get(sort_by, Medicine.name)
        ordering = sort_col.desc() if sort_order == "desc" else sort_col.asc()
        if sort_by == "expiry":
            ordering = ordering.nulls_last()

        rows = (
            await self.db.execute(stmt.order_by(ordering, Medicine.id).offset(skip).limit(limit))
        ).mappings().all()

        return StockListOut(
            items=[self._row_to_item(r) for r in rows],
            total=total,
            skip=skip,
            limit=limit,
            branch_id=self.branch_id,
        )

    async def summary(self) -> StockSummaryOut:
        today = date.today()
        grouped = self._grouped_stmt()[0].subquery()

        totals = (
            await self.db.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(grouped.c.total_quantity), 0),
                    func.coalesce(func.sum(grouped.c.purchase_value), 0),
                    func.coalesce(func.sum(grouped.c.sale_value), 0),
                    func.count().filter(grouped.c.sellable_quantity <= 0),
                    func.count().filter(
                        and_(
                            grouped.c.sellable_quantity > 0,
                            grouped.c.sellable_quantity <= grouped.c.reorder_level,
                        )
                    ),
                ).select_from(grouped)
            )
        ).one()

        batch_stmt = (
            select(
                func.count().filter(StockBatch.expiry_date < today),
                func.count().filter(
                    and_(
                        StockBatch.expiry_date >= today,
                        StockBatch.expiry_date <= today + timedelta(days=self.expiry_alert_days),
                    )
                ),
            )
            .select_from(StockBatch)
            .join(Medicine, Medicine.id == StockBatch.medicine_id)
            .where(
                StockBatch.store_id == self.store_id,
                StockBatch.quantity > 0,
                Medicine.is_active.is_(True),
            )
        )
        if self.branch_id is not None:
            batch_stmt = batch_stmt.where(StockBatch.branch_id == self.branch_id)
        expired_batches, expiring_batches = (await self.db.execute(batch_stmt)).one()

        return StockSummaryOut(
            branch_id=self.branch_id,
            total_medicines=totals[0],
            total_quantity=int(totals[1]),
            purchase_value=totals[2] or ZERO,
            sale_value=totals[3] or ZERO,
            out_of_stock_count=totals[4],
            low_stock_count=totals[5],
            expired_batch_count=expired_batches,
            expiring_soon_batch_count=expiring_batches,
            expiry_alert_days=self.expiry_alert_days,
        )

    async def get_detail(self, medicine_id: int) -> Optional[StockDetailOut]:
        medicine = (
            await self.db.execute(
                select(Medicine).where(
                    Medicine.id == medicine_id, Medicine.store_id == self.store_id
                )
            )
        ).scalar_one_or_none()
        if medicine is None:
            return None

        batch_stmt = select(StockBatch).where(
            StockBatch.store_id == self.store_id,
            StockBatch.medicine_id == medicine_id,
            StockBatch.quantity > 0,
        )
        if self.branch_id is not None:
            batch_stmt = batch_stmt.where(StockBatch.branch_id == self.branch_id)
        # FEFO: earliest expiry first, batches without expiry last
        batch_stmt = batch_stmt.order_by(
            StockBatch.expiry_date.asc().nulls_last(), StockBatch.id
        )
        batches = (await self.db.execute(batch_stmt)).scalars().all()

        today = date.today()
        alert_limit = today + timedelta(days=self.expiry_alert_days)

        batch_out: list[StockBatchOut] = []
        total = sellable = 0
        purchase_value = sale_value = ZERO
        nearest_expiry: Optional[date] = None

        for b in batches:
            if b.expiry_date is not None and b.expiry_date < today:
                b_status = BatchStatus.expired
            elif b.expiry_date is not None and b.expiry_date <= alert_limit:
                b_status = BatchStatus.expiring_soon
            else:
                b_status = BatchStatus.ok

            p_val = b.quantity * b.purchase_price
            s_val = b.quantity * b.sale_price
            total += b.quantity
            purchase_value += p_val
            sale_value += s_val
            if b_status != BatchStatus.expired:
                sellable += b.quantity
                if b.expiry_date is not None and (
                    nearest_expiry is None or b.expiry_date < nearest_expiry
                ):
                    nearest_expiry = b.expiry_date

            batch_out.append(
                StockBatchOut(
                    batch_id=b.id,
                    branch_id=b.branch_id,
                    batch_no=b.batch_no,
                    quantity=b.quantity,
                    purchase_price=b.purchase_price,
                    sale_price=b.sale_price,
                    purchase_value=p_val,
                    sale_value=s_val,
                    expiry_date=b.expiry_date,
                    days_to_expiry=(b.expiry_date - today).days if b.expiry_date else None,
                    status=b_status,
                    received_at=b.received_at,
                )
            )

        return StockDetailOut(
            medicine_id=medicine.id,
            name=medicine.name,
            generic_name=medicine.generic_name,
            category=medicine.category,
            unit=medicine.unit,
            reorder_level=medicine.reorder_level,
            is_active=medicine.is_active,
            total_quantity=total,
            sellable_quantity=sellable,
            expired_quantity=total - sellable,
            batch_count=len(batch_out),
            purchase_value=purchase_value,
            sale_value=sale_value,
            nearest_expiry=nearest_expiry,
            status=derive_stock_status(sellable, medicine.reorder_level),
            batches=batch_out,
        )