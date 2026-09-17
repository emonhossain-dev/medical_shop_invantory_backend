from fastapi import APIRouter
from app.api.v1.routes.auth import Auth as auth_router

router = APIRouter()
router.include_router(auth_router.router, tags=["Auth"])