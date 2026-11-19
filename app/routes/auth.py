from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import User
from app.security.auth import client_ip, current_user_optional, end_session, start_session
from app.security.passwords import password_problems
from app.services import accounts
from app.web import kick_outbox, redirect, render, safe_next

router = APIRouter()


@router.get("/login")
def login_form(
    request: Request, next: str = "/", user: User | None = Depends(current_user_optional)
):
    if user:
        return redirect(safe_next(next))
    return render(request, "auth/login.html", next=safe_next(next))


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    try:
        user = accounts.authenticate(db, email, password, ip)
    except accounts.AccountError as exc:
        return render(request, "auth/login.html", 429, error=str(exc), email=email, next=next)
    if user is None:
        return render(
            request,
            "auth/login.html",
            400,
            error="That email and password don't match an active account.",
            email=email,
            next=next,
        )
    resp = redirect(safe_next(next))
    start_session(db, user, request, resp)
    audit(db, "auth.login", actor=user, request=request)
    db.commit()
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    resp = redirect("/login", "You're signed out.")
    end_session(db, request, resp)
    db.commit()
    return resp


@router.get("/invite/{token}")
def invite_form(request: Request, token: str, db: Session = Depends(get_db)):
    invite = accounts.find_invite(db, token)
    if invite is None:
        return render(request, "auth/invalid_link.html", 404, what="invite")
    return render(request, "auth/accept_invite.html", invite=invite, token=token)


@router.post("/invite/{token}")
def accept_invite(
    request: Request,
    token: str,
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    invite = accounts.find_invite(db, token)
    if invite is None:
        return render(request, "auth/invalid_link.html", 404, what="invite")
    problems = password_problems(password, invite.email)
    if password != password_confirm:
        problems.append("The two passwords don't match.")
    if not full_name.strip():
        problems.append("Enter your name.")
    if problems:
        return render(
            request,
            "auth/accept_invite.html",
            400,
            invite=invite,
            token=token,
            errors=problems,
            full_name=full_name,
        )
    try:
        user = accounts.accept_invite(db, invite, full_name=full_name, password=password)
    except accounts.AccountError as exc:
        return render(request, "auth/invalid_link.html", 400, what="invite", error=str(exc))
    resp = redirect("/", f"Welcome, {user.display_name}! Your account is ready.")
    start_session(db, user, request, resp)
    audit(
        db,
        "user.invite_accepted",
        actor=user,
        target_type="user",
        target_id=user.id,
        request=request,
        role=user.role,
    )
    db.commit()
    return resp


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "auth/forgot.html")


@router.post("/forgot")
def forgot(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    accounts.request_password_reset(db, email)
    db.commit()
    kick_outbox(background)
    return render(request, "auth/forgot.html", sent=True)


@router.get("/reset/{token}")
def reset_form(request: Request, token: str, db: Session = Depends(get_db)):
    if accounts.find_reset(db, token) is None:
        return render(request, "auth/invalid_link.html", 404, what="reset")
    return render(request, "auth/reset.html", token=token)


@router.post("/reset/{token}")
def reset(
    request: Request,
    token: str,
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    reset_row = accounts.find_reset(db, token)
    if reset_row is None:
        return render(request, "auth/invalid_link.html", 404, what="reset")
    user = db.get(User, reset_row.user_id)
    problems = password_problems(password, user.email if user else "")
    if password != password_confirm:
        problems.append("The two passwords don't match.")
    if problems:
        return render(request, "auth/reset.html", 400, token=token, errors=problems)
    try:
        user = accounts.complete_reset(db, reset_row, password)
    except accounts.AccountError as exc:
        return render(request, "auth/invalid_link.html", 400, what="reset", error=str(exc))
    from app.security.auth import revoke_user_sessions

    revoke_user_sessions(db, user.id)
    audit(db, "auth.password_reset", actor=user, request=request)
    db.commit()
    return redirect("/login", "Your password is updated. Sign in with the new one.")
