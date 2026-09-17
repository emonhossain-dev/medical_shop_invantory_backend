from typing import Annotated, Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import verify_token
from app.models.all_models import StaffRole,StoreMember, User


security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency to get the current authenticated user."""
    token = credentials.credentials
    payload = verify_token(token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    result = await db.execute(
        select(User)
        .options(selectinload(User.store_memberships))
        .where(User.id == int(user_id))
    )
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def get_current_store_member(
    x_store_id: Annotated[
        Optional[int],
        Header(description="Which store this request is acting on"),
    ] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoreMember:
    """
    Resolves which store the current request is scoped to.
    Returns the StoreMember for (current_user + x_store_id).
    Does NOT check role — use require_store_admin / require_owner for that.
    """
    if x_store_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Store-Id header is required for this request",
        )

    result = await db.execute(
        select(StoreMember)
        .options(selectinload(StoreMember.store))
        .where(
            StoreMember.user_id == current_user.id,
            StoreMember.store_id == x_store_id,
            StoreMember.is_active == True,
        )
    )
    member = result.scalar_one_or_none()

    if member is None:
        # Super admin-কে allow করতে চাইলে এখানে আলাদা logic দিতে পারেন
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this store",
        )

    return member


async def require_store_admin(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    """
    Requires the user to be OWNER or MANAGER of the store
    (or platform super admin).
    """
    if current_user.is_super_admin:
        return member

    if member.role not in (StaffRole.owner, StaffRole.manager):  # ← casing দেখে নিন
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Store admin access required (owner/manager only)",
        )
    return member


async def require_owner(
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
) -> StoreMember:
    """Requires the user to be the OWNER of the store."""
    if current_user.is_super_admin:
        return member

    if member.role != StaffRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only store owner can perform this action",
        )
    return member


async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Platform-wide super admin only."""
    if not current_user.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )
    return current_user