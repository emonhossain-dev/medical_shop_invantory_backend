# app/services/dashboard_service.py
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.all_models import (
    Customer,
    DuePayment,
    Medicine,
    Purchase,
    Sale,
    SaleItem,
    StockBatch,
    SupplierPayment,
)
from app.services.stock_service import StockService
from app.schemas.dashboard import (
    CustomerDueSummary,
    DashboardCharts,
    DashboardResponse,
    PeriodInfo,
    PieChartData,
    PieSlice,
    SalesSummary,
    ScopeInfo,
    StockSummary,
    SupplierDueSummary,
    TopDueCustomer,
    TrendPoint,
)

TZ_NAME = "Asia/Dhaka"
BD_TZ = ZoneInfo(TZ_NAME)


def _f(value) -> float:
    """Decimal / None -> float (JSON এ number হিসেবে যাবে, string না)"""
    return float(value or 0)


def today_bd() -> date:
    return datetime.now(BD_TZ).date()


def _bounds(d_from: date, d_to: date) -> tuple[datetime, datetime]:
    """[start, end) — Bangladesh সময় অনুযায়ী দিনের সীমা"""
    start = datetime.combine(d_from, time.min, tzinfo=BD_TZ)
    end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=BD_TZ)
    return start, end


def _growth(current: float, previous: float) -> Optional[float]:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 2)


def _pie(title: str, items: list[tuple[str, float, Optional[int]]]) -> PieChartData:
    items = [(label, value, count) for label, value, count in items if value > 0]
    total = sum(v for _, v, _ in items)
    slices = [
        PieSlice(
            label=label,
            value=round(value, 2),
            percentage=round(value / total * 100, 2) if total > 0 else 0,
            count=count,
        )
        for label, value, count in items
    ]
    return PieChartData(title=title, total=round(total, 2), slices=slices)


