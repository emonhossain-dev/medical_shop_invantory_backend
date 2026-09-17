from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, require_store_admin
from app.models.all_models import Customer, StoreMember
from app.repositories.base import BaseRepository
from app.repositories.deps import get_repository
from app.schemas.customer import CustomerCreateRequest, CustomerUpdateRequest, CustomerResponse

router = APIRouter(prefix="/customers", tags=["Customer"])


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def create_customer(
    data: CustomerCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    customer = await repo.create(
        name=data.name,
        phone=data.phone,
        address=data.address,
    )
    await repo.db.commit()
    return CustomerResponse.model_validate(customer)


@router.get("", response_model=list[CustomerResponse])
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


@router.get("/{customer_id}", response_model=CustomerResponse)
async def get_customer(
    customer_id: int,
    member: StoreMember = Depends(get_current_store_member),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    customer = await repo.get_by_id_or_404(customer_id)
    return CustomerResponse.model_validate(customer)


@router.patch("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: int,
    data: CustomerUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
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


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(
    customer_id: int,
    member: StoreMember = Depends(require_store_admin),
    repo: BaseRepository[Customer] = Depends(get_repository(Customer)),
):
    deleted = await repo.delete(customer_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    await repo.db.commit()
    return None