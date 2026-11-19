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
from app.services import tickets as ticket_service
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
    return render(request, "chat/chat.html", conversations=_recent(db, user), conv=None)


@router.post("/chat")
def start_chat(
    request: Request,
    question: str = Form(...),
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    if not question.strip():
        return redirect("/chat", "Type a question first.", "error")
    conv = Conversation(user_id=user.id)
    db.add(conv)
    db.flush()
    chat_service.ask(db, user, conv, question)
    return redirect(f"/chat/{conv.id}#latest")


@router.get("/chat/{conv_id}")
def show_chat(
    request: Request,
    conv_id: int,
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    return render(request, "chat/chat.html", conversations=_recent(db, user), conv=conv)


@router.post("/chat/{conv_id}/ask")
def ask(
    conv_id: int,
    question: str = Form(...),
    user: User = Depends(require_perm(Perm.CHAT)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    if question.strip():
        chat_service.ask(db, user, conv, question)
    return redirect(f"/chat/{conv.id}#latest")


@router.post("/chat/{conv_id}/delete")
def delete_chat(conv_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    conv = _own_conversation(db, user, conv_id)
    db.delete(conv)
    db.commit()
    return redirect("/chat", "Conversation deleted.")


@router.get("/chat/{conv_id}/escalate")
def escalate_form(
    request: Request,
    conv_id: int,
    user: User = Depends(require_perm(Perm.TICKETS_CREATE)),
    db: Session = Depends(get_db),
):
    conv = _own_conversation(db, user, conv_id)
    last_q = next((m.content for m in reversed(conv.messages) if m.role == "user"), "")
    return render(
        request,
        "tickets/new.html",
        conv=conv,
        subject=chat_service.title_from(last_q),
    )


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
    if not has_perm(user, Perm.TICKETS_CREATE):
        raise HTTPException(403)
    conv = _own_conversation(db, user, conv_id)
    try:
        ticket = ticket_service.escalate(
            db, user=user, subject=subject, details=details, conversation=conv
        )
    except ticket_service.TicketError as exc:
        return render(
            request,
            "tickets/new.html",
            400,
            conv=conv,
            subject=subject,
            details=details,
            error=str(exc),
        )
    audit(
        db,
        "ticket.created",
        actor=user,
        target_type="ticket",
        target_id=ticket.id,
        request=request,
        from_conversation=conv.id,
    )
    db.commit()
    kick_outbox(background)
    return redirect(
        f"/tickets/{ticket.id}", "Sent to management. You'll get an email when they reply."
    )
