# app/api/dashboard.py
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member
from app.models.all_models import Branch, StoreMember
from app.schemas.dashboard import DashboardResponse
from app.services.dashboard_service import DashboardService, today_bd

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

MAX_RANGE_DAYS = 366

# owner/manager হলে profit দেখানো হয় -- role.slug দিয়ে চেক করা হচ্ছে, কারণ
# StoreMember.role এখন সরাসরি StaffRole enum না, একটা Role ORM row (যার
# slug "owner" / "manager" ইত্যাদি হতে পারে)।
PROFIT_VISIBLE_ROLE_SLUGS = {"owner", "manager"}


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    date_from: Optional[date] = Query(None, description="Default: আজ (Asia/Dhaka)"),
    date_to: Optional[date] = Query(None, description="Default: আজ (Asia/Dhaka)"),
    branch_id: Optional[int] = Query(
        None,
        description="owner/manager এর জন্য optional filter -- একটা নির্দিষ্ট branch এর data দেখতে। branch-bound staff এর জন্য নিজের branch ছাড়া অন্য কিছু দিলে 403।",
    ),
    x_branch_id: Optional[int] = Header(
        None,
        alias="X-Branch-Id",
        description="branch_id এর বিকল্প -- header দিয়েও পাঠানো যায়। দুটোই দিলে ?branch_id= প্রাধান্য পাবে।",
    ),
    db: AsyncSession = Depends(get_db),
    member: StoreMember = Depends(get_current_store_member),
):
    """
    Store dashboard — daily sales, customer due, supplier due, stock alerts
    এবং pie chart data।

    - কোনো date না দিলে শুধু আজকের data আসবে।
    - branch-bound staff (member.branch_id সেট) সবসময় শুধু নিজের branch এর
      data দেখবে -- অন্য branch_id/X-Branch-Id দিলে 403।
    - owner/manager store-wide সব branch এর data দেখবে, বা ?branch_id=/
      X-Branch-Id দিয়ে একটা নির্দিষ্ট branch এ filter করতে পারবে।
    - estimated_gross_profit শুধু owner/manager এর জন্য।
    """
    d_from = date_from or date_to or today_bd()
    d_to = date_to or d_from

    if d_from > d_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be on or before date_to",
        )
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days",
        )

    requested_branch_id = branch_id if branch_id is not None else x_branch_id

    if member.branch_id is not None:
        # branch-bound staff -- override করতে দেওয়া হবে না
        if requested_branch_id is not None and requested_branch_id != member.branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view the dashboard of your own branch",
            )
        scope_branch_id: Optional[int] = member.branch_id
    else:
        scope_branch_id = requested_branch_id
        if scope_branch_id is not None:
            exists = await db.execute(
                select(Branch.id).where(
                    Branch.id == scope_branch_id, Branch.store_id == member.store_id
                )
            )
            if exists.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
                )

    service = DashboardService(
        db=db,
        store_id=member.store_id,
        branch_id=scope_branch_id,
        can_see_profit=member.role.slug in PROFIT_VISIBLE_ROLE_SLUGS,
    )
    return await service.build(d_from, d_to)