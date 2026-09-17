# app/api/store/announcements.py
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import get_current_store_member
from app.models.all_models import (
    Announcement, AnnouncementRead, Subscription, StoreMember,
    AnnouncementTarget, SubscriptionStatus,
)
from app.schemas.store import AnnouncementOut

router = APIRouter(prefix="/store/announcements", tags=["Store - Announcements"])


@router.get("", response_model=List[AnnouncementOut])
async def list_store_announcements(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    sub_result = await db.execute(
        select(Subscription)
        .where(Subscription.store_id == member.store_id)
        .order_by(Subscription.created_at.desc())
    )
    subscription = sub_result.scalars().first()

    conditions = [Announcement.target == AnnouncementTarget.all]
    if subscription:
        if subscription.status == SubscriptionStatus.trialing:
            conditions.append(Announcement.target == AnnouncementTarget.trial)
        if subscription.status == SubscriptionStatus.active:
            conditions.append(Announcement.target == AnnouncementTarget.active)
        conditions.append(
            (Announcement.target == AnnouncementTarget.plan_specific)
            & (Announcement.target_plan_id == subscription.plan_id)
        )

    result = await db.execute(
        select(Announcement)
        .where(Announcement.published_at.is_not(None), or_(*conditions))
        .order_by(Announcement.published_at.desc())
    )
    announcements = result.scalars().all()

    read_result = await db.execute(
        select(AnnouncementRead.announcement_id).where(AnnouncementRead.store_member_id == member.id)
    )
    read_ids = {row[0] for row in read_result.all()}

    out = []
    for a in announcements:
        item = AnnouncementOut.model_validate(a)
        item.is_read = a.id in read_ids
        out.append(item)
    return out


@router.post("/{announcement_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_announcement_read(
    announcement_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    announcement = await db.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")

    existing = await db.execute(
        select(AnnouncementRead).where(
            AnnouncementRead.announcement_id == announcement_id,
            AnnouncementRead.store_member_id == member.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(AnnouncementRead(announcement_id=announcement_id, store_member_id=member.id))
        await db.commit()