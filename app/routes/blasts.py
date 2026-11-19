from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import EmailBlast, EmailMessage, User
from app.permissions import Perm
from app.security.auth import require_perm
from app.services import blasts as blast_service
from app.web import kick_outbox, redirect, render

router = APIRouter(prefix="/blasts")


def _get(db: Session, blast_id: int) -> EmailBlast:
    blast = db.get(EmailBlast, blast_id)
    if blast is None:
        raise HTTPException(404)
    return blast


@router.get("")
def index(
    request: Request,
    user: User = Depends(require_perm(Perm.BLASTS_SEND)),
    db: Session = Depends(get_db),
):
    blasts = list(db.scalars(select(EmailBlast).order_by(EmailBlast.id.desc()).limit(100)))
    return render(request, "blasts/list.html", blasts=blasts)


@router.get("/new")
def new(request: Request, user: User = Depends(require_perm(Perm.BLASTS_SEND))):
    return render(request, "blasts/new.html", roles=["homeowner"])


@router.post("")
def create(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    roles: list[str] = Form(default=[]),
    official: str = Form(""),
    user: User = Depends(require_perm(Perm.BLASTS_SEND)),
    db: Session = Depends(get_db),
):
    try:
        blast = blast_service.create_draft(
            db, author=user, subject=subject, body=body, roles=roles, official=official == "1"
        )
    except blast_service.BlastError as exc:
        return render(
            request,
            "blasts/new.html",
            400,
            error=str(exc),
            subject=subject,
            body=body,
            roles=roles,
            official=official == "1",
        )
    audit(
        db,
        "blast.drafted",
        actor=user,
        target_type="blast",
        target_id=blast.id,
        request=request,
        roles=blast.audience_roles,
    )
    db.commit()
    return redirect(f"/blasts/{blast.id}")


@router.get("/{blast_id}")
def show(
    request: Request,
    blast_id: int,
    user: User = Depends(require_perm(Perm.BLASTS_SEND)),
    db: Session = Depends(get_db),
):
    blast = _get(db, blast_id)
    counts = dict(
        db.execute(
            select(EmailMessage.status, func.count())
            .where(EmailMessage.blast_id == blast.id)
            .group_by(EmailMessage.status)
        ).all()
    )
    preview = []
    if blast.status == "draft":
        people = blast_service.recipients(
            db, blast.audience_roles, official=blast.is_official_notice
        )
        blast.recipient_count = len(people)
        preview = people[:25]
    return render(request, "blasts/show.html", blast=blast, counts=counts, preview=preview)


@router.post("/{blast_id}/send")
def send(
    request: Request,
    background: BackgroundTasks,
    blast_id: int,
    confirm_count: int = Form(...),
    user: User = Depends(require_perm(Perm.BLASTS_SEND)),
    db: Session = Depends(get_db),
):
    blast = _get(db, blast_id)
    current = len(
        blast_service.recipients(db, blast.audience_roles, official=blast.is_official_notice)
    )
    if current != confirm_count:
        return redirect(
            f"/blasts/{blast.id}",
            f"The audience changed to {current} people since you previewed it. "
            "Check it and send again.",
            "error",
        )
    try:
        n = blast_service.queue_blast(db, blast, user)
    except blast_service.BlastError as exc:
        return redirect(f"/blasts/{blast.id}", str(exc), "error")
    audit(
        db,
        "blast.sent",
        actor=user,
        target_type="blast",
        target_id=blast.id,
        request=request,
        recipients=n,
        roles=blast.audience_roles,
        official=blast.is_official_notice,
    )
    db.commit()
    kick_outbox(background)
    return redirect(f"/blasts/{blast.id}", f"Queued for {n} recipients.")


@router.post("/{blast_id}/delete")
def delete(
    request: Request,
    blast_id: int,
    user: User = Depends(require_perm(Perm.BLASTS_SEND)),
    db: Session = Depends(get_db),
):
    blast = _get(db, blast_id)
    if blast.status != "draft":
        return redirect(f"/blasts/{blast.id}", "Sent announcements can't be deleted.", "error")
    db.delete(blast)
    audit(
        db,
        "blast.draft_deleted",
        actor=user,
        target_type="blast",
        target_id=blast_id,
        request=request,
    )
    db.commit()
    return redirect("/blasts", "Draft deleted.")