class DashboardService:
    def __init__(
        self,
        db: AsyncSession,
        store_id: int,
        branch_id: Optional[int] = None,
        can_see_profit: bool = False,
    ):
        self.db = db
        self.store_id = store_id
        self.branch_id = branch_id
        self.can_see_profit = can_see_profit

    # ------------------------------------------------------------------
    # filter helpers
    # ------------------------------------------------------------------
    def _sale_conds(self, start: Optional[datetime] = None, end: Optional[datetime] = None) -> list:
        conds = [Sale.store_id == self.store_id]
        if self.branch_id is not None:
            conds.append(Sale.branch_id == self.branch_id)
        if start is not None:
            conds.append(Sale.created_at >= start)
        if end is not None:
            conds.append(Sale.created_at < end)
        return conds

    def _purchase_conds(self, start: Optional[datetime] = None, end: Optional[datetime] = None) -> list:
        conds = [Purchase.store_id == self.store_id]
        if self.branch_id is not None:
            conds.append(Purchase.branch_id == self.branch_id)
        if start is not None:
            conds.append(Purchase.created_at >= start)
        if end is not None:
            conds.append(Purchase.created_at < end)
        return conds

    def _batch_conds(self) -> list:
        conds = [StockBatch.store_id == self.store_id]
        if self.branch_id is not None:
            conds.append(StockBatch.branch_id == self.branch_id)
        return conds

    # ------------------------------------------------------------------
    # sales
    # ------------------------------------------------------------------
    async def _sales_totals(self, start: datetime, end: datetime):
        q = select(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total), 0),
            func.coalesce(func.sum(Sale.paid_amount), 0),
            func.coalesce(func.sum(Sale.due_amount), 0),
            func.coalesce(func.sum(Sale.discount), 0),
        ).where(*self._sale_conds(start, end))
        row = (await self.db.execute(q)).one()
        return {
            "count": int(row[0]),
            "total": _f(row[1]),
            "paid": _f(row[2]),
            "due": _f(row[3]),
            "discount": _f(row[4]),
        }

    async def _estimated_profit(self, start: datetime, end: datetime, discount: float) -> float:
        """
        (line_total - qty * batch.purchase_price) শুধু সেই item গুলোর জন্য যাদের
        stock_batch_id আছে, তারপর sale-level discount বাদ।
        """
        q = (
            select(
                func.coalesce(
                    func.sum(SaleItem.line_total - SaleItem.quantity * StockBatch.purchase_price), 0
                )
            )
            .select_from(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .join(StockBatch, StockBatch.id == SaleItem.stock_batch_id)
            .where(*self._sale_conds(start, end))
        )
        gross = _f((await self.db.execute(q)).scalar_one())
        return round(gross - discount, 2)

    async def _sales_trend(self, d_to: date) -> list[TrendPoint]:
        first_day = d_to - timedelta(days=6)
        start, end = _bounds(first_day, d_to)

        day_col = func.date(func.timezone(TZ_NAME, Sale.created_at)).label("day")
        q = (
            select(
                day_col,
                func.coalesce(func.sum(Sale.total), 0),
                func.count(Sale.id),
            )
            .where(*self._sale_conds(start, end))
            .group_by(day_col)
        )
        rows = (await self.db.execute(q)).all()
        by_day = {r[0]: (_f(r[1]), int(r[2])) for r in rows}

        points: list[TrendPoint] = []
        for i in range(7):
            d = first_day + timedelta(days=i)
            total, cnt = by_day.get(d, (0.0, 0))
            points.append(TrendPoint(date=d, total_sales=round(total, 2), invoice_count=cnt))
        return points

    async def _top_categories(self, start: datetime, end: datetime) -> list[tuple[str, float, Optional[int]]]:
        category = func.coalesce(Medicine.category, "Uncategorized").label("category")
        q = (
            select(
                category,
                func.coalesce(func.sum(SaleItem.line_total), 0),
                func.count(func.distinct(Sale.id)),
            )
            .select_from(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .join(Medicine, Medicine.id == SaleItem.medicine_id)
            .where(*self._sale_conds(start, end))
            .group_by(category)
            .order_by(desc(func.sum(SaleItem.line_total)))
        )
        rows = (await self.db.execute(q)).all()
        items = [(r[0], _f(r[1]), int(r[2])) for r in rows]

        # top 5 + বাকি সব "Others"
        if len(items) > 6:
            top, rest = items[:5], items[5:]
            others_value = sum(v for _, v, _ in rest)
            items = top + [("Others", others_value, None)]
        return items

    # ------------------------------------------------------------------
    # customer due (receivable)
    # ------------------------------------------------------------------
    async def _customer_due(self, start: datetime, end: datetime) -> CustomerDueSummary:
        outstanding_q = select(
            func.coalesce(func.sum(Sale.due_amount), 0),
            func.count(Sale.id),
            func.count(func.distinct(Sale.customer_id)),
        ).where(*self._sale_conds(), Sale.due_amount > 0)
        o = (await self.db.execute(outstanding_q)).one()

        # due collection — DuePayment এ branch_id নেই, তাই branch scope এ Sale join করা হয়
        collected_q = select(
            func.coalesce(func.sum(DuePayment.amount), 0),
            func.count(DuePayment.id),
        ).where(
            DuePayment.store_id == self.store_id,
            DuePayment.created_at >= start,
            DuePayment.created_at < end,
        )
        if self.branch_id is not None:
            collected_q = collected_q.join(Sale, Sale.id == DuePayment.sale_id).where(
                Sale.branch_id == self.branch_id
            )
        c = (await self.db.execute(collected_q)).one()

        return CustomerDueSummary(
            total_outstanding=round(_f(o[0]), 2),
            due_invoice_count=int(o[1]),
            due_customer_count=int(o[2]),
            collected_in_period=round(_f(c[0]), 2),
            collected_count=int(c[1]),
        )

    async def _due_collection_by_method(self, start: datetime, end: datetime):
        q = (
            select(
                DuePayment.method,
                func.coalesce(func.sum(DuePayment.amount), 0),
                func.count(DuePayment.id),
            )
            .where(
                DuePayment.store_id == self.store_id,
                DuePayment.created_at >= start,
                DuePayment.created_at < end,
            )
            .group_by(DuePayment.method)
        )
        if self.branch_id is not None:
            q = q.join(Sale, Sale.id == DuePayment.sale_id).where(Sale.branch_id == self.branch_id)
        rows = (await self.db.execute(q)).all()
        return [
            (getattr(r[0], "value", str(r[0])).capitalize(), _f(r[1]), int(r[2]))
            for r in rows
        ]

    async def _top_due_customers(self, limit: int = 5) -> list[TopDueCustomer]:
        name_expr = func.coalesce(func.max(Customer.name), func.max(Sale.customer_name))
        total_due = func.sum(Sale.due_amount)
        q = (
            select(Sale.customer_id, name_expr, total_due, func.count(Sale.id))
            .select_from(Sale)
            .outerjoin(Customer, Customer.id == Sale.customer_id)
            .where(*self._sale_conds(), Sale.due_amount > 0)
            .group_by(Sale.customer_id)
            .order_by(desc(total_due))
            .limit(limit)
        )
        rows = (await self.db.execute(q)).all()
        return [
            TopDueCustomer(
                customer_id=r[0],
                name=r[1] or "Walk-in / Unknown",
                total_due=round(_f(r[2]), 2),
                invoice_count=int(r[3]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # supplier due (payable)
    # ------------------------------------------------------------------
    async def _supplier_due(self, start: datetime, end: datetime) -> SupplierDueSummary:
        outstanding_q = select(
            func.coalesce(func.sum(Purchase.due_amount), 0),
            func.count(Purchase.id),
        ).where(*self._purchase_conds(), Purchase.due_amount > 0)
        o = (await self.db.execute(outstanding_q)).one()

        purchased_q = select(
            func.coalesce(func.sum(Purchase.total_amount), 0),
            func.count(Purchase.id),
        ).where(*self._purchase_conds(start, end))
        p = (await self.db.execute(purchased_q)).one()

        paid_q = select(func.coalesce(func.sum(SupplierPayment.amount), 0)).where(
            SupplierPayment.store_id == self.store_id,
            SupplierPayment.created_at >= start,
            SupplierPayment.created_at < end,
        )
        if self.branch_id is not None:
            # SupplierPayment এ branch_id নেই -> purchase এর branch দিয়ে scope
            paid_q = paid_q.join(Purchase, Purchase.id == SupplierPayment.purchase_id).where(
                Purchase.branch_id == self.branch_id
            )
        paid = _f((await self.db.execute(paid_q)).scalar_one())

        return SupplierDueSummary(
            total_outstanding=round(_f(o[0]), 2),
            due_purchase_count=int(o[1]),
            purchased_in_period=round(_f(p[0]), 2),
            purchase_count=int(p[1]),
            paid_in_period=round(paid, 2),
        )

    # ------------------------------------------------------------------
    # stock
    # ------------------------------------------------------------------
    async def _stock(self, today: date) -> StockSummary:
        """Stock page-এর সাথে একই হিসাব (sellable qty, LEFT JOIN, active medicine) — দুই জায়গায় দুই সংখ্যা হবে না।"""
        s = await StockService(self.db, self.store_id, self.branch_id).summary()
        return StockSummary(
            low_stock_count=s.low_stock_count,
            out_of_stock_count=s.out_of_stock_count,
            expiring_soon_count=s.expiring_soon_batch_count,
            expired_count=s.expired_batch_count,
            stock_value_at_cost=round(_f(s.purchase_value), 2),
        )

    # ------------------------------------------------------------------
    # main entry
    # ------------------------------------------------------------------
    async def build(self, date_from: date, date_to: date) -> DashboardResponse:
        start, end = _bounds(date_from, date_to)

        # আগের সমান-দৈর্ঘ্যের period (growth এর জন্য)
        days = (date_to - date_from).days + 1
        prev_from = date_from - timedelta(days=days)
        prev_to = date_from - timedelta(days=1)
        prev_start, prev_end = _bounds(prev_from, prev_to)

        cur = await self._sales_totals(start, end)
        prev = await self._sales_totals(prev_start, prev_end)

        profit = None
        if self.can_see_profit:
            profit = await self._estimated_profit(start, end, cur["discount"])

        sales = SalesSummary(
            invoice_count=cur["count"],
            total_sales=round(cur["total"], 2),
            total_discount=round(cur["discount"], 2),
            paid_amount=round(cur["paid"], 2),
            due_amount=round(cur["due"], 2),
            previous_total_sales=round(prev["total"], 2),
            growth_percent=_growth(cur["total"], prev["total"]),
            estimated_gross_profit=profit,
        )

        customer_due = await self._customer_due(start, end)
        supplier_due = await self._supplier_due(start, end)
        stock = await self._stock(today_bd())
        top_due_customers = await self._top_due_customers()
        trend = await self._sales_trend(date_to)

        collection_items = await self._due_collection_by_method(start, end)
        category_items = await self._top_categories(start, end)

        charts = DashboardCharts(
            sales_composition=_pie(
                "Sales: Paid vs Due",
                [("Paid", cur["paid"], None), ("Due", cur["due"], None)],
            ),
            due_overview=_pie(
                "Receivable vs Payable",
                [
                    ("Customer Due (পাবো)", customer_due.total_outstanding, customer_due.due_invoice_count),
                    ("Supplier Due (দেবো)", supplier_due.total_outstanding, supplier_due.due_purchase_count),
                ],
            ),
            due_collection_by_method=_pie("Due Collection by Method", collection_items),
            top_categories=_pie("Sales by Category", category_items),
        )

        return DashboardResponse(
            period=PeriodInfo(date_from=date_from, date_to=date_to, timezone=TZ_NAME),
            scope=ScopeInfo(store_id=self.store_id, branch_id=self.branch_id),
            sales=sales,
            customer_due=customer_due,
            supplier_due=supplier_due,
            stock=stock,
            top_due_customers=top_due_customers,
            sales_trend=trend,
            charts=charts,
        )