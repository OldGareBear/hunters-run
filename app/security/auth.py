"""Server-side sessions, request dependencies and CSRF protection."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import User, UserSession, as_utc
from app.permissions import Perm, has_perm
from app.security.tokens import hash_token, new_token, tokens_equal

CSRF_COOKIE = "hoa_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFError(Exception):
    pass


def client_ip(request: Request) -> str:
    # Behind a proxy, run uvicorn with --proxy-headers --forwarded-allow-ips so
    # request.client reflects the real client address.
    return request.client.host if request.client else ""


# --- Sessions ---------------------------------------------------------------


def start_session(db: Session, user: User, request: Request, response: Response) -> UserSession:
    s = get_settings()
    token = new_token()
    sess = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=new_token(24),
        expires_at=utcnow() + timedelta(hours=s.session_ttl_hours),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    user.last_login_at = utcnow()
    db.add(sess)
    db.flush()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        secure=s.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    # Rotate the CSRF token on privilege change (login).
    set_csrf_cookie(response, new_token(24))
    return sess


def end_session(db: Session, request: Request, response: Response) -> None:
    s = get_settings()
    token = request.cookies.get(s.session_cookie_name)
    if token:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        if sess:
            db.delete(sess)
    response.delete_cookie(s.session_cookie_name, path="/")


def revoke_user_sessions(db: Session, user_id: int) -> None:
    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        db.delete(sess)


def _load_session(request: Request, db: Session) -> UserSession | None:
    if "session_obj" in request.state._state:
        return request.state.session_obj
    s = get_settings()
    token = request.cookies.get(s.session_cookie_name)
    sess = None
    if token:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        if sess is not None:
            now = utcnow()
            if as_utc(sess.expires_at) <= now or not sess.user.is_active:
                db.delete(sess)
                db.commit()
                sess = None
            elif now - as_utc(sess.last_seen_at) > timedelta(minutes=5):
                sess.last_seen_at = now
                db.commit()
    request.state.session_obj = sess
    request.state.user = sess.user if sess else None
    return sess


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    sess = _load_session(request, db)
    return sess.user if sess else None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user_optional(request, db)
    if user is None:
        raise LoginRequired()
    return user


def require_perm(perm: Perm):
    def _dep(user: User = Depends(require_user)) -> User:
        if not has_perm(user, perm):
            raise Forbidden()
        return user

    return _dep


# --- CSRF (double-submit cookie) ---------------------------------------------


def set_csrf_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        value,
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )


def csrf_token_for(request: Request) -> str:
    """The token templates embed in forms. Created lazily and set by middleware."""
    token = getattr(request.state, "csrf_token", None)
    if token:
        return token
    token = request.cookies.get(CSRF_COOKIE) or new_token(24)
    request.state.csrf_token = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            supplied = str(form.get(CSRF_FIELD, ""))
    if not cookie or not supplied or not tokens_equal(cookie, supplied):
        raise CSRFError()
