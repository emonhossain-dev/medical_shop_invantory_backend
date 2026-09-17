# app/api/admin/announcements.py
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.depends.deps import require_super_admin
from app.models.all_models import Announcement, User
from app.schemas.admin import AnnouncementCreate, AnnouncementUpdate, AnnouncementOut

router = APIRouter(prefix="/admin/announcements", tags=["Admin - Announcements"])


@router.post("", response_model=AnnouncementOut, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    payload: AnnouncementCreate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    """draft তৈরি হয় (published_at=None) — /publish endpoint দিয়ে live করতে হবে"""
    announcement = Announcement(**payload.model_dump())
    db.add(announcement)
    await db.commit()
    await db.refresh(announcement)
    return announcement


@router.get("", response_model=List[AnnouncementOut])
async def list_announcements(
    published_only: bool = Query(False),
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = select(Announcement)
    if published_only:
        query = query.where(Announcement.published_at.is_not(None))
    query = query.order_by(Announcement.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{announcement_id}", response_model=AnnouncementOut)
async def get_announcement(
    announcement_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    announcement = await db.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return announcement


@router.put("/{announcement_id}", response_model=AnnouncementOut)
async def update_announcement(
    announcement_id: int,
    payload: AnnouncementUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    announcement = await db.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(announcement, key, value)

    await db.commit()
    await db.refresh(announcement)
    return announcement


@router.post("/{announcement_id}/publish", response_model=AnnouncementOut)
async def publish_announcement(
    announcement_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    announcement = await db.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")

    announcement.published_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(announcement)
    return announcement


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(
    announcement_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    announcement = await db.get(Announcement, announcement_id)
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")

    await db.delete(announcement)
    await db.commit()