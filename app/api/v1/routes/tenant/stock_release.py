# app/api/v1/routes/tenant/stock_release.py
"""
Stock Release (write-off / adjustment) endpoints.

Damage, expiry, supplier return, and count correction — posted as one voucher.
Stock is deducted/added in the same transaction.

Send an Idempotency-Key header so a retry never writes stock off twice.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user
from app.models.all_models import StaffRole, StockReleaseReason, StoreMember, User
from app.schemas.stock_release import (
    PaginatedStockReleases,
    ReleasableBatchResponse,
    StockReleaseCreateRequest,
    StockReleaseResponse,
)
from app.services.stock_release_service import StockReleaseService

router = APIRouter(prefix="/stock-releases", tags=["Stock Release"])

RELEASING_ROLES = (StaffRole.owner, StaffRole.manager, StaffRole.pharmacist)


async def require_release_access(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    if current_user.is_super_admin:
        return member
    if member.role not in RELEASING_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner, manager or pharmacist can release / write off stock",
        )
    return member


def get_service(db: AsyncSession = Depends(get_db)) -> StockReleaseService:
    return StockReleaseService(db)


@router.post(
    "",
    response_model=StockReleaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a stock release (write-off / adjustment)",
    description=(
        "Posts a write-off voucher and updates batch quantities in one transaction.\n\n"
        "* **damage** / **expiry** — `direction` must be `out`.\n"
        "* **supplier_return** — `direction=out`; optional `purchase_item_id` to cap qty against that purchase line.\n"
        "* **count_correction** — `out` for shortage, `in` for surplus.\n\n"
        "Quantity is always positive. Send `Idempotency-Key` (UUID) so retries are safe."
    ),
)
async def create_stock_release(
    payload: StockReleaseCreateRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, max_length=100),
    member: StoreMember = Depends(require_release_access),
    current_user: User = Depends(get_current_user),
    service: StockReleaseService = Depends(get_service),
):
    return await service.release(
        payload,
        member=member,
        user=current_user,
        idempotency_key=idempotency_key,
        ip_address=request.client.host if request.client else None,
    )


@router.get("", response_model=PaginatedStockReleases, summary="List stock releases")
async def list_stock_releases(
    branch_id: Optional[int] = Query(default=None, description="Ignored for branch-bound staff"),
    reason: Optional[StockReleaseReason] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = Query(default=None, max_length=100, description="Search release no / reference no"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    member: StoreMember = Depends(require_release_access),
    service: StockReleaseService = Depends(get_service),
):
    return await service.list_releases(
        member,
        branch_id=branch_id,
        reason=reason,
        date_from=date_from,
        date_to=date_to,
        q=q,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/batches",
    response_model=list[ReleasableBatchResponse],
    summary="Batches available to write off",
)
async def list_releasable_batches(
    branch_id: int = Query(gt=0),
    medicine_id: Optional[int] = Query(default=None, gt=0),
    q: Optional[str] = Query(default=None, max_length=100, description="Medicine name or batch no"),
    expired_only: bool = False,
    in_stock_only: bool = True,
    limit: int = Query(default=50, ge=1, le=100),
    member: StoreMember = Depends(require_release_access),
    service: StockReleaseService = Depends(get_service),
):
    return await service.list_batches(
        member,
        branch_id=branch_id,
        medicine_id=medicine_id,
        q=q,
        expired_only=expired_only,
        in_stock_only=in_stock_only,
        limit=limit,
    )


@router.get("/{release_id}", response_model=StockReleaseResponse, summary="Stock release details")
async def get_stock_release(
    release_id: int,
    member: StoreMember = Depends(require_release_access),
    service: StockReleaseService = Depends(get_service),
):
    return await service.get(release_id, member)
