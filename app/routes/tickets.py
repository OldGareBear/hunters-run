from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import TICKET_STATUSES, Ticket, User
from app.permissions import Perm, Role, can_view_ticket, has_perm
from app.security.auth import require_perm, require_user
from app.services import tickets as ticket_service
from app.web import kick_outbox, redirect, render

router = APIRouter(prefix="/tickets")


def _get(db: Session, user: User, ticket_id: int) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None or not can_view_ticket(user, ticket):
        raise HTTPException(404)
    return ticket


@router.get("")
def index(
    request: Request,
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    tickets = ticket_service.visible_tickets(db, user)
    if status:
        tickets = [t for t in tickets if t.status == status]
    return render(
        request, "tickets/list.html", tickets=tickets, status=status, statuses=TICKET_STATUSES
    )


@router.get("/new")
def new(request: Request, user: User = Depends(require_perm(Perm.TICKETS_CREATE))):
    return render(request, "tickets/new.html", conv=None)


@router.post("")
def create(
    request: Request,
    background: BackgroundTasks,
    subject: str = Form(...),
    details: str = Form(""),
    user: User = Depends(require_perm(Perm.TICKETS_CREATE)),
    db: Session = Depends(get_db),
):
    try:
        ticket = ticket_service.escalate(
            db, user=user, subject=subject, details=details, conversation=None
        )
    except ticket_service.TicketError as exc:
        return render(
            request,
            "tickets/new.html",
            400,
            conv=None,
            subject=subject,
            details=details,
            error=str(exc),
        )
    audit(
        db, "ticket.created", actor=user, target_type="ticket", target_id=ticket.id, request=request
    )
    db.commit()
    kick_outbox(background)
    return redirect(f"/tickets/{ticket.id}", "Sent to management.")


@router.get("/{ticket_id}")
def show(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    ticket = _get(db, user, ticket_id)
    staff = has_perm(user, Perm.TICKETS_HANDLE)
    messages = [m for m in ticket.messages if staff or not m.is_internal]
    handlers = []
    if staff:
        handlers = list(
            db.scalars(
                select(User).where(
                    User.role.in_([Role.MANAGEMENT.value, Role.ADMIN.value]),
                    User.is_active.is_(True),
                )
            )
        )
    can_reply = staff or ticket.created_by_id == user.id
    return render(
        request,
        "tickets/show.html",
        ticket=ticket,
        messages=messages,
        staff=staff,
        handlers=handlers,
        statuses=TICKET_STATUSES,
        can_reply=can_reply,
    )


@router.post("/{ticket_id}/reply")
def reply(
    request: Request,
    background: BackgroundTasks,
    ticket_id: int,
    body: str = Form(...),
    internal: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    ticket = _get(db, user, ticket_id)
    if not (has_perm(user, Perm.TICKETS_HANDLE) or ticket.created_by_id == user.id):
        raise HTTPException(403)
    try:
        ticket_service.add_reply(db, ticket, author=user, body=body, internal=internal == "1")
    except ticket_service.TicketError as exc:
        return redirect(f"/tickets/{ticket.id}", str(exc), "error")
    audit(
        db,
        "ticket.note_added" if internal == "1" else "ticket.replied",
        actor=user,
        target_type="ticket",
        target_id=ticket.id,
        request=request,
    )
    db.commit()
    kick_outbox(background)
    return redirect(f"/tickets/{ticket.id}#latest", "Saved.")


@router.post("/{ticket_id}/status")
def update_status(
    request: Request,
    ticket_id: int,
    status: str = Form(...),
    assignee_id: str = Form(""),
    user: User = Depends(require_perm(Perm.TICKETS_HANDLE)),
    db: Session = Depends(get_db),
):
    ticket = _get(db, user, ticket_id)
    assignee = None
    if assignee_id:
        assignee = db.get(User, int(assignee_id))
        if assignee is None or not has_perm(assignee, Perm.TICKETS_HANDLE):
            raise HTTPException(400)
    before = ticket.status
    try:
        ticket_service.set_status(ticket, status, assignee=assignee)
    except ticket_service.TicketError as exc:
        return redirect(f"/tickets/{ticket.id}", str(exc), "error")
    audit(
        db,
        "ticket.status_changed",
        actor=user,
        target_type="ticket",
        target_id=ticket.id,
        request=request,
        before=before,
        after=status,
        assignee=assignee.id if assignee else None,
    )
    db.commit()
    return redirect(f"/tickets/{ticket.id}", "Ticket updated.")
