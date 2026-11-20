from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db, utcnow
from app.models import AuditLog, Invite, User
from app.permissions import Perm, Role, has_perm, roles_user_may_invite
from app.security.auth import require_perm, revoke_user_sessions
from app.services import accounts
from app.web import kick_outbox, redirect, render

router = APIRouter(prefix="/admin")


@router.get("/users")
def users(
    request: Request,
    user: User = Depends(require_perm(Perm.USERS_VIEW)),
    db: Session = Depends(get_db),
):
    people = list(db.scalars(select(User).order_by(User.role, User.email)))
    invites = [
        i for i in db.scalars(select(Invite).order_by(Invite.created_at.desc())) if i.is_pending
    ]
    return render(
        request,
        "admin/users.html",
        people=people,
        invites=invites,
        invite_roles=roles_user_may_invite(user),
    )


@router.post("/invites")
def invite(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    role: str = Form(...),
    unit: str = Form(""),
    user: User = Depends(require_perm(Perm.USERS_VIEW)),
    db: Session = Depends(get_db),
):
    try:
        role_enum = Role(role)
    except ValueError as exc:
        raise HTTPException(400) from exc
    if role_enum not in roles_user_may_invite(user):
        raise HTTPException(403)
    try:
        inv = accounts.create_invite(db, email=email, role=role_enum, invited_by=user, unit=unit)
    except accounts.AccountError as exc:
        return redirect("/admin/users", str(exc), "error")
    audit(
        db,
        "user.invited",
        actor=user,
        target_type="invite",
        target_id=inv.id,
        request=request,
        email=inv.email,
        role=inv.role,
    )
    db.commit()
    kick_outbox(background)
    return redirect("/admin/users", f"Invite sent to {inv.email}.")


@router.post("/invites/{invite_id}/revoke")
def revoke_invite(
    request: Request,
    invite_id: int,
    user: User = Depends(require_perm(Perm.USERS_VIEW)),
    db: Session = Depends(get_db),
):
    inv = db.get(Invite, invite_id)
    if inv is None or not inv.is_pending:
        raise HTTPException(404)
    if not (has_perm(user, Perm.USERS_MANAGE) or inv.invited_by_id == user.id):
        raise HTTPException(403)
    inv.revoked_at = utcnow()
    audit(
        db,
        "user.invite_revoked",
        actor=user,
        target_type="invite",
        target_id=inv.id,
        request=request,
        email=inv.email,
    )
    db.commit()
    return redirect("/admin/users", f"Invite for {inv.email} revoked.")


def _target(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if target.id == actor.id:
        raise HTTPException(400, "You can't change your own role or access here.")
    return target


@router.post("/users/{user_id}/role")
def change_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    user: User = Depends(require_perm(Perm.USERS_MANAGE)),
    db: Session = Depends(get_db),
):
    target = _target(db, user, user_id)
    try:
        new_role = Role(role)
    except ValueError as exc:
        raise HTTPException(400) from exc
    old = target.role
    if old == Role.ADMIN.value and new_role != Role.ADMIN and accounts.active_admin_count(db) <= 1:
        return redirect("/admin/users", "There must be at least one active administrator.", "error")
    target.role = new_role.value
    revoke_user_sessions(db, target.id)  # take effect immediately
    audit(
        db,
        "user.role_changed",
        actor=user,
        target_type="user",
        target_id=target.id,
        request=request,
        before=old,
        after=new_role.value,
    )
    db.commit()
    return redirect("/admin/users", f"{target.display_name} is now {new_role.label}.")


@router.post("/users/{user_id}/unit")
def set_unit(
    request: Request,
    user_id: int,
    unit: str = Form(""),
    user: User = Depends(require_perm(Perm.USERS_VIEW)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    if not accounts.may_set_unit(user, target):
        raise HTTPException(403)
    before = target.unit
    target.unit = accounts.clean_unit(unit)
    audit(
        db,
        "user.unit_changed",
        actor=user,
        target_type="user",
        target_id=target.id,
        request=request,
        before=before,
        after=target.unit,
    )
    db.commit()
    return redirect("/admin/users", f"Saved the unit for {target.display_name}.")


@router.post("/users/{user_id}/active")
def set_active(
    request: Request,
    user_id: int,
    active: str = Form(...),
    user: User = Depends(require_perm(Perm.USERS_MANAGE)),
    db: Session = Depends(get_db),
):
    target = _target(db, user, user_id)
    make_active = active == "1"
    if not make_active and target.role == Role.ADMIN.value and accounts.active_admin_count(db) <= 1:
        return redirect("/admin/users", "There must be at least one active administrator.", "error")
    target.is_active = make_active
    if not make_active:
        revoke_user_sessions(db, target.id)
    audit(
        db,
        "user.activated" if make_active else "user.deactivated",
        actor=user,
        target_type="user",
        target_id=target.id,
        request=request,
    )
    db.commit()
    verb = "reactivated" if make_active else "deactivated"
    return redirect("/admin/users", f"{target.display_name} {verb}.")


@router.get("/audit")
def audit_log(
    request: Request,
    page: int = 1,
    user: User = Depends(require_perm(Perm.AUDIT_VIEW)),
    db: Session = Depends(get_db),
):
    page = max(page, 1)
    rows = list(
        db.scalars(
            select(AuditLog).order_by(AuditLog.id.desc()).offset((page - 1) * 100).limit(101)
        )
    )
    return render(request, "admin/audit.html", rows=rows[:100], page=page, has_more=len(rows) > 100)
