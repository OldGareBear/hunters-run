"""Asking management: one email to the management company, then email takes over.

The portal keeps no copy of what was asked. The email goes from the portal's address
to management with the homeowner copied and set as Reply-To, so management answers by
replying and the conversation carries on in their inboxes. The audit log records only
that a message was sent, which is also what the daily limit counts.

When the question comes from a conversation with Hunter, the email ends with a "Hunter
Bot Context" section: facts from the documents that help management answer or decide
whether it needs the board, each with its source. The homeowner is copied, and replies
quote the email, so Hunter only uses documents both the homeowner and management can
see. The section is extra: if Hunter is off, over budget or fails, the email still goes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.email.outbox import enqueue
from app.models import AuditLog, Conversation, User
from app.permissions import Role
from app.rag import llm as llm_module
from app.rag.retrieval import retrieve
from app.services import chat_limits

log = logging.getLogger(__name__)

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

    Hunter's answers stay out. They were written from documents the asker can
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


# --- Hunter Bot Context ----------------------------------------------------------

CONTEXT_MAX_LINES = 8
CONTEXT_MAX_LINE_CHARS = 400
_LINK = re.compile(r"(https?://|www\.)\S+|\S+@\S+\.\S+|\+?\d[\d ().-]{7,}\d", re.I)
_MARK = re.compile(r"\[\[src:(\d+)\]\]")


@dataclass
class BotContext:
    """What goes in the Hunter Bot Context section. `note` explains an empty one."""

    lines: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # n, title, page, url
    note: str = ""


NOTHING_FOUND = (
    "Hunter found nothing that bears on this in the documents the homeowner and "
    "management can both see."
)
UNAVAILABLE = "Hunter couldn't add context this time."
OVER_BUDGET = "Hunter's budget for this month is used up, so no context was added."


def _request_text(subject: str, details: str, questions: list[str]) -> str:
    parts = [f"Subject: {subject}"]
    if details:
        parts.append(details)
    if questions:
        parts.append(
            "Questions they asked Hunter first:\n" + "\n".join(f"- {q}" for q in questions)
        )
    text = "\n\n".join(parts)[:6000]
    return re.sub(r"(</?\s*)request", r"\1re-quest", text, flags=re.I)


def _lines(text: str, sources) -> tuple[list[str], list[dict]]:
    """Keep the cited lines, plain text, numbered by first citation."""
    # A citation that lands at the start of a line belongs to the line before it.
    text = re.sub(r"\n(\s*(?:\[\[src:\d+\]\])+)", r"\1\n", text)
    # Numbered by first citation; passages from the same page share a number.
    numbering: dict[tuple, int] = {}
    first: dict[tuple, object] = {}
    lines: list[str] = []
    for raw in text.splitlines():
        marks = [int(i) for i in _MARK.findall(raw) if int(i) < len(sources)]
        if not marks or _LINK.search(raw):
            continue  # every fact rests on a document; no links or contact details
        words = " ".join(_MARK.sub("", raw).split()).lstrip("-*• ").strip()
        if not words:
            continue
        keys = [(sources[i].document_id, sources[i].page, i) for i in marks]
        ns = sorted({numbering.setdefault(k[:2], len(numbering) + 1) for k in keys})
        for k in keys:
            first.setdefault(k[:2], sources[k[2]])
        lines.append(f"{words[:CONTEXT_MAX_LINE_CHARS]} " + "".join(f"[{n}]" for n in ns))
        if len(lines) == CONTEXT_MAX_LINES:
            break
    base = get_settings().base_url.rstrip("/")
    used = [
        {
            "n": n,
            "title": first[key].title,
            "page": first[key].page,
            "url": f"{base}/documents/{first[key].document_id}",
        }
        for key, n in sorted(numbering.items(), key=lambda kv: kv[1])
    ]
    return lines, used


def bot_context(
    db: Session, user: User, *, subject: str, details: str, questions: list[str]
) -> BotContext:
    """Hunter's notes for management. Never raises: the email matters more than the notes."""
    try:
        if chat_limits.status(db, user).over_budget:
            return BotContext(note=OVER_BUDGET)
        request = _request_text(subject, details, questions)
        sources = retrieve(db, user, request, also_visible_to=Role.MANAGEMENT.value)
        if not sources:
            return BotContext(note=NOTHING_FOUND)
        try:
            result = llm_module.get_llm().context(request=request, sources=sources)
        except llm_module.AnswerTookTooLong as slow:
            chat_limits.record(
                db,
                None,
                model=slow.model,
                input_tokens=slow.input_tokens,
                output_tokens=slow.output_tokens,
            )
            return BotContext(note=UNAVAILABLE)
        # Counted in the monthly budget, but not as one of the homeowner's questions.
        chat_limits.record(
            db,
            None,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        if result.refused:
            return BotContext(note=UNAVAILABLE)
        if result.text.strip() == llm_module.NOTHING_RELEVANT:
            return BotContext(note=NOTHING_FOUND)
        lines, used = _lines(result.text, sources)
        if not lines:
            return BotContext(note=NOTHING_FOUND)
        return BotContext(lines=lines, sources=used)
    except Exception:
        log.exception("Hunter Bot Context failed")
        return BotContext(note=UNAVAILABLE)


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
    context = (
        bot_context(db, user, subject=subject, details=details, questions=questions)
        if conversation is not None
        else None
    )
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
        context=context,
    )
