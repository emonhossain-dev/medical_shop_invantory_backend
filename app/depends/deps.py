from typing import Annotated, Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import verify_token
from app.models.all_models import StoreMember, User, Role, RolePermission, Permission


security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(
        select(User).options(selectinload(User.store_memberships)).where(User.id == int(user_id))
    )
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_current_store_member(
    x_store_id: Annotated[Optional[int], Header(description="Which store this request is acting on")] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoreMember:
    if x_store_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Store-Id header is required")

    result = await db.execute(
        select(StoreMember)
        .options(
            selectinload(StoreMember.store),
            # role -> permission_links -> permission, সব eager-load — permission
            # check করার সময় আর কোনো extra DB round-trip লাগবে না
            selectinload(StoreMember.role)
            .selectinload(Role.permission_links)
            .selectinload(RolePermission.permission),
        )
        .where(
            StoreMember.user_id == current_user.id,
            StoreMember.store_id == x_store_id,
            StoreMember.is_active == True,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not an active member of this store")
    return member


def get_effective_permissions(member: StoreMember) -> set[str]:
    """role এর permission set + per-member override মিলিয়ে চূড়ান্ত permission set।"""
    if member.role.is_system and member.role.slug == "owner":
        return {"*"}

    codes = {link.permission.code for link in member.role.permission_links}

    override = member.permissions or {}
    codes |= set(override.get("extra", []))
    codes -= set(override.get("revoke", []))
    return codes


def has_permission(member: StoreMember, module: str, action: str) -> bool:
    perms = get_effective_permissions(member)
    return "*" in perms or f"{module}:{action}" in perms


def require_permission(module: str, action: str):
    """
    Usage:
        @router.post("/medicines")
        async def create_medicine(
            member: StoreMember = Depends(require_permission("medicines", "create")),
        ): ...

    যেই permission member কে দেওয়া হয়নি, সেই কাজ সে করতে পারবে না —
    role-এ না থাকলে এবং member.permissions["extra"] তেও না থাকলে 403।
    """
    async def _checker(
        current_user: User = Depends(get_current_user),
        member: StoreMember = Depends(get_current_store_member),
    ) -> StoreMember:
        if current_user.is_super_admin:
            return member
        if not has_permission(member, module, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {module}:{action}",
            )
        return member

    return _checker


# ---------------------------------------------------------------------------
# Backward-compatible role-level shortcuts (routers এ আগে থেকেই ব্যবহার হচ্ছে
# require_store_admin / require_owner — সেগুলো এখন role.slug দিয়ে কাজ করে)
# ---------------------------------------------------------------------------

async def require_store_admin(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    if current_user.is_super_admin:
        return member
    if member.role.slug not in ("owner", "manager"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Store admin access required (owner/manager only)")
    return member


async def require_owner(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    if current_user.is_super_admin:
        return member
    if not (member.role.is_system and member.role.slug == "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only store owner can perform this action")
    return member


async def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_super_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin access required")
    return current_user