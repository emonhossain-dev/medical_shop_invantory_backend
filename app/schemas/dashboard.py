# app/schemas/dashboard.py
from datetime import date
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Generic pie chart shape (frontend / Flutter সরাসরি PieChart এ দিতে পারবে)
# ---------------------------------------------------------------------------

class PieSlice(BaseModel):
    label: str
    value: float
    percentage: float
    count: Optional[int] = None


class PieChartData(BaseModel):
    title: str
    total: float
    slices: list[PieSlice]


# ---------------------------------------------------------------------------
# Summary blocks
# ---------------------------------------------------------------------------

class PeriodInfo(BaseModel):
    date_from: date
    date_to: date
    timezone: str


class ScopeInfo(BaseModel):
    store_id: int
    branch_id: Optional[int] = None  # None = store-wide (owner/manager)


class SalesSummary(BaseModel):
    invoice_count: int
    total_sales: float          # sum(Sale.total)  (discount এর পরের amount)
    total_discount: float
    paid_amount: float          # sum(Sale.paid_amount)
    due_amount: float           # sum(Sale.due_amount) — এই period এর sale গুলোর বর্তমান বাকি
    previous_total_sales: float
    growth_percent: Optional[float] = None  # আগের সমান-দৈর্ঘ্যের period এর তুলনায়
    estimated_gross_profit: Optional[float] = None  # শুধু owner/manager দেখতে পাবে


class CustomerDueSummary(BaseModel):
    total_outstanding: float    # সব সময়ের মোট বাকি (receivable)
    due_invoice_count: int
    due_customer_count: int
    collected_in_period: float  # এই period এ due collect হয়েছে
    collected_count: int


class SupplierDueSummary(BaseModel):
    total_outstanding: float    # supplier কে দেওয়া বাকি (payable)
    due_purchase_count: int
    purchased_in_period: float
    purchase_count: int
    paid_in_period: float       # এই period এ supplier কে payment


class StockSummary(BaseModel):
    low_stock_count: int
    out_of_stock_count: int
    expiring_soon_count: int    # ৩০ দিনের মধ্যে expire হবে (stock > 0)
    expired_count: int          # expire হয়ে গেছে কিন্তু stock > 0
    stock_value_at_cost: float


class TopDueCustomer(BaseModel):
    customer_id: Optional[int] = None
    name: str
    total_due: float
    invoice_count: int


class TrendPoint(BaseModel):
    date: date
    total_sales: float
    invoice_count: int


class DashboardCharts(BaseModel):
    sales_composition: PieChartData        # Paid vs Due (sales)
    due_overview: PieChartData             # Receivable vs Payable
    due_collection_by_method: PieChartData # cash / bkash / nagad ...
    top_categories: PieChartData           # category wise sales


class DashboardResponse(BaseModel):
    period: PeriodInfo
    scope: ScopeInfo
    sales: SalesSummary
    customer_due: CustomerDueSummary
    supplier_due: SupplierDueSummary
    stock: StockSummary
    top_due_customers: list[TopDueCustomer]
    sales_trend: list[TrendPoint]          # শেষ ৭ দিন (date_to পর্যন্ত)
    charts: DashboardCharts