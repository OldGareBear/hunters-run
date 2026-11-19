"""Shared helpers for HTML routes: templates, flash messages, redirects, outbox kick."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from markupsafe import Markup, escape

from app.config import get_settings
from app.db import session_factory
from app.permissions import ALL_ROLES, SHAREABLE_ROLES, Perm, Role, has_perm
from app.security.auth import CSRF_FIELD, csrf_token_for

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
FLASH_COOKIE = "hoa_flash"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="flash")


def csrf_input(request: Request) -> Markup:
    return Markup('<input type="hidden" name="{}" value="{}">').format(
        CSRF_FIELD, csrf_token_for(request)
    )


def role_label(role: str) -> str:
    try:
        return Role(role).label
    except ValueError:
        return role


def nl2p(text: str) -> Markup:
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    para = Markup("<p>{}</p>")
    br = Markup("<br>")
    return Markup("").join(para.format(escape(p).replace("\n", br)) for p in paras)


templates.env.globals.update(
    csrf_input=csrf_input,
    has_perm=has_perm,
    Perm=Perm,
    Role=Role,
    ALL_ROLES=ALL_ROLES,
    SHAREABLE_ROLES=SHAREABLE_ROLES,
    role_label=role_label,
    settings=get_settings,
)
templates.env.filters["nl2p"] = nl2p


def _reservation_helpers() -> None:
    from app.services import reservations
    from app.services.accounts import may_set_unit

    templates.env.globals.update(
        fmt_when=reservations.fmt_when,
        fmt_time=reservations.fmt_time,
        fmt_minutes=reservations.fmt_minutes,
        space_name=reservations.space_name,
        local_time=reservations.local,
        may_set_unit=may_set_unit,
    )


_reservation_helpers()


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    flash = None
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        try:
            flash = _serializer().loads(raw)
        except BadSignature:
            flash = None
        request.state.clear_flash = True
    ctx.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(
        request, name, {"flash": flash, **ctx}, status_code=status_code
    )


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative redirects."""
    if not url:
        return default
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return default
    return url


def redirect(url: str, flash: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie(
            FLASH_COOKIE,
            _serializer().dumps({"text": flash, "kind": kind}),
            max_age=60,
            httponly=True,
            secure=get_settings().session_cookie_secure,
            samesite="lax",
        )
    return resp


def _deliver() -> None:
    from app.email.outbox import deliver_pending

    db = session_factory()()
    try:
        deliver_pending(db)
    except Exception:  # pragma: no cover - logged, the worker will retry
        log.exception("inline email delivery failed")
    finally:
        db.close()


def kick_outbox(background: BackgroundTasks) -> None:
    if get_settings().email_send_inline:
        background.add_task(_deliver)
