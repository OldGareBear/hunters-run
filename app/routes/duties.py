"""The duty calendar: recurring board and management duties and their reminder emails."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import Duty, User
from app.permissions import Perm
from app.security.auth import require_perm
from app.services import duties as duty_service
from app.web import redirect, render

router = APIRouter(prefix="/duties")
manager = require_perm(Perm.DUTIES_MANAGE)


def _get(db: Session, duty_id: int) -> Duty:
    duty = db.get(Duty, duty_id)
    if duty is None:
        raise HTTPException(404)
    return duty


def _all(db: Session) -> list[Duty]:
    return sorted(db.scalars(select(Duty)), key=lambda d: d.title.lower())


def _index(request: Request, db: Session, status: int = 200, **ctx):
    today = duty_service.local_today()
    duties = _all(db)
    return render(
        request,
        "duties/list.html",
        status,
        today=today,
        upcoming=duty_service.upcoming(db, today),
        duties=duties,
        next_due={d.id: duty_service.next_due(d, today) for d in duties},
        **ctx,
    )


@router.get("")
def index(request: Request, user: User = Depends(manager), db: Session = Depends(get_db)):
    return _index(request, db)


@router.post("/upload")
def upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(manager),
    db: Session = Depends(get_db),
):
    data = file.file.read(duty_service.MAX_CSV_BYTES + 1)
    try:
        rows = duty_service.parse_csv(data)
    except duty_service.DutyError as exc:
        return _index(request, db, 400, error=str(exc), errors=exc.errors)
    added, updated = duty_service.import_rows(db, rows, by=user)
    audit(db, "duties.uploaded", actor=user, request=request, added=added, updated=updated)
    db.commit()
    return redirect("/duties", f"Uploaded: {added} added, {updated} updated.")


@router.get("/duties.csv")
def export(user: User = Depends(manager), db: Session = Depends(get_db)):
    body = duty_service.to_csv(_all(db))
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="duties.csv"'},
    )


@router.get("/template.csv")
def template(user: User = Depends(manager)):
    return Response(
        duty_service.template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="duties-template.csv"'},
    )


def _form(request: Request, duty: Duty | None, status: int = 200, **values):
    if duty is not None and not values:
        values = {
            "title": duty.title,
            "details": duty.details,
            "owners": "; ".join(duty.owners),
            "first_due": duty.first_due.isoformat(),
            "repeat": duty.repeat,
            "remind_days": "; ".join(str(n) for n in duty.remind_days),
        }
    values.setdefault("remind_days", "7; 0")
    return render(
        request,
        "duties/form.html",
        status,
        duty=duty,
        repeats=list(duty_service.REPEATS),
        v=values,
        error=values.pop("error", None),
    )


@router.get("/new")
def new(request: Request, user: User = Depends(manager)):
    return _form(request, None)


@router.get("/{duty_id}")
def edit(
    request: Request, duty_id: int, user: User = Depends(manager), db: Session = Depends(get_db)
):
    return _form(request, _get(db, duty_id))


def _save(request: Request, db: Session, user: User, duty: Duty | None, values: dict):
    try:
        fields = duty_service.parse_fields(**values)
        saved = duty_service.save(db, fields, by=user, duty=duty)
    except (ValueError, duty_service.DutyError) as exc:
        msg = str(exc)
        return _form(request, duty, 400, error=msg[:1].upper() + msg[1:], **values)
    db.flush()
    audit(
        db,
        "duty.updated" if duty else "duty.created",
        actor=user,
        target_type="duty",
        target_id=saved.id,
        request=request,
    )
    db.commit()
    return redirect("/duties", "Saved.")


@router.post("")
def create(
    request: Request,
    title: str = Form(""),
    details: str = Form(""),
    owners: str = Form(""),
    first_due: str = Form(""),
    repeat: str = Form("none"),
    remind_days: str = Form(""),
    user: User = Depends(manager),
    db: Session = Depends(get_db),
):
    values = dict(
        title=title,
        details=details,
        owners=owners,
        first_due=first_due,
        repeat=repeat,
        remind_days=remind_days,
    )
    return _save(request, db, user, None, values)


@router.post("/{duty_id}")
def update(
    request: Request,
    duty_id: int,
    title: str = Form(""),
    details: str = Form(""),
    owners: str = Form(""),
    first_due: str = Form(""),
    repeat: str = Form("none"),
    remind_days: str = Form(""),
    user: User = Depends(manager),
    db: Session = Depends(get_db),
):
    values = dict(
        title=title,
        details=details,
        owners=owners,
        first_due=first_due,
        repeat=repeat,
        remind_days=remind_days,
    )
    return _save(request, db, user, _get(db, duty_id), values)


@router.post("/{duty_id}/delete")
def delete(
    request: Request, duty_id: int, user: User = Depends(manager), db: Session = Depends(get_db)
):
    duty = _get(db, duty_id)
    audit(db, "duty.deleted", actor=user, target_type="duty", target_id=duty.id, request=request)
    db.delete(duty)
    db.commit()
    return redirect("/duties", "Deleted.")
