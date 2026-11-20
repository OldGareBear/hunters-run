"""The chatbot must only ever see documents the asking user may read."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import ChatMessage, ChatUsage, Conversation, User
from app.permissions import Role
from app.rag.llm import Answer, Citation
from app.rag.retrieval import retrieve
from app.services import chat as chat_service
from app.services import documents as doc_service

BOARD_SECRET = (
    "Executive session. The board voted to refer the Lot 7 delinquent account to the "
    "association attorney for collection. Landscaping bids are capped at 84000 dollars."
)
POOL = "Pool rules. The pool is open from 10 AM to 8 PM daily. Guests must be accompanied."


class RecordingLLM:
    def __init__(self):
        self.calls = []

    def answer(self, *, question, history, sources):
        self.calls.append({"question": question, "history": history, "sources": sources})
        return Answer("Here you go.[[src:0]]", [Citation(0, sources[0].text[:40])], model="fake")


@pytest.fixture
def llm(monkeypatch):
    fake = RecordingLLM()
    monkeypatch.setattr(chat_service, "get_llm", lambda: fake)
    return fake


@pytest.fixture
def docs(db, make_user):
    admin = make_user(Role.ADMIN, "admin@example.com")
    for title, text, roles in [
        ("Executive minutes", BOARD_SECRET, ["board"]),
        ("Pool rules", POOL, ["board", "management", "homeowner"]),
    ]:
        doc_service.create_document(
            db,
            uploader=admin,
            title=title,
            description="",
            filename=f"{title}.txt",
            data=text.encode(),
            declared_type="text/plain",
            roles=roles,
        )
    db.commit()


def _user(db, role):
    return db.scalar(select(User).where(User.role == role.value))


def test_retrieval_filters_by_role(db, docs, make_user):
    owner = make_user(Role.HOMEOWNER)
    board = make_user(Role.BOARD)
    q = "What did the board decide about the Lot 7 delinquent account and attorney?"
    owner_titles = {s.title for s in retrieve(db, owner, q, min_score=-1)}
    board_titles = {s.title for s in retrieve(db, board, q, min_score=-1)}
    assert "Executive minutes" not in owner_titles
    assert "Executive minutes" in board_titles


def test_retrieval_excludes_deleted_and_inactive_docs(db, docs, make_user):
    board = make_user(Role.BOARD)
    from app.models import Document

    doc = db.scalar(select(Document).where(Document.title == "Executive minutes"))
    doc_service.soft_delete(db, doc)
    db.commit()
    titles = {s.title for s in retrieve(db, board, "Lot 7 attorney", min_score=-1)}
    assert "Executive minutes" not in titles


def test_homeowner_question_never_sends_board_text_to_model(client_for, docs, llm, db):
    owner = client_for(Role.HOMEOWNER)
    resp = owner.post_form("/chat", {"question": "What did the board vote about Lot 7?"})
    assert resp.status_code == 303
    for call in llm.calls:
        for src in call["sources"]:
            assert "Lot 7" not in src.text
            assert src.title != "Executive minutes"


def test_board_gets_cited_answer(client_for, docs, llm, db):
    board = client_for(Role.BOARD)
    resp = board.post_form("/chat", {"question": "What did the board vote about Lot 7 attorney?"})
    page = board.get(resp.headers["location"].split("#")[0])
    assert page.status_code == 200
    assert "Here you go.[1]" in page.text
    msg = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert msg.answered
    assert msg.citations[0]["n"] == 1
    assert msg.citations[0]["title"] in {"Executive minutes", "Pool rules"}


def test_no_sources_offers_escalation_without_calling_model(client_for, llm, db, make_user):
    owner = client_for(Role.HOMEOWNER)
    resp = owner.post_form("/chat", {"question": "Can I keep chickens?"})
    page = owner.get(resp.headers["location"].split("#")[0])
    assert "Send this question to management" in page.text
    assert llm.calls == []
    msg = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert msg.answered is False


def test_follow_up_sends_history_as_plain_text(client_for, docs, llm, db):
    owner = client_for(Role.HOMEOWNER)
    resp = owner.post_form("/chat", {"question": "When is the pool open?"})
    conv_id = int(resp.headers["location"].split("/")[2].split("#")[0])
    owner.post_form(f"/chat/{conv_id}/ask", {"question": "And can guests come?"})
    history = llm.calls[-1]["history"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert all(isinstance(m["content"], str) for m in history)
    assert "[1]" not in history[1]["content"]


def test_users_cannot_read_each_others_conversations(client_for, docs, llm, db):
    a = client_for(Role.HOMEOWNER, "a@example.com")
    b = client_for(Role.HOMEOWNER, "b@example.com")
    resp = a.post_form("/chat", {"question": "When is the pool open?"})
    conv_path = resp.headers["location"].split("#")[0]
    assert b.get(conv_path).status_code == 404
    assert b.post_form(conv_path + "/ask", {"question": "hi"}).status_code == 404
    assert db.scalar(select(Conversation)).user_id == a.user.id


def test_llm_failure_is_handled(client_for, docs, monkeypatch, db):
    class Boom:
        def answer(self, **_):
            raise RuntimeError("API down")

    monkeypatch.setattr(chat_service, "get_llm", lambda: Boom())
    owner = client_for(Role.HOMEOWNER)
    resp = owner.post_form("/chat", {"question": "When is the pool open?"})
    page = owner.get(resp.headers["location"].split("#")[0])
    assert "Hunter couldn" in page.text and "answer just now" in page.text
    assert db.scalar(select(func.count(ChatUsage.id))) == 0  # doesn't use up a question


def test_answers_abandoned_at_the_deadline_still_count(client_for, docs, monkeypatch, db):
    from app.config import get_settings
    from app.rag.llm import AnswerTookTooLong

    class Slow:
        def answer(self, **_):
            raise AnswerTookTooLong(model="m", input_tokens=6000, output_tokens=1000)

    monkeypatch.setattr(chat_service, "get_llm", lambda: Slow())
    monkeypatch.setattr(get_settings(), "chat_daily_limit_per_user", 2)
    owner = client_for(Role.HOMEOWNER)
    for _ in range(3):
        owner.post_form("/chat", {"question": "When is the pool open?"})
    rows = db.scalars(select(ChatUsage)).all()
    assert len(rows) == 2 and rows[0].output_tokens == 1000 and rows[0].cost_micros > 0
    page = owner.get("/chat")
    assert "daily limit" in page.text


def test_offline_llm_end_to_end(client_for, docs, db):
    owner = client_for(Role.HOMEOWNER)
    resp = owner.post_form("/chat", {"question": "What hours is the pool open?"})
    page = owner.get(resp.headers["location"].split("#")[0])
    assert "10 AM to 8 PM" in page.text
    assert "/documents/" in page.text  # citation links to the source
