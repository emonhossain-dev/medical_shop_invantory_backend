# app/api/store/support_tickets.py
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.depends.deps import get_current_store_member, get_current_user
from app.models.all_models import SupportTicket, SupportTicketReply, StoreMember, User, TicketStatus
from app.schemas.store import TicketCreate, TicketReplyCreate, TicketOut

router = APIRouter(prefix="/store/support-tickets", tags=["Store - Support Tickets"])


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    ticket = SupportTicket(
        store_id=member.store_id, created_by=current_user.id,
        subject=payload.subject, message=payload.message,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket, attribute_names=["replies"])
    return ticket


@router.get("", response_model=List[TicketOut])
async def list_own_tickets(
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.replies))
        .where(SupportTicket.store_id == member.store_id)
        .order_by(SupportTicket.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_own_ticket(
    ticket_id: int,
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.replies))
        .where(SupportTicket.id == ticket_id, SupportTicket.store_id == member.store_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.post("/{ticket_id}/reply", response_model=TicketOut)
async def reply_to_own_ticket(
    ticket_id: int,
    payload: TicketReplyCreate,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(get_current_store_member),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.replies))
        .where(SupportTicket.id == ticket_id, SupportTicket.store_id == member.store_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    reply = SupportTicketReply(
        ticket_id=ticket.id, sender_id=current_user.id, is_admin_reply=False, message=payload.message,
    )
    db.add(reply)

    if ticket.status in (TicketStatus.resolved, TicketStatus.closed):
        ticket.status = TicketStatus.open  # store থেকে reply এলে reopen হয়

    await db.commit()
    await db.refresh(ticket, attribute_names=["replies"])
    return ticket