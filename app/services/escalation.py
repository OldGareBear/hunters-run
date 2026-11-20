"""Asking management: one email to the management company, then email takes over.

The portal keeps no copy of what was asked. The email goes from the portal's address
to management with the homeowner copied and set as Reply-To, so management answers by
replying and the conversation carries on in their inboxes. The audit log records only
that a message was sent, which is also what the daily limit counts.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.email.outbox import enqueue
from app.models import AuditLog, Conversation, User
from app.permissions import Role

AUDIT_ACTION = "escalation.sent"


class EscalationError(Exception):
    pass


def management_recipients(db: Session) -> list[str]:
    users = db.scalars(
        select(User).where(User.role == Role.MANAGEMENT.value, User.is_active.is_(True))
    )
    emails = {u.email for u in users}
    emails.update(get_settings().management_notify_list)
    return sorted(emails)


def conversation_questions(conversation: Conversation) -> list[str]:
    """The asker's own questions, in order: what an escalation copies into the email.

    The assistant's answers stay out. They were written from documents the asker can
    read, which the people at the management company may not be allowed to see.
    """
    return [m.content for m in conversation.messages if m.role == "user" and m.content.strip()]


def sent_today(db: Session, user: User) -> int:
    since = utcnow() - timedelta(days=1)
    return db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor_id == user.id,
            AuditLog.action == AUDIT_ACTION,
            AuditLog.created_at >= since,
        )
    )


def escalate(
    db: Session,
    *,
    user: User,
    subject: str,
    details: str,
    conversation: Conversation | None,
) -> None:
    """Queue the email. The caller records the audit entry and commits."""
    subject = " ".join(subject.split())[:200]
    details = details.strip()[:10000]
    if not subject:
        raise EscalationError("Add a short subject so management knows what this is about.")
    if conversation is not None and conversation.user_id != user.id:
        raise EscalationError("You can only send your own conversations.")
    limit = get_settings().escalations_per_day
    if sent_today(db, user) >= limit:
        raise EscalationError(
            f"You've sent {limit} messages to management in the last day, which is the most "
            "the portal sends for one person. Try again tomorrow, or contact the management "
            "office directly."
        )
    recipients = management_recipients(db)
    if not recipients:
        raise EscalationError(
            "The portal doesn't have an address for management yet. Contact the management "
            "office directly."
        )
    # A copy of the questions, taken now: the answers in the conversation may quote
    # documents management can't see.
    questions = conversation_questions(conversation) if conversation else []
    enqueue(
        db,
        to=", ".join(recipients),
        cc=user.email,
        reply_to=user.email,
        subject=f"{subject} (from {user.display_name})",
        template="escalation",
        kind="escalation",
        subject_line=subject,
        requester=user.display_name,
        requester_email=user.email,
        unit=user.unit or "",
        details=details,
        questions=questions,
    )
