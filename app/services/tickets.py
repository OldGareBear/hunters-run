"""Escalation from the chatbot to the management company."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.email.outbox import enqueue
from app.models import TICKET_STATUSES, Conversation, Ticket, TicketMessage, User
from app.permissions import Perm, Role, has_perm


class TicketError(Exception):
    pass


def management_recipients(db: Session) -> list[str]:
    users = db.scalars(
        select(User).where(User.role == Role.MANAGEMENT.value, User.is_active.is_(True))
    )
    emails = {u.email for u in users}
    emails.update(get_settings().management_notify_list)
    return sorted(emails)


def _ticket_link(ticket: Ticket) -> str:
    return f"{get_settings().base_url.rstrip('/')}/tickets/{ticket.id}"


def conversation_questions(conversation: Conversation) -> list[str]:
    """The asker's own questions, in order: what an escalation copies into the ticket.

    The assistant's answers stay out. They were written from documents the asker can
    read, which the people handling the ticket may not be allowed to see.
    """
    return [m.content for m in conversation.messages if m.role == "user" and m.content.strip()]


def questions_block(questions: list[str]) -> str:
    lines = ["Questions I asked the assistant:"]
    lines += ["- " + " ".join(q.split()) for q in questions]
    return "\n".join(lines)


def escalate(
    db: Session,
    *,
    user: User,
    subject: str,
    details: str,
    conversation: Conversation | None,
) -> Ticket:
    subject = " ".join(subject.split())[:200]
    details = details.strip()[:10000]
    if not subject:
        raise TicketError("Add a short subject so management knows what this is about.")
    if conversation is not None and conversation.user_id != user.id:
        raise TicketError("You can only escalate your own conversations.")
    ticket = Ticket(
        subject=subject,
        conversation_id=conversation.id if conversation else None,
        created_by_id=user.id,
    )
    db.add(ticket)
    db.flush()
    # A copy of the questions, taken now: the conversation can go on or be deleted later,
    # and the answers in it may quote documents management can't see.
    questions = conversation_questions(conversation) if conversation else []
    if questions:
        body = questions_block(questions)
        db.add(TicketMessage(ticket_id=ticket.id, author_id=user.id, body=body))
    if details:
        db.add(TicketMessage(ticket_id=ticket.id, author_id=user.id, body=details))
    for email in management_recipients(db):
        enqueue(
            db,
            to=email,
            subject=f"[Ticket #{ticket.id}] {subject}",
            template="ticket_new",
            kind="ticket",
            ticket_id=ticket.id,
            subject_line=subject,
            requester=user.display_name,
            requester_email=user.email,
            details=details,
            link=_ticket_link(ticket),
            questions=questions,
        )
    return ticket


def add_reply(
    db: Session, ticket: Ticket, *, author: User, body: str, internal: bool = False
) -> TicketMessage:
    body = body.strip()[:10000]
    if not body:
        raise TicketError("Write a message first.")
    staff = has_perm(author, Perm.TICKETS_HANDLE)
    if internal and not staff:
        raise TicketError("Only management can add internal notes.")
    msg = TicketMessage(ticket_id=ticket.id, author_id=author.id, body=body, is_internal=internal)
    db.add(msg)
    ticket.updated_at = utcnow()
    if internal:
        return msg
    if staff and author.id != ticket.created_by_id:
        if ticket.status == "open":
            ticket.status = "in_progress"
        enqueue(
            db,
            to=ticket.created_by.email,
            subject=f"[Ticket #{ticket.id}] Reply from management: {ticket.subject}",
            template="ticket_reply",
            kind="ticket",
            ticket_id=ticket.id,
            subject_line=ticket.subject,
            author=author.display_name,
            body=body,
            link=_ticket_link(ticket),
        )
    else:
        if ticket.status in ("waiting_on_homeowner", "resolved"):
            ticket.status = "open"
        for email in management_recipients(db):
            enqueue(
                db,
                to=email,
                subject=f"[Ticket #{ticket.id}] New reply: {ticket.subject}",
                template="ticket_reply",
                kind="ticket",
                ticket_id=ticket.id,
                subject_line=ticket.subject,
                author=author.display_name,
                body=body,
                link=_ticket_link(ticket),
            )
    return msg


def set_status(ticket: Ticket, status: str, *, assignee: User | None = None) -> None:
    if status not in TICKET_STATUSES:
        raise TicketError("Unknown status.")
    ticket.status = status
    if assignee is not None:
        ticket.assigned_to_id = assignee.id
    ticket.updated_at = utcnow()


def visible_tickets(db: Session, user: User) -> list[Ticket]:
    q = select(Ticket).order_by(Ticket.updated_at.desc())
    staff = has_perm(user, Perm.TICKETS_HANDLE) or (
        Role(user.role) == Role.BOARD and get_settings().board_can_view_tickets
    )
    if not staff:
        q = q.where(Ticket.created_by_id == user.id)
    return list(db.scalars(q))
