from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import User, UserSession
from app.security.auth import require_user
from app.security.passwords import hash_password, password_problems, verify_password
from app.web import redirect, render

router = APIRouter(prefix="/account")


@router.get("")
def account(request: Request, user: User = Depends(require_user)):
    return render(request, "account/account.html")


@router.post("/profile")
def profile(
    request: Request,
    full_name: str = Form(""),
    announcements: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    user = db.merge(user)
    user.full_name = full_name.strip()[:200]
    user.email_opt_out_announcements = announcements != "1"
    db.commit()
    return redirect("/account", "Saved.")


@router.post("/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    user = db.merge(user)
    if not verify_password(user.password_hash, current):
        return render(
            request, "account/account.html", 400, errors=["Your current password isn't right."]
        )
    problems = password_problems(new, user.email)
    if new != confirm:
        problems.append("The two new passwords don't match.")
    if problems:
        return render(request, "account/account.html", 400, errors=problems)
    user.password_hash = hash_password(new)
    # Sign out every other device.
    current_sess = request.state.session_obj
    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        if current_sess is None or sess.id != current_sess.id:
            db.delete(sess)
    audit(db, "auth.password_changed", actor=user, request=request)
    db.commit()
    return redirect("/account", "Password changed. Other devices were signed out.")
