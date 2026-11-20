"""Board email blasts: compose, preview the audience, queue one message per recipient."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.email.outbox import enqueue
from app.models import EmailBlast, User
from app.permissions import ALL_ROLES


class BlastError(Exception):
    pass


def clean_audience(roles: Iterable[str]) -> list[str]:
    allowed = {r.value for r in ALL_ROLES}
    picked = sorted({r for r in roles if r in allowed})
    if not picked:
        raise BlastError("Choose at least one group to send to.")
    return picked


def recipients(db: Session, roles: list[str], *, official: bool) -> list[User]:
    q = select(User).where(User.role.in_(roles), User.is_active.is_(True)).order_by(User.email)
    if not official:
        q = q.where(User.email_opt_out_announcements.is_(False))
    return list(db.scalars(q))


def create_draft(
    db: Session, *, author: User, subject: str, body: str, roles: Iterable[str], official: bool
) -> EmailBlast:
    subject, body = subject.strip(), body.strip()
    if not subject or len(subject) > 200:
        raise BlastError("Write a subject of up to 200 characters.")
    if not body or len(body) > 20000:
        raise BlastError("Write a message of up to 20,000 characters.")
    audience = clean_audience(roles)
    blast = EmailBlast(
        subject=subject,
        body=body,
        audience_roles=audience,
        is_official_notice=official,
        created_by_id=author.id,
        recipient_count=len(recipients(db, audience, official=official)),
    )
    db.add(blast)
    db.flush()
    return blast


def queue_blast(db: Session, blast: EmailBlast, sender: User) -> int:
    """Queue the blast. Idempotent: a blast is only ever queued once.

    The draft is claimed with a conditional UPDATE, so of two sends at the same time
    (two tabs, two board members, a double tap) only one matches the row; on Postgres
    the second waits for the first to commit, then matches nothing.
    """
    if blast.status != "draft":
        raise BlastError("This announcement has already been sent.")
    people = recipients(db, blast.audience_roles, official=blast.is_official_notice)
    if not people:
        raise BlastError("No one in the chosen groups can receive this message.")
    queued_at = utcnow()
    claimed = db.execute(
        update(EmailBlast)
        .where(EmailBlast.id == blast.id, EmailBlast.status == "draft")
        .values(status="queued", queued_at=queued_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not claimed:
        raise BlastError("This announcement has already been sent.")
    paragraphs = [p.strip() for p in blast.body.split("\n\n") if p.strip()]
    for person in people:
        enqueue(
            db,
            to=person.email,
            subject=blast.subject,
            template="blast",
            kind="blast",
            blast_id=blast.id,
            body=blast.body,
            paragraphs=paragraphs,
            sender_name=sender.display_name,
            official=blast.is_official_notice,
        )
    blast.status = "queued"
    blast.queued_at = queued_at
    blast.recipient_count = len(people)
    return len(people)
