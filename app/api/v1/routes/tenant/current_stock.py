# app/api/stock.py
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member
from app.models.all_models import Branch, StoreMember
from app.schemas.current_stock import (
    StockDetailOut,
    StockListOut,
    StockStatus,
    StockSummaryOut,
)
from app.services.stock_service import EXPIRY_ALERT_DAYS, StockService

router = APIRouter(prefix="/stock", tags=["Stock"])


async def get_stock_service(
    branch_id: Optional[int] = Query(
        default=None,
        description="Filter by branch. Ignored/forbidden mismatch for branch-bound staff.",
    ),
    expiry_alert_days: int = Query(EXPIRY_ALERT_DAYS, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    member: StoreMember = Depends(get_current_store_member),
) -> StockService:
    """
    Resolves the branch scope:
      - branch-bound staff  -> always their own branch (other branch_id => 403)
      - owner / manager     -> all branches, or a specific ?branch_id= of this store
    """
    if member.branch_id is not None:
        if branch_id is not None and branch_id != member.branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view stock of your own branch",
            )
        scope_branch_id: Optional[int] = member.branch_id
    else:
        scope_branch_id = branch_id
        if scope_branch_id is not None:
            exists = await db.execute(
                select(Branch.id).where(
                    Branch.id == scope_branch_id, Branch.store_id == member.store_id
                )
            )
            if exists.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
                )

    return StockService(
        db=db,
        store_id=member.store_id,
        branch_id=scope_branch_id,
        expiry_alert_days=expiry_alert_days,
    )


@router.get("", response_model=StockListOut, summary="Current stock (per medicine)")
async def list_current_stock(
    search: Optional[str] = Query(None, min_length=1, max_length=100, description="Name / generic name"),
    category: Optional[str] = Query(None, max_length=100),
    stock_status: Optional[StockStatus] = Query(None, alias="status"),
    include_inactive: bool = Query(False),
    sort_by: Literal["name", "quantity", "stock_value", "expiry"] = "name",
    sort_order: Literal["asc", "desc"] = "asc",
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: StockService = Depends(get_stock_service),
):
    return await service.list_stock(
        search=search,
        category=category,
        status=stock_status,
        include_inactive=include_inactive,
        sort_by=sort_by,
        sort_order=sort_order,
        skip=skip,
        limit=limit,
    )


@router.get("/summary", response_model=StockSummaryOut, summary="Stock dashboard totals")
async def stock_summary(service: StockService = Depends(get_stock_service)):
    return await service.summary()


@router.get(
    "/{medicine_id}",
    response_model=StockDetailOut,
    summary="Batch-wise stock of one medicine",
)
async def medicine_stock_detail(
    medicine_id: int,
    service: StockService = Depends(get_stock_service),
):
    detail = await service.get_detail(medicine_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    return detail