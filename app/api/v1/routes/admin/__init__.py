# app/api/admin/__init__.py
from fastapi import APIRouter
from app.api.v1.routes.admin import plans, subscriptions, payments, support_tickets, announcements

router = APIRouter()
router.include_router(plans.router)
router.include_router(subscriptions.router)
router.include_router(payments.router)
router.include_router(support_tickets.router)
router.include_router(announcements.router)