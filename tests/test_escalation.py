"""Asking management: one email, the asker copied, nothing kept in the portal."""

from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, EmailMessage
from app.permissions import Role
from tests import test_chat

docs = test_chat.docs  # fixtures: two documents (one board-only) and a fake model
llm = test_chat.llm


def _ask(client, question, conv_id=None):
    url = f"/chat/{conv_id}/ask" if conv_id else "/chat"
    resp = client.post_form(url, {"question": question})
    return int(resp.headers["location"].split("/")[2].split("#")[0])


def test_one_email_to_management_with_the_asker_copied(client_for, make_user, outbox, db):
    make_user(Role.MANAGEMENT, "manager@example.com")
    make_user(Role.MANAGEMENT, "gone@example.com", active=False)
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    conv_id = _ask(owner, "Can I install a hot tub?")
    form = owner.get(f"/chat/{conv_id}/escalate")
    assert "Can I install a hot tub?" in form.text  # subject prefilled

    resp = owner.post_form(
        f"/chat/{conv_id}/escalate", {"subject": "Hot tub", "details": "Rear patio, 6x6 ft."}
    )
    assert resp.status_code == 303 and resp.headers["location"] == f"/chat/{conv_id}"
    assert len(outbox) == 1
    mail = outbox[0]
    assert mail.to == "manager@example.com, office@management.example"
    assert mail.cc == "owner@example.com" and mail.reply_to == "owner@example.com"
    assert mail.subject.startswith("Hot tub (from ")
    assert "Rear patio" in mail.text
    assert "- Can I install a hot tub?" in mail.text
    assert "/tickets" not in mail.text and "/tickets" not in mail.html

    # Nothing is kept: the outbox forgets the body once it's sent, and the audit
    # entry says only that a message went.
    db.expire_all()
    row = db.scalar(select(EmailMessage).where(EmailMessage.kind == "escalation"))
    assert row.status == "sent" and row.text_body == "" and row.html_body == ""
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "escalation.sent"))
    assert entry.target_id == str(conv_id)
    assert "Hot tub" not in str(entry.details)


def test_ask_management_without_a_conversation(client_for, outbox):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    assert owner.get("/ask-management").status_code == 200
    resp = owner.post_form("/ask-management", {"subject": "Streetlight out", "details": "By 12"})
    assert resp.status_code == 303 and resp.headers["location"] == "/chat"
    assert "copied on the email" in owner.get("/chat").text
    assert outbox[0].to == "office@management.example"
    assert "Questions they asked" not in outbox[0].text


def test_subject_is_required(client_for, outbox):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    resp = owner.post_form("/ask-management", {"subject": "   ", "details": "x"})
    assert resp.status_code == 400 and "Add a short subject" in resp.text
    assert not outbox


def test_daily_limit(client_for, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "escalations_per_day", 2)
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    for i in range(2):
        assert owner.post_form("/ask-management", {"subject": f"q{i}"}).status_code == 303
    resp = owner.post_form("/ask-management", {"subject": "q3"})
    assert resp.status_code == 400 and "in the last day" in resp.text
    assert len(outbox) == 2
    # The limit is per person.
    other = client_for(Role.HOMEOWNER, "other@example.com")
    assert other.post_form("/ask-management", {"subject": "q"}).status_code == 303


def test_no_management_address_is_an_error_not_a_lost_message(client_for, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "management_notify_emails", "")
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    resp = owner.post_form("/ask-management", {"subject": "Gate code"})
    assert resp.status_code == 400 and "an address for management" in resp.text
    assert not outbox


def test_cannot_escalate_someone_elses_conversation(client_for, outbox):
    a = client_for(Role.HOMEOWNER, "a@example.com")
    b = client_for(Role.HOMEOWNER, "b@example.com")
    conv_id = _ask(a, "Can I install a hot tub?")
    assert b.get(f"/chat/{conv_id}/escalate").status_code == 404
    assert b.post_form(f"/chat/{conv_id}/escalate", {"subject": "x"}).status_code == 404
    assert not outbox


def test_management_cannot_send_to_itself(client_for):
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    assert mgr.get("/ask-management").status_code == 403
    assert mgr.post_form("/ask-management", {"subject": "x"}).status_code == 403


def test_email_copies_only_the_questions(client_for, docs, llm, outbox):
    board = client_for(Role.BOARD, "board@example.com")
    conv_id = _ask(board, "What did the board decide about the Lot 7 attorney?")
    assert "Executive minutes" in board.get(f"/chat/{conv_id}").text  # the answer cites it
    _ask(board, "And the landscaping bids?", conv_id)

    form = board.get(f"/chat/{conv_id}/escalate").text
    assert "What did the board decide about the Lot 7 attorney?" in form  # what will be sent
    board.post_form(f"/chat/{conv_id}/escalate", {"subject": "Lot 7", "details": "fyi"})
    body = outbox[-1].text + outbox[-1].html
    assert "Lot 7 attorney?" in body and "landscaping bids?" in body
    assert "Here you go" not in body  # the assistant's answer
    assert "Executive minutes" not in body  # the board-only source
    assert f"/chat/{conv_id}" not in body
