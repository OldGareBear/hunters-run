"""Invites, sign-in, password resets and user administration."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.email.outbox import enqueue
from app.models import Invite, LoginAttempt, PasswordReset, User, as_utc
from app.permissions import Perm, Role, has_perm
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.tokens import hash_token, new_token


class AccountError(Exception):
    pass


def normalise_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 320 or " " in email:
        raise AccountError("Enter a valid email address.")
    return email


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


# --- Invites ------------------------------------------------------------------


def clean_unit(unit: str) -> str:
    return " ".join((unit or "").split())[:80]


def may_set_unit(actor: User, target: User) -> bool:
    """Admins can set anyone's unit; board and management can for everyone but admins
    and themselves (joining another household would share its bookings and limits)."""
    if has_perm(actor, Perm.USERS_MANAGE):
        return True
    return (
        has_perm(actor, Perm.USERS_INVITE_HOMEOWNER)
        and target.role != Role.ADMIN.value
        and target.id != actor.id
    )


def create_invite(
    db: Session, *, email: str, role: Role, invited_by: User | None, unit: str = ""
) -> Invite:
    email = normalise_email(email)
    if get_user_by_email(db, email):
        raise AccountError("Someone with that email already has an account.")
    # Revoke any earlier pending invite for this address so only one link works.
    for old in db.scalars(select(Invite).where(Invite.email == email)):
        if old.is_pending:
            old.revoked_at = utcnow()
    token = new_token()
    invite = Invite(
        email=email,
        role=role.value,
        unit=clean_unit(unit),
        token_hash=hash_token(token),
        invited_by_id=invited_by.id if invited_by else None,
        expires_at=utcnow() + timedelta(hours=get_settings().invite_ttl_hours),
    )
    db.add(invite)
    db.flush()
    enqueue(
        db,
        to=email,
        subject=f"You're invited to the {get_settings().hoa_name} portal",
        template="invite",
        kind="invite",
        link=f"{get_settings().base_url.rstrip('/')}/invite/{token}",
        role_label=role.label,
        inviter=invited_by.display_name if invited_by else "",
        ttl_days=max(1, get_settings().invite_ttl_hours // 24),
    )
    return invite


def find_invite(db: Session, token: str) -> Invite | None:
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    return invite if invite and invite.is_pending else None


def accept_invite(db: Session, invite: Invite, *, full_name: str, password: str) -> User:
    if get_user_by_email(db, invite.email):
        raise AccountError("This invite has already been used.")
    user = User(
        email=invite.email,
        full_name=full_name.strip()[:200],
        role=invite.role,
        unit=invite.unit,
        password_hash=hash_password(password),
    )
    db.add(user)
    invite.accepted_at = utcnow()
    db.flush()
    return user


# --- Sign in --------------------------------------------------------------------


def is_locked_out(db: Session, email: str) -> bool:
    s = get_settings()
    since = utcnow() - timedelta(minutes=s.login_lockout_minutes)
    # Failures before the person last reset (or changed) their password don't count, so
    # someone locked out by a stranger guessing at their address can always get back in.
    user = get_user_by_email(db, email)
    if user is not None:
        reset_at = db.scalar(
            select(func.max(PasswordReset.used_at)).where(PasswordReset.user_id == user.id)
        )
        if reset_at is not None:
            since = max(since, as_utc(reset_at))
    failures = db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.email == email,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= since,
        )
    )
    return (failures or 0) >= s.login_max_failures


def authenticate(db: Session, email: str, password: str, ip: str) -> User | None:
    email = email.strip().lower()[:320]
    if is_locked_out(db, email):
        raise AccountError("Too many failed attempts. Wait a few minutes or reset your password.")
    user = get_user_by_email(db, email)
    ok = verify_password(user.password_hash if user else None, password)
    ok = ok and user is not None and user.is_active
    db.add(LoginAttempt(email=email, ip=ip, success=bool(ok)))
    if not ok:
        db.commit()
        return None
    assert user is not None
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


# --- Password reset ---------------------------------------------------------------

RESETS_PER_HOUR = 3  # reset emails per person; more requests are dropped silently


def request_password_reset(db: Session, email: str) -> None:
    """Always behaves the same whether or not the account exists."""
    try:
        email = normalise_email(email)
    except AccountError:
        return
    user = get_user_by_email(db, email)
    if user is None or not user.is_active:
        return
    recent = db.scalar(
        select(func.count(PasswordReset.id)).where(
            PasswordReset.user_id == user.id,
            PasswordReset.created_at >= utcnow() - timedelta(hours=1),
        )
    )
    if (recent or 0) >= RESETS_PER_HOUR:  # anyone can ask; don't let them flood a mailbox
        return
    token = new_token()
    s = get_settings()
    db.add(
        PasswordReset(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(minutes=s.password_reset_ttl_minutes),
        )
    )
    enqueue(
        db,
        to=user.email,
        subject=f"Reset your {s.hoa_name} portal password",
        template="password_reset",
        kind="reset",
        link=f"{s.base_url.rstrip('/')}/reset/{token}",
        ttl_minutes=s.password_reset_ttl_minutes,
    )


def find_reset(db: Session, token: str) -> PasswordReset | None:
    reset = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == hash_token(token)))
    if reset is None or reset.used_at is not None or as_utc(reset.expires_at) <= utcnow():
        return None
    return reset


def complete_reset(db: Session, reset: PasswordReset, password: str) -> User:
    user = db.get(User, reset.user_id)
    if user is None or not user.is_active:
        raise AccountError("This account is no longer active.")
    user.password_hash = hash_password(password)
    reset.used_at = utcnow()
    void_resets(db, user)  # every other outstanding link
    return user


def void_resets(db: Session, user: User) -> None:
    """End every outstanding reset link for this person (their password just changed)."""
    for other in db.scalars(
        select(PasswordReset).where(
            PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None)
        )
    ):
        other.used_at = utcnow()


def active_admin_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == Role.ADMIN.value, User.is_active.is_(True))
        )
        or 0
    )
