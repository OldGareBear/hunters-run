"""Transactional outbox: enqueue emails in the same DB transaction as the change
that caused them, then deliver with retries (inline after commit, or via the worker).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.email.providers import EmailSendError, OutgoingEmail, get_provider
from app.models import EmailMessage

log = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates" / "emails"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template: str, **ctx) -> tuple[str, str]:
    """Render `<template>.txt` and `<template>.html`; returns (text, html)."""
    s = get_settings()
    ctx = {"hoa_name": s.hoa_name, "base_url": s.base_url.rstrip("/"), **ctx}
    text = _env.get_template(f"{template}.txt").render(**ctx)
    html = _env.get_template(f"{template}.html").render(**ctx)
    return text, html


def enqueue(
    db: Session,
    *,
    to: str,
    subject: str,
    template: str,
    kind: str,
    blast_id: int | None = None,
    **ctx,
) -> EmailMessage:
    text, html = render(template, **ctx)
    msg = EmailMessage(
        to_email=to,
        subject=subject,
        text_body=text,
        html_body=html,
        kind=kind,
        blast_id=blast_id,
    )
    db.add(msg)
    return msg


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** (attempts - 1), 60))


LEASE = timedelta(minutes=10)


def _claim(db: Session, limit: int) -> list[EmailMessage]:
    """Claim due messages with a lease, so a crashed worker's batch is retried later
    and concurrent workers never send the same message (SKIP LOCKED on Postgres)."""
    now = utcnow()
    q = (
        select(EmailMessage)
        .where(
            EmailMessage.status.in_(("pending", "sending")),
            EmailMessage.next_attempt_at <= now,
        )
        .order_by(EmailMessage.id)
        .limit(limit)
    )
    if db.get_bind().dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    batch = list(db.scalars(q))
    for m in batch:
        m.status = "sending"
        m.next_attempt_at = now + LEASE
    db.commit()
    return batch


def deliver_pending(db: Session, *, limit: int = 200) -> int:
    """Send due messages; returns how many were sent."""
    s = get_settings()
    provider = get_provider()
    sent = 0
    for m in _claim(db, limit):
        m.attempts += 1
        headers = {}
        if m.kind == "blast":
            headers["List-Unsubscribe"] = f"<{s.base_url.rstrip('/')}/account>"
        try:
            m.provider_message_id = provider.send(
                OutgoingEmail(
                    to=m.to_email,
                    subject=m.subject,
                    text=m.text_body,
                    html=m.html_body,
                    kind="broadcast" if m.kind == "blast" else "transactional",
                    headers=headers,
                )
            )
            m.status = "sent"
            m.sent_at = utcnow()
            m.last_error = ""
            sent += 1
        except EmailSendError as exc:
            m.last_error = str(exc)[:1000]
            if exc.permanent or m.attempts >= s.email_max_attempts:
                m.status = "failed"
            else:
                m.status = "pending"
                m.next_attempt_at = utcnow() + _backoff(m.attempts)
            log.warning("email %s failed: %s", m.id, exc)
        except Exception as exc:  # unexpected provider bug: retry later, don't crash the loop
            m.last_error = f"unexpected: {exc}"[:1000]
            m.status = "pending" if m.attempts < s.email_max_attempts else "failed"
            m.next_attempt_at = utcnow() + _backoff(m.attempts)
            log.exception("email %s failed unexpectedly", m.id)
        db.commit()
    return sent
