"""The inbox people cc: the emails the portal kept from it, for board and management."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import InboundEmail, User
from app.permissions import Perm
from app.security.auth import require_perm
from app.services import inbound_email
from app.web import render

router = APIRouter(prefix="/inbox")
reader = require_perm(Perm.INBOX_VIEW)


@router.get("")
def index(request: Request, user: User = Depends(reader), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=get_settings().inbox_retention_days)
    return render(
        request,
        "inbox/list.html",
        emails=inbound_email.recent(db, user.role, since),
        state=inbound_email.last_state(db),
    )


@router.get("/{email_id}")
def show(
    request: Request, email_id: int, user: User = Depends(reader), db: Session = Depends(get_db)
):
    email = db.get(InboundEmail, email_id)
    if email is None or not inbound_email.may_see(user, email):
        raise HTTPException(404)
    return render(request, "inbox/show.html", email=email)
