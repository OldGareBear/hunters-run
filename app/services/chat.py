"""The chatbot flow: retrieve permitted sources, ask the model, store the turn."""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import ChatMessage, Conversation, User
from app.rag.llm import NO_SOURCES_ANSWER, AnswerTookTooLong, get_llm
from app.rag.retrieval import Source, retrieve
from app.services import chat_limits

log = logging.getLogger(__name__)

ERROR_ANSWER = (
    "Sorry, I couldn't reach the assistant just now. Please try again in a minute, "
    "or escalate your question to management."
)
_MARK = re.compile(r"\[\[src:(\d+)\]\]")


def _history(conv: Conversation, turns: int) -> list[dict]:
    """Prior turns as plain text (no document blocks or citations are replayed)."""
    msgs = [m for m in conv.messages if m.role in ("user", "assistant")][-turns * 2 :]
    while msgs and msgs[0].role != "user":
        msgs = msgs[1:]
    out: list[dict] = []
    for m in msgs:
        text = re.sub(r"\[\d+\]", "", m.content).strip() or "(no answer)"
        if out and out[-1]["role"] == m.role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": m.role, "content": text})
    if out and out[-1]["role"] == "user":  # e.g. a turn whose answer failed to save
        out.pop()
    return out


def _render(answer_text: str, sources: list[Source], cited_quotes: dict[int, str]):
    """Replace [[src:i]] markers with [n], numbering sources in first-cited order."""
    numbering: dict[int, int] = {}

    def sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx >= len(sources):
            return ""
        n = numbering.setdefault(idx, len(numbering) + 1)
        return f"[{n}]"

    text = _MARK.sub(sub, answer_text)
    text = re.sub(r"(\[\d+\])(\1)+", r"\1", text)  # collapse repeats like [1][1]
    citations = [
        {
            "n": n,
            "document_id": sources[idx].document_id,
            "title": sources[idx].title,
            "page": sources[idx].page,
            "quote": (cited_quotes.get(idx) or "")[:500],
        }
        for idx, n in sorted(numbering.items(), key=lambda kv: kv[1])
    ]
    return text, citations


def title_from(question: str) -> str:
    q = " ".join(question.split())
    return (q[:77] + "...") if len(q) > 80 else q


class LimitReached(Exception):
    pass


def ask(db: Session, user: User, conv: Conversation, question: str) -> ChatMessage:
    """Answer a question. Raises LimitReached (saving nothing) if a usage limit is hit."""
    s = get_settings()
    reason = chat_limits.status(db, user).blocked_reason
    if reason:
        raise LimitReached(reason)
    question = question.strip()[:4000]
    history = _history(conv, s.rag_history_turns)
    db.add(ChatMessage(conversation_id=conv.id, role="user", content=question))
    if conv.title == "New conversation":
        conv.title = title_from(question)
    conv.updated_at = utcnow()

    sources = retrieve(db, user, question)
    if not sources and history:
        # Short follow-ups ("and guests?") rarely match on their own; search again with
        # the previous question for context. Still filtered by the user's role.
        previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        if previous:
            sources = retrieve(db, user, f"{previous}\n{question}")
    if not sources:
        reply = ChatMessage(
            conversation_id=conv.id,
            role="assistant",
            content=NO_SOURCES_ANSWER,
            answered=False,
        )
        db.add(reply)
        chat_limits.record(db, user)
        db.commit()
        return reply

    try:
        result = get_llm().answer(question=question, history=history, sources=sources)
    except AnswerTookTooLong as slow:
        # Billed even though nobody saw it, so it counts like an answer: otherwise slow
        # answers could be repeated without limit past the budget.
        reply = ChatMessage(
            conversation_id=conv.id, role="assistant", content=ERROR_ANSWER, answered=False
        )
        db.add(reply)
        chat_limits.record(
            db,
            user,
            model=slow.model,
            input_tokens=slow.input_tokens,
            output_tokens=slow.output_tokens,
        )
        db.commit()
        return reply
    except Exception:
        log.exception("LLM call failed")
        reply = ChatMessage(
            conversation_id=conv.id, role="assistant", content=ERROR_ANSWER, answered=False
        )
        db.add(reply)
        db.commit()  # not counted: an outage shouldn't use up people's questions
        return reply

    quotes: dict[int, str] = {}
    for c in result.citations:
        quotes.setdefault(c.source_index, c.cited_text)
    text, citations = _render(result.text, sources, quotes)
    reply = ChatMessage(
        conversation_id=conv.id,
        role="assistant",
        content=text,
        citations=citations,
        answered=bool(citations) and not result.refused,
        model=result.model,
    )
    db.add(reply)
    chat_limits.record(
        db,
        user,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    db.commit()
    return reply
