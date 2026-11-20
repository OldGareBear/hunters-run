from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import Conversation, User
from app.permissions import Perm, has_perm
from app.security.auth import require_perm, require_user
from app.services import chat as chat_service
from app.services import chat_limits, escalation
from app.web import kick_outbox, redirect, render

router = APIRouter()


def _own_conversation(db: Session, user: User, conv_id: int) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404)
    return conv


def _recent(db: Session, user: User) -> list[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
            .limit(30)
        )
    )


@router.get("/")
def home(user: User = Depends(require_user)):
    return redirect("/chat")


@router.get("/chat")
def chat_index(
    request: Request, user: User = Depends(require_perm(Perm.CHAT)), db: Session = Depends(get_db)
):
    return render(
        request, "chat/chat.html", conversations=_recent(db, user), conv=None, **_limits(db, user)
    )


def _limits(db: Session, user: User) -> dict:
    st = chat_limits.status(db, user)
    return {"limits": st, "show_spend": has_perm(user, Perm.USERS_MANAGE)}


@router.post("/chat")
def start_chat(
    request: Request,
    question: str = Form(...),
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    if not question.strip():
        return redirect("/chat", "Type a question first.", "error")
    reason = chat_limits.status(db, user).blocked_reason
    if reason:
        return redirect("/chat", reason, "error")
    conv = Conversation(user_id=user.id)
    db.add(conv)
    db.flush()
    try:
        chat_service.ask(db, user, conv, question)
    except chat_service.LimitReached as exc:  # e.g. another tab used the last question
        db.rollback()
        return redirect("/chat", str(exc), "error")
    return redirect(f"/chat/{conv.id}#latest")


@router.get("/chat/{conv_id}")
def show_chat(
    request: Request,
    conv_id: int,
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    return render(
        request, "chat/chat.html", conversations=_recent(db, user), conv=conv, **_limits(db, user)
    )


@router.post("/chat/{conv_id}/ask")
def ask(
    conv_id: int,
    question: str = Form(...),
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    if question.strip():
        try:
            chat_service.ask(db, user, conv, question)
        except chat_service.LimitReached as exc:
            return redirect(f"/chat/{conv.id}#latest", str(exc), "error")
    return redirect(f"/chat/{conv.id}#latest")


@router.post("/chat/{conv_id}/delete")
def delete_chat(conv_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    conv = _own_conversation(db, user, conv_id)
    db.delete(conv)
    db.commit()
    return redirect("/chat", "Conversation deleted.")


# --- Asking management --------------------------------------------------------
# One email to management with the asker copied; the portal keeps nothing else.

SENT_MESSAGE = "Sent to management. You're copied on the email, and their reply will come to you."


def _escalation_form(request: Request, conv: Conversation | None, status: int = 200, **ctx):
    questions = escalation.conversation_questions(conv) if conv else []
    return render(request, "escalation/form.html", status, conv=conv, questions=questions, **ctx)


def _send(
    request: Request,
    background: BackgroundTasks,
    db: Session,
    user: User,
    conv: Conversation | None,
    subject: str,
    details: str,
):
    try:
        escalation.escalate(db, user=user, subject=subject, details=details, conversation=conv)
    except escalation.EscalationError as exc:
        return _escalation_form(
            request, conv, 400, subject=subject, details=details, error=str(exc)
        )
    audit(
        db,
        escalation.AUDIT_ACTION,
        actor=user,
        target_type="conversation" if conv else "",
        target_id=conv.id if conv else "",
        request=request,
    )
    db.commit()
    kick_outbox(background)
    return redirect(f"/chat/{conv.id}" if conv else "/chat", SENT_MESSAGE)


@router.get("/ask-management")
def ask_management_form(request: Request, user: User = Depends(require_perm(Perm.ASK_MANAGEMENT))):
    return _escalation_form(request, None)


@router.post("/ask-management")
def ask_management(
    request: Request,
    background: BackgroundTasks,
    subject: str = Form(...),
    details: str = Form(""),
    user: User = Depends(require_perm(Perm.ASK_MANAGEMENT)),
    db: Session = Depends(get_db),
):
    return _send(request, background, db, user, None, subject, details)


@router.get("/chat/{conv_id}/escalate")
def escalate_form(
    request: Request,
    conv_id: int,
    user: User = Depends(require_perm(Perm.ASK_MANAGEMENT)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    last_q = next((m.content for m in reversed(conv.messages) if m.role == "user"), "")
    return _escalation_form(request, conv, subject=chat_service.title_from(last_q))


@router.post("/chat/{conv_id}/escalate")
def escalate(
    request: Request,
    background: BackgroundTasks,
    conv_id: int,
    subject: str = Form(...),
    details: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not has_perm(user, Perm.ASK_MANAGEMENT):
        raise HTTPException(403)
    conv = _own_conversation(db, user, conv_id)
    return _send(request, background, db, user, conv, subject, details)
