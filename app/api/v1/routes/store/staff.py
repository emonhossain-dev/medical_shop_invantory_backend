from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import hash_password          # ← আপনার existing function
from app.models.all_models import User, StoreMember, Branch, Subscription
from app.models.all_models import StaffRole
from app.schemas.store import StaffCreateRequest, StaffUpdateRequest, StaffResponse
from app.depends.deps import get_current_store_member, require_store_admin, require_owner

router = APIRouter(prefix="/staff", tags=["Staff"])


@router.post("", response_model=StaffResponse, status_code=201)
async def add_staff(
    data: StaffCreateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    store_id = member.store_id

    # Plan limit check (max_staff)
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.store_id == store_id,
            Subscription.status.in_(["trialing", "active"]),
        )
    )
    subscription = result.scalar_one_or_none()

    if subscription and subscription.plan.max_staff is not None:
        count_result = await db.execute(
            select(func.count()).select_from(StoreMember).where(
                StoreMember.store_id == store_id,
                StoreMember.is_active == True,
            )
        )
        current_count = count_result.scalar()
        if current_count >= subscription.plan.max_staff:
            raise HTTPException(
                status_code=400,
                detail=f"Staff limit reached. Your plan allows max {subscription.plan.max_staff} staff.",
            )

    # User আছে কিনা চেক
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user:
        # নতুন user তৈরি (temporary password)
        temp_password = "ChangeMe123!"  # পরে invite system বানাতে পারেন
        user = User(
            email=data.email,
            full_name=data.full_name,
            phone=data.phone,
            password_hash=hash_password(temp_password),  # ← আপনার function
            is_active=True,
        )
        db.add(user)
        await db.flush()

    # Already member কিনা
    existing = await db.execute(
        select(StoreMember).where(
            StoreMember.store_id == store_id,
            StoreMember.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="User is already a staff of this store")

    # branch_id valid কিনা
    if data.branch_id:
        branch_check = await db.execute(
            select(Branch).where(
                Branch.id == data.branch_id,
                Branch.store_id == store_id,
            )
        )
        if not branch_check.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Invalid branch_id")

    staff = StoreMember(
        store_id=store_id,
        user_id=user.id,
        branch_id=data.branch_id,
        role=data.role,
        permissions=data.permissions,
        is_active=True,
    )
    db.add(staff)
    await db.commit()
    await db.refresh(staff)

    return StaffResponse(
        id=staff.id,
        store_id=staff.store_id,
        user_id=staff.user_id,
        branch_id=staff.branch_id,
        role=staff.role,
        permissions=staff.permissions,
        is_active=staff.is_active,
        created_at=staff.created_at,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
    )


@router.get("", response_model=list[StaffResponse])
async def list_staff(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StoreMember)
        .options(selectinload(StoreMember.user))
        .where(StoreMember.store_id == member.store_id)
        .order_by(StoreMember.created_at)
    )
    staffs = result.scalars().all()

    response = []
    for s in staffs:
        response.append(
            StaffResponse(
                id=s.id,
                store_id=s.store_id,
                user_id=s.user_id,
                branch_id=s.branch_id,
                role=s.role,
                permissions=s.permissions,
                is_active=s.is_active,
                created_at=s.created_at,
                full_name=s.user.full_name if s.user else None,
                email=s.user.email if s.user else None,
                phone=s.user.phone if s.user else None,
            )
        )
    return response


@router.patch("/{staff_id}", response_model=StaffResponse)
async def update_staff(
    staff_id: int,
    data: StaffUpdateRequest,
    member: StoreMember = Depends(require_store_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StoreMember)
        .options(selectinload(StoreMember.user))
        .where(
            StoreMember.id == staff_id,
            StoreMember.store_id == member.store_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff not found")

    # Owner নিজেকে demote করতে পারবে না
    if staff.role == StaffRole.owner and data.role and data.role != StaffRole.owner:
        raise HTTPException(status_code=400, detail="Cannot change owner's role")

    if data.role is not None:
        staff.role = data.role
    if data.branch_id is not None:
        staff.branch_id = data.branch_id
    if data.permissions is not None:
        staff.permissions = data.permissions
    if data.is_active is not None:
        staff.is_active = data.is_active

    await db.commit()
    await db.refresh(staff)

    return StaffResponse(
        id=staff.id,
        store_id=staff.store_id,
        user_id=staff.user_id,
        branch_id=staff.branch_id,
        role=staff.role,
        permissions=staff.permissions,
        is_active=staff.is_active,
        created_at=staff.created_at,
        full_name=staff.user.full_name if staff.user else None,
        email=staff.user.email if staff.user else None,
        phone=staff.user.phone if staff.user else None,
    )


@router.delete("/{staff_id}", status_code=204)
async def remove_staff(
    staff_id: int,
    member: StoreMember = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StoreMember).where(
            StoreMember.id == staff_id,
            StoreMember.store_id == member.store_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff not found")

    if staff.role == StaffRole.owner:
        raise HTTPException(status_code=400, detail="Cannot remove store owner")

    await db.delete(staff)
    await db.commit()
    return None