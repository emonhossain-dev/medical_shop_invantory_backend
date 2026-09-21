# app/api/v1/stock_receipts.py
"""
Stock Receiving (GRN) endpoints.

Register in your main router:
    from app.api.v1 import stock_receipts
    api_router.include_router(stock_receipts.router)

All endpoints need:  Authorization: Bearer <token>  +  X-Store-Id: <store id>
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user
from app.models.all_models import StaffRole, StoreMember, User
from app.schemas.stock_receipt import (
    MedicineSearchResult,
    PaginatedStockReceipts,
    RequisitionOutstandingResponse,
    StockReceiptCreateRequest,
    StockReceiptResponse,
)
from app.services.stock_receipt_service import StockReceiptService

router = APIRouter(prefix="/stock-receipts", tags=["Stock Receiving"])

# Who may receive stock. Cashiers / plain staff can only request (requisition), not receive.
RECEIVING_ROLES = (StaffRole.owner, StaffRole.manager, StaffRole.pharmacist)


async def require_receiving_access(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    if current_user.is_super_admin:
        return member
    if member.role not in RECEIVING_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner, manager or pharmacist can receive stock",
        )
    return member


def get_service(db: AsyncSession = Depends(get_db)) -> StockReceiptService:
    return StockReceiptService(db)


# ---------------------------------------------------------------------------
# POST /stock-receipts  -> receive stock
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=StockReceiptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Receive stock (GRN)",
    description=(
        "Receives a delivery into a branch. Creates the receipt, the purchase invoice, "
        "and the stock batches in a single transaction.\n\n"
        "* **Against a requisition**: send `requisition_id` + `requisition_item_id` per line. "
        "Partial deliveries are allowed; the requisition auto-completes when everything is received "
        "(or set `close_requisition=true` to close it short).\n"
        "* **Direct**: send `branch_id` + `medicine_id` per line.\n\n"
        "Send an `Idempotency-Key` header (e.g. a UUID) so a retried request never receives the stock twice."
    ),
)
async def receive_stock(
    payload: StockReceiptCreateRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, max_length=100),
    member: StoreMember = Depends(require_receiving_access),
    current_user: User = Depends(get_current_user),
    service: StockReceiptService = Depends(get_service),
):
    return await service.receive(
        payload,
        member=member,
        user=current_user,
        idempotency_key=idempotency_key,
        ip_address=request.client.host if request.client else None,
    )


# ---------------------------------------------------------------------------
# GET /stock-receipts  -> history
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedStockReceipts, summary="List stock receipts")
async def list_stock_receipts(
    branch_id: Optional[int] = Query(default=None, description="Ignored for branch-bound staff"),
    supplier_id: Optional[int] = None,
    requisition_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = Query(default=None, max_length=100, description="Search receipt no / supplier challan no"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    member: StoreMember = Depends(require_receiving_access),
    service: StockReceiptService = Depends(get_service),
):
    return await service.list_receipts(
        member,
        branch_id=branch_id,
        supplier_id=supplier_id,
        requisition_id=requisition_id,
        date_from=date_from,
        date_to=date_to,
        q=q,
        skip=skip,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# GET /stock-receipts/medicines/search  -> search store + master medicines together
# ---------------------------------------------------------------------------

@router.get(
    "/medicines/search",
    response_model=list[MedicineSearchResult],
    summary="Search medicines (store + master)",
    description=(
        "Searches the store's own medicines and the master medicine list in one call.\n\n"
        "* `source=store`  -> send its `medicine_id` in the receive request.\n"
        "* `source=master` -> send its `master_medicine_id`; it is added to the store automatically on receive.\n\n"
        "Master medicines that already exist in the store are not repeated."
    ),
)
async def search_medicines(
    q: str = Query(min_length=2, max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
    member: StoreMember = Depends(require_receiving_access),
    service: StockReceiptService = Depends(get_service),
):
    return await service.search_medicines(member.store_id, q, limit)


# ---------------------------------------------------------------------------
# GET /stock-receipts/requisitions/{id}/outstanding  -> what is still expected
# (declared BEFORE "/{receipt_id}" so the path is never mistaken for an id)
# ---------------------------------------------------------------------------

@router.get(
    "/requisitions/{requisition_id}/outstanding",
    response_model=RequisitionOutstandingResponse,
    summary="Ordered / received / outstanding quantities for a requisition",
)
async def requisition_outstanding(
    requisition_id: int,
    member: StoreMember = Depends(require_receiving_access),
    service: StockReceiptService = Depends(get_service),
):
    return await service.outstanding(requisition_id, member)


# ---------------------------------------------------------------------------
# GET /stock-receipts/{id}
# ---------------------------------------------------------------------------

@router.get("/{receipt_id}", response_model=StockReceiptResponse, summary="Stock receipt details")
async def get_stock_receipt(
    receipt_id: int,
    member: StoreMember = Depends(require_receiving_access),
    service: StockReceiptService = Depends(get_service),
):
    return await service.get(receipt_id, member)