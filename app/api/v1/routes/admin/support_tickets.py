# app/api/admin/support_tickets.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import require_super_admin
from app.models.all_models import SupportTicket, SupportTicketReply, User, TicketStatus
from app.schemas.admin import TicketUpdate, TicketOut, TicketReplyCreate

router = APIRouter(prefix="/admin/support-tickets", tags=["Admin - Support Tickets"])


@router.get("", response_model=List[TicketOut])
async def list_tickets(
    status_filter: Optional[TicketStatus] = Query(None, alias="status"),
    store_id: Optional[int] = Query(None),
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = select(SupportTicket).options(selectinload(SupportTicket.replies))
    if status_filter:
        query = query.where(SupportTicket.status == status_filter)
    if store_id:
        query = query.where(SupportTicket.store_id == store_id)
    query = query.order_by(SupportTicket.created_at.desc()).offset(skip).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.replies))
        .where(SupportTicket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.put("/{ticket_id}", response_model=TicketOut)
async def update_ticket_status(
    ticket_id: int,
    payload: TicketUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = payload.status
    await db.commit()
    await db.refresh(ticket, attribute_names=["replies"])
    return ticket


@router.post("/{ticket_id}/reply", response_model=TicketOut)
async def reply_to_ticket(
    ticket_id: int,
    payload: TicketReplyCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """Admin থেকে থ্রেডেড reply — status এখনো open থাকলে auto in_progress হয়ে যায়।"""
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.replies))
        .where(SupportTicket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    reply = SupportTicketReply(
        ticket_id=ticket.id, sender_id=admin.id, is_admin_reply=True, message=payload.message,
    )
    db.add(reply)

    if ticket.status == TicketStatus.open:
        ticket.status = TicketStatus.in_progress

    await db.commit()
    await db.refresh(ticket, attribute_names=["replies"])
    return ticket