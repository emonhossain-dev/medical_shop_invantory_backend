from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_permission
from app.models.all_models import Customer, DuePayment, Sale, StoreMember
from app.repositories.base import BaseRepository
from app.repositories.deps import get_repository
from app.schemas.customer import (
    CustomerCreateRequest,
    CustomerDueItem,
    CustomerDueListResponse,
    CustomerFullDetailResponse,
    CustomerResponse,
    CustomerTotalDueResponse,
    CustomerUpdateRequest,
    DuePaymentSummary,
    SaleSummary,
)

router = APIRouter(prefix="/customers")


# ===========================================================================
# 1. Customer CRUD  — tag: "Customers"
#    (fixed-path due/* routes নিচে {customer_id} এর আগে রাখা হয়েছে, নাহলে
#    "due" কে customer_id হিসেবে match করার চেষ্টা করে int-parse error দিবে)
# ===========================================================================

@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED, tags=["Customers"])
async def create_customer(
    data: CustomerCreateRequest,
    member: StoreMember = Depends(require_permission("customers", "create")),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    customer = await repo.create(
        name=data.name,
        phone=data.phone,
        address=data.address,
    )
    await repo.db.commit()
    return CustomerResponse.model_validate(customer)


@router.get("", response_model=list[CustomerResponse], tags=["Customers"])
async def list_customers(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by customer name or phone"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = select(Customer).where(Customer.store_id == member.store_id)
    if search:
        query = query.where(
            or_(
                Customer.name.ilike(f"%{search}%"),
                Customer.phone.ilike(f"%{search}%"),
            )
        )
    query = query.order_by(Customer.name).offset(skip).limit(limit)

    result = await db.execute(query)
    customers = result.scalars().all()
    return [CustomerResponse.model_validate(c) for c in customers]


# ===========================================================================
# 2. Customer Due — tag: "Customer Due"
# ===========================================================================

@router.get("/due", response_model=CustomerDueListResponse, tags=["Customer Due"])
async def list_customers_due(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, description="Search by customer name or phone"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """
    সব customer এর due, group by customer — একটাই call এ।
    প্রতিটা row এ: total sale, total paid, current due, last payment date,
    এবং সবচেয়ে পুরনো unpaid sale কতদিন আগের (aging_days)।
    Default sort: সবচেয়ে বেশি due যার, সে আগে।
    """
    # Branch access control — branch-bound staff শুধু নিজের branch-এর sale/due
    # দেখতে পারবে, due_payment.py-এর _assert_branch_access-এর মতোই স্কোপ করা হলো।
    # DuePayment নিজে branch_id রাখে না, তাই Sale-এর সাথে join করে scope করা হয়েছে।
    last_payment_subq_query = (
        select(
            DuePayment.customer_id.label("customer_id"),
            func.max(DuePayment.created_at).label("last_payment_date"),
        )
        .join(Sale, DuePayment.sale_id == Sale.id)
        .where(DuePayment.store_id == member.store_id)
    )
    if member.branch_id is not None:
        last_payment_subq_query = last_payment_subq_query.where(Sale.branch_id == member.branch_id)
    last_payment_subq = last_payment_subq_query.group_by(DuePayment.customer_id).subquery()

    oldest_due_subq_query = select(
        Sale.customer_id.label("customer_id"),
        func.min(Sale.created_at).label("oldest_due_date"),
    ).where(Sale.store_id == member.store_id, Sale.due_amount > 0)
    if member.branch_id is not None:
        oldest_due_subq_query = oldest_due_subq_query.where(Sale.branch_id == member.branch_id)
    oldest_due_subq = oldest_due_subq_query.group_by(Sale.customer_id).subquery()

    base_query = (
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("name"),
            Customer.phone.label("phone"),
            func.coalesce(func.sum(Sale.total), 0).label("total_sale_amount"),
            func.coalesce(func.sum(Sale.paid_amount), 0).label("total_paid_amount"),
            func.coalesce(func.sum(Sale.due_amount), 0).label("total_due_amount"),
            last_payment_subq.c.last_payment_date,
            oldest_due_subq.c.oldest_due_date,
        )
        .join(Sale, Sale.customer_id == Customer.id)
        .outerjoin(last_payment_subq, last_payment_subq.c.customer_id == Customer.id)
        .outerjoin(oldest_due_subq, oldest_due_subq.c.customer_id == Customer.id)
        .where(Customer.store_id == member.store_id)
    )
    if member.branch_id is not None:
        # branch-bound staff -> শুধু নিজের branch-এর sale-গুলোই এই aggregation-এ ঢুকবে
        base_query = base_query.where(Sale.branch_id == member.branch_id)

    base_query = base_query.group_by(
        Customer.id,
        Customer.name,
        Customer.phone,
        last_payment_subq.c.last_payment_date,
        oldest_due_subq.c.oldest_due_date,
    ).having(func.coalesce(func.sum(Sale.due_amount), 0) > 0)

    if search:
        base_query = base_query.where(
            or_(Customer.name.ilike(f"%{search}%"), Customer.phone.ilike(f"%{search}%"))
        )

    total_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total = total_result.scalar_one()

    paged_query = base_query.order_by(desc("total_due_amount")).offset(skip).limit(limit)
    result = await db.execute(paged_query)
    rows = result.all()

    now = datetime.now(timezone.utc)
    items = [
        CustomerDueItem(
            customer_id=row.customer_id,
            name=row.name,
            phone=row.phone,
            total_sale_amount=row.total_sale_amount,
            total_paid_amount=row.total_paid_amount,
            total_due_amount=row.total_due_amount,
            last_payment_date=row.last_payment_date,
            oldest_due_date=row.oldest_due_date,
            aging_days=(now - row.oldest_due_date).days if row.oldest_due_date else None,
        )
        for row in rows
    ]

    return CustomerDueListResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/due/total", response_model=CustomerTotalDueResponse, tags=["Customer Due"])
async def get_total_customer_due(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """সব registered customer মিলিয়ে মোট combined due — এক number, এক call এ।"""
    query = select(
        func.coalesce(func.sum(Sale.due_amount), 0).label("total_due"),
        func.count(func.distinct(Sale.customer_id)).label("customers_with_due"),
    ).where(
        Sale.store_id == member.store_id,
        Sale.customer_id.isnot(None),
        Sale.due_amount > 0,
    )
    if member.branch_id is not None:
        query = query.where(Sale.branch_id == member.branch_id)
    result = await db.execute(query)
    row = result.one()
    return CustomerTotalDueResponse(total_due=row.total_due, customers_with_due=row.customers_with_due)


# ===========================================================================
# 3. Individual customer — tag: "Customers" (basic) / "Customer Details" (full)
# ===========================================================================

@router.get("/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
async def get_customer(
    customer_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    customer = await repo.get_by_id_or_404(customer_id)
    return CustomerResponse.model_validate(customer)


@router.get("/{customer_id}/details", response_model=CustomerFullDetailResponse, tags=["Customer Details"])
async def get_customer_details(
    customer_id: int,
    sales_limit: int = Query(20, ge=1, le=100, description="সম্প্রতি কয়টা sale/payment history দেখাবে"),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    """
    একজন customer এর সম্পূর্ণ প্রোফাইল একসাথে:
    - basic info
    - total sale / paid / due
    - due aging (কতদিন ধরে due আছে) + last payment date
    - সম্প্রতি sale ও due-payment history
    """
    customer_result = await db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.store_id == member.store_id)
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # Branch access control — branch-bound staff শুধু নিজের branch-এর sale/due/payment
    # তথ্যই দেখতে পারবে (due_payment.py-এর _assert_branch_access-এর মতোই স্কোপ করা হলো)।
    branch_scoped = member.branch_id is not None

    totals_query = select(
        func.coalesce(func.sum(Sale.total), 0).label("total_sale_amount"),
        func.coalesce(func.sum(Sale.paid_amount), 0).label("total_paid_amount"),
        func.coalesce(func.sum(Sale.due_amount), 0).label("total_due_amount"),
        func.count(Sale.id).label("total_sale_count"),
    ).where(Sale.customer_id == customer_id, Sale.store_id == member.store_id)
    if branch_scoped:
        totals_query = totals_query.where(Sale.branch_id == member.branch_id)
    totals_result = await db.execute(totals_query)
    totals = totals_result.one()

    oldest_due_query = select(func.min(Sale.created_at)).where(
        Sale.customer_id == customer_id,
        Sale.store_id == member.store_id,
        Sale.due_amount > 0,
    )
    if branch_scoped:
        oldest_due_query = oldest_due_query.where(Sale.branch_id == member.branch_id)
    oldest_due_date = (await db.execute(oldest_due_query)).scalar_one_or_none()
    aging_days = (datetime.now(timezone.utc) - oldest_due_date).days if oldest_due_date else None

    # DuePayment নিজে branch_id রাখে না, তাই Sale-এর সাথে join করে scope করা হয়েছে।
    last_payment_query = (
        select(func.max(DuePayment.created_at))
        .join(Sale, DuePayment.sale_id == Sale.id)
        .where(DuePayment.customer_id == customer_id, DuePayment.store_id == member.store_id)
    )
    if branch_scoped:
        last_payment_query = last_payment_query.where(Sale.branch_id == member.branch_id)
    last_payment_date = (await db.execute(last_payment_query)).scalar_one_or_none()

    sales_query = (
        select(Sale)
        .where(Sale.customer_id == customer_id, Sale.store_id == member.store_id)
        .order_by(desc(Sale.created_at))
        .limit(sales_limit)
    )
    if branch_scoped:
        sales_query = sales_query.where(Sale.branch_id == member.branch_id)
    sales = (await db.execute(sales_query)).scalars().all()

    payments_query = (
        select(DuePayment)
        .join(Sale, DuePayment.sale_id == Sale.id)
        .where(DuePayment.customer_id == customer_id, DuePayment.store_id == member.store_id)
        .order_by(desc(DuePayment.created_at))
        .limit(sales_limit)
    )
    if branch_scoped:
        payments_query = payments_query.where(Sale.branch_id == member.branch_id)
    payments = (await db.execute(payments_query)).scalars().all()

    return CustomerFullDetailResponse(
        customer=CustomerResponse.model_validate(customer),
        total_sale_amount=totals.total_sale_amount,
        total_paid_amount=totals.total_paid_amount,
        total_due_amount=totals.total_due_amount,
        total_sale_count=totals.total_sale_count,
        oldest_due_date=oldest_due_date,
        aging_days=aging_days,
        last_payment_date=last_payment_date,
        recent_sales=[SaleSummary.model_validate(s) for s in sales],
        recent_due_payments=[DuePaymentSummary.model_validate(p) for p in payments],
    )


@router.patch("/{customer_id}", response_model=CustomerResponse, tags=["Customers"])
async def update_customer(
    customer_id: int,
    data: CustomerUpdateRequest,
    member: StoreMember = Depends(require_permission("customers", "update")),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    customer = await repo.update(customer_id, **update_data)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    await repo.db.commit()
    return CustomerResponse.model_validate(customer)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Customers"])
async def delete_customer(
    customer_id: int,
    member: StoreMember = Depends(require_permission("customers", "delete")),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    # Hard delete করলে sale history/due হারিয়ে যায় (বা FK error)। transaction থাকলে block।
    has_sales = await repo.db.scalar(
        select(Sale.id)
        .where(Sale.customer_id == customer_id, Sale.store_id == member.store_id)
        .limit(1)
    )
    if has_sales is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="এই customer এর sale/due history আছে, তাই delete করা যাবে না",
        )

    deleted = await repo.delete(customer_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    await repo.db.commit()
    return None