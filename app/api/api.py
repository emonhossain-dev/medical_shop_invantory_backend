from fastapi import APIRouter

from app.api.v1.routes.auth.Auth import router as auth_router
from app.api.v1.routes.store import router as store_group_router
from app.api.v1.routes.admin import router as admin_router
from app.api.v1.routes.tenant import router as tenant_router

router = APIRouter()

router.include_router(auth_router)
router.include_router(store_group_router)
router.include_router(tenant_router)

#router.include_router(admin_router)