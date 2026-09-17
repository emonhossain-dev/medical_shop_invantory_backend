from fastapi import APIRouter
from app.api.v1.routes.store import support_tickets, payments, announcements, branch, staff, store
router = APIRouter()
router.include_router(support_tickets.router)
router.include_router(payments.router)
router.include_router(announcements.router)
router.include_router(branch.router)
router.include_router(staff.router)
router.include_router(store.router)