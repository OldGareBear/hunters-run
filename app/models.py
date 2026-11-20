"""ORM models."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base, utcnow

EMBEDDING_DIM = get_settings().embedding_dim


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalise so comparisons with aware datetimes work."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(32), index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_opt_out_announcements: Mapped[bool] = mapped_column(Boolean, default=False)
    # Home address or unit number. People with the same unit share one household's
    # booking limits; leave blank and the person counts as their own household.
    unit: Mapped[str] = mapped_column(String(80), default="", server_default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def display_name(self) -> str:
        return self.full_name or self.email

    @property
    def household(self) -> str:
        unit = " ".join(self.unit.split()).lower()
        return f"unit:{unit}" if unit else f"user:{self.id}"


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(80), default="", server_default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    expires_at: Mapped[datetime] = _ts()
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    invited_by: Mapped[User | None] = relationship(foreign_keys=[invited_by_id])

    @property
    def is_pending(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and as_utc(self.expires_at) > utcnow()
        )


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    expires_at: Mapped[datetime] = _ts()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    expires_at: Mapped[datetime] = _ts()
    last_seen_at: Mapped[datetime] = _ts(default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_email_created", "email", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    ip: Mapped[str] = mapped_column(String(64), default="")
    success: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _ts(default=utcnow)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="processing")  # processing|ready|failed
    error: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "upload" (stored by the portal) or "dropbox" (read from the board's folder).
    source: Mapped[str] = mapped_column(String(20), default="upload", server_default="upload")
    source_id: Mapped[str | None] = mapped_column(String(200), unique=True)  # Dropbox file id
    source_path: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    source_rev: Mapped[str] = mapped_column(String(100), default="", server_default="")

    uploaded_by: Mapped[User | None] = relationship()
    access: Mapped[list[DocumentAccess]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def role_names(self) -> list[str]:
        return sorted(a.role for a in self.access)


class SyncState(Base):
    """One row per external document source; doubles as a lock between workers."""

    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")


class DocumentAccess(Base):
    __tablename__ = "document_access"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)

    document: Mapped[Document] = relationship(back_populates="access")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # Approximate nearest-neighbour index (Postgres/pgvector only).
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Full-text index for keyword search (Postgres only; see app/rag/retrieval.py).
        Index(
            "ix_document_chunks_fts",
            text("to_tsvector('english', text)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship()
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # [{"n": 1, "document_id": 3, "title": "...", "page": 2, "quote": "..."}]
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    answered: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ChatUsage(Base):
    """One row per question put to the assistant, for the usage limits.

    Kept apart from chat_messages so deleting a conversation doesn't reset them.
    """

    __tablename__ = "chat_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str] = mapped_column(String(80), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0)  # estimated, USD x 1e6
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)


class EmailBlast(Base):
    __tablename__ = "email_blasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    audience_roles: Mapped[list[str]] = mapped_column(JSON)
    is_official_notice: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|queued|sent
    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[User | None] = relationship()


class EmailMessage(Base):
    """Transactional outbox. Every email the app sends goes through this table."""

    __tablename__ = "email_messages"
    __table_args__ = (Index("ix_email_messages_status_next", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    to_email: Mapped[str] = mapped_column(Text)  # one or more addresses, comma separated
    cc_email: Mapped[str] = mapped_column(Text, default="", server_default="")
    reply_to: Mapped[str] = mapped_column(String(320), default="", server_default="")
    subject: Mapped[str] = mapped_column(String(300))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(30))  # invite|reset|blast|escalation|reservation
    blast_id: Mapped[int | None] = mapped_column(ForeignKey("email_blasts.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    next_attempt_at: Mapped[datetime] = _ts(default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


RESERVATION_ACTIVE = ("pending", "confirmed")


class Reservation(Base):
    """A booking of one amenity space. Times are stored in UTC."""

    __tablename__ = "reservations"
    __table_args__ = (
        Index("ix_reservations_space_starts", "space", "starts_at"),
        Index("ix_reservations_group_household", "amenity_group", "household", "starts_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    amenity_group: Mapped[str] = mapped_column(String(60))
    space: Mapped[str] = mapped_column(String(60))
    # "booking" by a household, or "block" (management closing the space).
    kind: Mapped[str] = mapped_column(String(20), default="booking")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    household: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[datetime] = _ts()
    ends_at: Mapped[datetime] = _ts()
    # pending (waiting for approval) | confirmed | declined | cancelled
    status: Mapped[str] = mapped_column(String(20), default="confirmed", index=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts(default=utcnow)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(Text, default="")
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    # True when the household cancelled inside the late-cancel window, so the booking
    # still counts toward their daily and weekly limits.
    counts_after_cancel: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id])
    cancelled_by: Mapped[User | None] = relationship(foreign_keys=[cancelled_by_id])

    @property
    def is_active(self) -> bool:
        return self.status in RESERVATION_ACTIVE

    @property
    def start(self) -> datetime:
        return as_utc(self.starts_at)

    @property
    def end(self) -> datetime:
        return as_utc(self.ends_at)


class Duty(Base):
    """A recurring board or management duty, raised in the weekly review when it's near."""

    __tablename__ = "duties"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str] = mapped_column(Text, default="")
    # Who usually does it, if known: email addresses and/or role names ("board",
    # "management", "admin"). A hint for the weekly review, which picks the owner.
    owners: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_due: Mapped[date] = mapped_column(Date)
    repeat: Mapped[str] = mapped_column(String(20), default="none")
    # A hard deadline is always in the weekly review from three weeks out.
    hard_deadline: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # No longer used (the weekly review replaced fixed reminders); kept for the
    # previous release, which reads it.
    remind_days: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)


class DutyReminder(Base):
    """One reminder sent for one occurrence of a duty, by the previous release's fixed
    reminders. No longer written; kept so that release can still run."""

    __tablename__ = "duty_reminders"
    __table_args__ = (UniqueConstraint("duty_id", "due_on", "lead_days"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    duty_id: Mapped[int] = mapped_column(ForeignKey("duties.id", ondelete="CASCADE"), index=True)
    due_on: Mapped[date] = mapped_column(Date)
    lead_days: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = _ts(default=utcnow)


class WeeklyReview(Base):
    """One weekly review of the calendar and recent documents. The unique week is what
    stops a second run (or a second worker) emailing the same week twice."""

    __tablename__ = "weekly_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True)  # the Monday
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|sent|failed
    # "model" when the assistant wrote it, "plain" for the dates-only fallback.
    mode: Mapped[str] = mapped_column(String(20), default="")
    note: Mapped[str] = mapped_column(Text, default="")  # why it fell back, if it did
    emails: Mapped[int] = mapped_column(Integer, default=0)
    # Per recipient: the items sent, for the review page and the record.
    items: Mapped[list[dict]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = _ts(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _ts(default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()


__all__ = [
    "AuditLog",
    "ChatMessage",
    "Conversation",
    "Document",
    "Duty",
    "DutyReminder",
    "DocumentAccess",
    "DocumentChunk",
    "EmailBlast",
    "EmailMessage",
    "Invite",
    "LoginAttempt",
    "PasswordReset",
    "RESERVATION_ACTIVE",
    "Reservation",
    "User",
    "UserSession",
]
