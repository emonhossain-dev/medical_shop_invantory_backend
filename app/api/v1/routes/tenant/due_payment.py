from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_user, get_current_store_member
from app.models.all_models import (
    Sale,
    DuePayment,
    Customer,
    StoreMember,
    User,
)
from app.schemas.due_payment import (
    DuePaymentCreateRequest,
    DuePaymentResponse,
    DuePaymentCreateResponse,
    SaleDueSnapshot,
    DueSaleSummary,
    CustomerDueSummaryResponse,
)

router = APIRouter(prefix="/due-payments", tags=["Due Payments"])
dues_router = APIRouter(prefix="/dues", tags=["Due Reports"])


def _assert_branch_access(member: StoreMember, branch_id: Optional[int]) -> None:
    """
    Branch-bound staff (member.branch_id is not None) can only touch
    records belonging to their own branch. Store-wide roles
    (member.branch_id is None -> owner/manager) can touch any branch.
    """
    if member.branch_id is not None and branch_id != member.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this branch's data",
        )


# ---------------------------------------------------------------------------
# CREATE — record a due payment against a sale
# ---------------------------------------------------------------------------
@router.post("", response_model=DuePaymentCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_due_payment(
    payload: DuePaymentCreateRequest,
    member: StoreMember = Depends(get_current_store_member),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Row lock — same sale-এ একসাথে দুইটা payment এলে race condition ঠেকাতে
    result = await db.execute(
        select(Sale)
        .where(Sale.id == payload.sale_id, Sale.store_id == member.store_id)
        .with_for_update()
    )
    sale = result.scalar_one_or_none()

    if sale is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")

    # Branch check — cashier can only collect due for sales made in their own branch
    _assert_branch_access(member, sale.branch_id)

    if sale.due_amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This sale has no outstanding due",
        )

    if payload.amount > sale.due_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount exceeds due ({sale.due_amount})",
        )

    due_payment = DuePayment(
        store_id=member.store_id,
        sale_id=sale.id,
        customer_id=sale.customer_id,
        amount=payload.amount,
        method=payload.method,
        received_by=current_user.id,
        note=payload.note,
    )
    db.add(due_payment)

    sale.paid_amount = sale.paid_amount + payload.amount
    sale.due_amount = sale.due_amount - payload.amount

    await db.flush()
    await db.refresh(due_payment)
    await db.commit()

    return DuePaymentCreateResponse(
        payment=DuePaymentResponse.model_validate(due_payment),
        sale=SaleDueSnapshot(
            sale_id=sale.id,
            invoice_no=sale.invoice_no,
            total=sale.total,
            paid_amount=sale.paid_amount,
            due_amount=sale.due_amount,
        ),
    )


# ---------------------------------------------------------------------------
# LIST — due payment history (filter by sale / customer / date)
# ---------------------------------------------------------------------------
@router.get("", response_model=list[DuePaymentResponse])
async def list_due_payments(
    sale_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    # DuePayment itself doesn't carry branch_id, so join to Sale to scope it
    query = (
        select(DuePayment)
        .join(Sale, DuePayment.sale_id == Sale.id)
        .where(DuePayment.store_id == member.store_id)
    )

    if member.branch_id is not None:
        query = query.where(Sale.branch_id == member.branch_id)

    if sale_id is not None:
        query = query.where(DuePayment.sale_id == sale_id)
    if customer_id is not None:
        query = query.where(DuePayment.customer_id == customer_id)

    query = query.order_by(DuePayment.created_at.desc())

    result = await db.execute(query)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# GET single
# ---------------------------------------------------------------------------
@router.get("/{due_payment_id}", response_model=DuePaymentResponse)
async def get_due_payment(
    due_payment_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(DuePayment)
        .join(Sale, DuePayment.sale_id == Sale.id)
        .where(
            DuePayment.id == due_payment_id,
            DuePayment.store_id == member.store_id,
        )
    )
    if member.branch_id is not None:
        query = query.where(Sale.branch_id == member.branch_id)

    result = await db.execute(query)
    due_payment = result.scalar_one_or_none()
    if due_payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Due payment not found")
    return due_payment


# ---------------------------------------------------------------------------
# REPORTS — outstanding sales list + per-customer summary
# ---------------------------------------------------------------------------
@dues_router.get("/sales", response_model=list[DueSaleSummary])
async def list_outstanding_sales(
    branch_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Sale)
        .where(Sale.store_id == member.store_id, Sale.due_amount > 0)
        .order_by(Sale.created_at.desc())
    )

    if member.branch_id is not None:
        # Branch-bound staff: ignore/override whatever branch_id they passed,
        # always force to their own branch — never trust the query param.
        _assert_branch_access(member, branch_id if branch_id is not None else member.branch_id)
        query = query.where(Sale.branch_id == member.branch_id)
    elif branch_id is not None:
        # Store-wide roles (owner/manager) may optionally filter to one branch.
        query = query.where(Sale.branch_id == branch_id)

    if customer_id is not None:
        query = query.where(Sale.customer_id == customer_id)

    result = await db.execute(query)
    return result.scalars().all()


@dues_router.get("/customers/{customer_id}", response_model=CustomerDueSummaryResponse)
async def get_customer_due_summary(
    customer_id: int,
    branch_id: Optional[int] = None,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    customer_result = await db.execute(
        select(Customer).where(
            Customer.id == customer_id, Customer.store_id == member.store_id
        )
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    sales_query = select(Sale).where(
        Sale.store_id == member.store_id,
        Sale.customer_id == customer_id,
        Sale.due_amount > 0,
    )

    if member.branch_id is not None:
        _assert_branch_access(member, branch_id if branch_id is not None else member.branch_id)
        sales_query = sales_query.where(Sale.branch_id == member.branch_id)
    elif branch_id is not None:
        sales_query = sales_query.where(Sale.branch_id == branch_id)

    sales_query = sales_query.order_by(Sale.created_at.desc())

    sales_result = await db.execute(sales_query)
    sales = sales_result.scalars().all()

    total_due: Decimal = sum((s.due_amount for s in sales), Decimal("0"))

    return CustomerDueSummaryResponse(
        customer_id=customer.id,
        customer_name=customer.name,
        customer_phone=customer.phone,
        total_due=total_due,
        sales=[DueSaleSummary.model_validate(s) for s in sales],
    )