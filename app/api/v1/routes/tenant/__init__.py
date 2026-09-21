from fastapi import APIRouter
from app.api.v1.routes.tenant import (
    customer,
    due_payment,
    purchase,
    Sale,
    medicine,
    stock_adjustment,
    stock_transfer,
    supplier,
    purchase_requisition,
    stock_receipts,
    stock_release,
    current_stock
)

router = APIRouter()
router.include_router(medicine.router)
router.include_router(supplier.router)
router.include_router(purchase.router)
router.include_router(purchase_requisition.router)
router.include_router(stock_receipts.router)
router.include_router(stock_release.router)
router.include_router(current_stock.router)

router.include_router(stock_transfer.router)
router.include_router(stock_adjustment.router)

router.include_router(customer.router)

router.include_router(Sale.router)
router.include_router(due_payment.router)
router.include_router(due_payment.dues_router)