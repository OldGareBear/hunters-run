from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db, utcnow
from app.models import Reservation, User
from app.permissions import Perm, has_perm
from app.security.auth import require_perm, require_user
from app.services import reservations as svc
from app.web import kick_outbox, redirect, render

router = APIRouter(prefix="/reservations")


def _may_use(user: User) -> None:
    if not (has_perm(user, Perm.RESERVATIONS_BOOK) or has_perm(user, Perm.RESERVATIONS_MANAGE)):
        raise HTTPException(403)


def _space(slug: str):
    found = svc.find_space(slug)
    if found is None:
        raise HTTPException(404)
    return found


def _get(db: Session, user: User, reservation_id: int) -> Reservation:
    r = db.get(Reservation, reservation_id)
    if r is None or not svc.can_view(user, r):
        raise HTTPException(404)
    return r


@router.get("")
def index(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _may_use(user)
    manager = has_perm(user, Perm.RESERVATIONS_MANAGE)
    return render(
        request,
        "reservations/index.html",
        groups=svc.groups(),
        mine=svc.upcoming_for(db, user) if has_perm(user, Perm.RESERVATIONS_BOOK) else [],
        pending_count=len(svc.pending_requests(db)) if manager else 0,
    )


@router.get("/manage")
def manage(
    request: Request,
    user: User = Depends(require_perm(Perm.RESERVATIONS_MANAGE)),
    db: Session = Depends(get_db),
):
    return render(
        request,
        "reservations/manage.html",
        pending=svc.pending_requests(db),
        upcoming=svc.upcoming_all(db),
    )


@router.get("/r/{reservation_id}")
def show(
    request: Request,
    reservation_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    r = _get(db, user, reservation_id)
    manager = has_perm(user, Perm.RESERVATIONS_MANAGE)
    can_cancel = r.is_active and r.end > utcnow()
    return render(
        request,
        "reservations/show.html",
        r=r,
        group=svc.group_by_key(r.amenity_group),
        manager=manager,
        can_cancel=can_cancel,
    )


@router.post("/r/{reservation_id}/cancel")
def cancel(
    request: Request,
    background: BackgroundTasks,
    reservation_id: int,
    reason: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    r = _get(db, user, reservation_id)
    try:
        svc.cancel(db, r, by=user, reason=reason)
    except svc.ReservationError as exc:
        return redirect(f"/reservations/r/{r.id}", str(exc), "error")
    audit(
        db,
        "reservation.cancelled",
        actor=user,
        target_type="reservation",
        target_id=r.id,
        request=request,
        space=r.space,
        counts_toward_limits=r.counts_after_cancel,
    )
    db.commit()
    kick_outbox(background)
    flash = "Cancelled."
    if r.counts_after_cancel:
        flash += " It was close to the start, so it still counts toward your limits."
    return redirect(f"/reservations/{r.space}?week={svc.local(r.start).date()}", flash)


@router.post("/r/{reservation_id}/decide")
def decide(
    request: Request,
    background: BackgroundTasks,
    reservation_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    user: User = Depends(require_perm(Perm.RESERVATIONS_MANAGE)),
    db: Session = Depends(get_db),
):
    r = _get(db, user, reservation_id)
    if decision not in ("approve", "decline"):
        raise HTTPException(400)
    try:
        svc.decide(db, r, by=user, approve=decision == "approve", note=note)
    except svc.ReservationError as exc:
        return redirect(f"/reservations/r/{r.id}", str(exc), "error")
    audit(
        db,
        "reservation.approved" if decision == "approve" else "reservation.declined",
        actor=user,
        target_type="reservation",
        target_id=r.id,
        request=request,
        space=r.space,
    )
    db.commit()
    kick_outbox(background)
    return redirect("/reservations/manage", "Approved." if r.status == "confirmed" else "Declined.")


@router.get("/{space}")
def calendar(
    request: Request,
    space: str,
    week: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _may_use(user)
    _space(space)
    try:
        day = date.fromisoformat(week) if week else svc.local(utcnow()).date()
    except ValueError:
        day = svc.local(utcnow()).date()
    view = svc.week_view(db, space, day, user)
    return render(
        request,
        "reservations/calendar.html",
        view=view,
        rules=svc.rule_summary(view.group),
    )


@router.get("/{space}/new")
def new(
    request: Request,
    space: str,
    start: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _may_use(user)
    group, name = _space(space)
    try:
        when = svc.parse_local(start)
    except svc.ReservationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return render(request, "reservations/new.html", **_form_ctx(user, group, space, name, when))


def _form_ctx(user, group, space, name, when, **extra):
    close = svc.at(when.date(), group.close_time)
    fits = [m for m in group.durations() if when.timestamp() + m * 60 <= close.timestamp()]
    block_lengths = [
        m
        for m in range(group.rules.slot_minutes, 24 * 60 + 1, group.rules.slot_minutes)
        if when.timestamp() + m * 60 <= close.timestamp()
    ]
    return {
        "group": group,
        "space": space,
        "space_label": name,
        "when": when,
        "start": when.strftime("%Y-%m-%dT%H:%M"),
        "durations": fits,
        "block_lengths": block_lengths,
        "can_book": has_perm(user, Perm.RESERVATIONS_BOOK),
        "can_block": has_perm(user, Perm.RESERVATIONS_MANAGE),
        "rules": svc.rule_summary(group),
        **extra,
    }


@router.post("/{space}")
def create(
    request: Request,
    background: BackgroundTasks,
    space: str,
    start: str = Form(...),
    minutes: str = Form(...),
    purpose: str = Form(""),
    block: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _may_use(user)
    group, name = _space(space)
    is_block = block == "1"
    if is_block and not has_perm(user, Perm.RESERVATIONS_MANAGE):
        raise HTTPException(403)
    length = int(minutes) if minutes.strip().isdigit() else 0
    try:
        when = svc.parse_local(start)
        if not length:
            raise svc.ReservationError("Choose how long you'd like to book.")
        r = svc.book(
            db,
            user=user,
            space=space,
            start=when,
            minutes=length,
            purpose=purpose,
            block=is_block,
        )
    except svc.ReservationError as exc:
        db.rollback()
        try:
            when = svc.parse_local(start)
        except svc.ReservationError:
            raise HTTPException(400, str(exc)) from exc
        ctx = _form_ctx(user, group, space, name, when, error=str(exc), purpose=purpose)
        return render(request, "reservations/new.html", 400, minutes=length, **ctx)
    action = (
        "reservation.blocked"
        if is_block
        else "reservation.requested"
        if r.status == "pending"
        else "reservation.booked"
    )
    audit(
        db,
        action,
        actor=user,
        target_type="reservation",
        target_id=r.id,
        request=request,
        space=space,
        starts_at=r.start.isoformat(),
        minutes=length,
    )
    db.commit()
    kick_outbox(background)
    if is_block:
        flash = f"{name} is closed {svc.fmt_when(r.start, r.end)}."
    elif r.status == "pending":
        flash = "Request sent. Management will confirm it by email."
    else:
        flash = f"Booked: {name}, {svc.fmt_when(r.start, r.end)}."
    return redirect(f"/reservations/{space}?week={svc.local(r.start).date()}", flash)
