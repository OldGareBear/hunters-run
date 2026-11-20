from sqlalchemy import select

from app.config import get_settings
from app.models import Ticket
from app.permissions import Role
from tests import test_chat

docs = test_chat.docs  # fixtures: two documents (one board-only) and a fake model
llm = test_chat.llm


def _conversation(client):
    resp = client.post_form("/chat", {"question": "Can I install a hot tub?"})
    return int(resp.headers["location"].split("/")[2].split("#")[0])


def test_escalate_conversation_notifies_management(client_for, make_user, outbox, db):
    make_user(Role.MANAGEMENT, "manager@example.com")
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    conv_id = _conversation(owner)
    form = owner.get(f"/chat/{conv_id}/escalate")
    assert "Can I install a hot tub?" in form.text  # subject prefilled

    resp = owner.post_form(
        f"/chat/{conv_id}/escalate", {"subject": "Hot tub", "details": "Rear patio, 6x6 ft."}
    )
    assert resp.status_code == 303
    ticket = db.scalar(select(Ticket))
    assert ticket.conversation_id == conv_id and ticket.status == "open"
    to = sorted(m.to for m in outbox)
    assert to == ["manager@example.com", "office@management.example"]
    assert "Rear patio" in outbox[0].text
    assert "- Can I install a hot tub?" in outbox[0].text


def test_management_reply_notifies_homeowner(client_for, outbox, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    owner.post_form("/tickets", {"subject": "Streetlight out", "details": "By lot 12"})
    ticket = db.scalar(select(Ticket))
    outbox.clear()

    page = mgr.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200 and "By lot 12" in page.text
    mgr.post_form(f"/tickets/{ticket.id}/reply", {"body": "Vendor scheduled for Friday."})
    assert [m.to for m in outbox] == ["owner@example.com"]
    db.expire_all()
    assert db.get(Ticket, ticket.id).status == "in_progress"

    # Internal notes are hidden from the homeowner and send no email.
    outbox.clear()
    mgr.post_form(f"/tickets/{ticket.id}/reply", {"body": "Vendor is Acme.", "internal": "1"})
    assert not outbox
    assert "Vendor is Acme." not in owner.get(f"/tickets/{ticket.id}").text
    assert "Vendor is Acme." in mgr.get(f"/tickets/{ticket.id}").text


def test_homeowners_only_see_their_own_tickets(client_for, db):
    a = client_for(Role.HOMEOWNER, "a@example.com")
    b = client_for(Role.HOMEOWNER, "b@example.com")
    a.post_form("/tickets", {"subject": "Private matter", "details": ""})
    ticket = db.scalar(select(Ticket))
    assert b.get(f"/tickets/{ticket.id}").status_code == 404
    assert "Private matter" not in b.get("/tickets").text
    assert b.post_form(f"/tickets/{ticket.id}/reply", {"body": "hi"}).status_code == 404


def test_cannot_escalate_someone_elses_conversation(client_for, db):
    a = client_for(Role.HOMEOWNER, "a@example.com")
    b = client_for(Role.HOMEOWNER, "b@example.com")
    conv_id = _conversation(a)
    resp = b.post_form(f"/chat/{conv_id}/escalate", {"subject": "x"})
    assert resp.status_code == 404
    assert db.scalar(select(Ticket)) is None


def test_homeowner_cannot_change_status_or_add_internal_note(client_for, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    owner.post_form("/tickets", {"subject": "Gate code", "details": ""})
    ticket = db.scalar(select(Ticket))
    assert owner.post_form(f"/tickets/{ticket.id}/status", {"status": "closed"}).status_code == 403
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    data = {"status": "open", "assignee_id": "me"}
    assert mgr.post_form(f"/tickets/{ticket.id}/status", data).status_code == 400
    resp = owner.post_form(f"/tickets/{ticket.id}/reply", {"body": "x", "internal": "1"})
    assert resp.status_code == 303
    assert "Only management" in owner.get(resp.headers["location"]).text


def test_board_ticket_visibility_is_off_by_default(client_for, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    board = client_for(Role.BOARD, "board@example.com")
    owner.post_form("/tickets", {"subject": "Neighbor dispute", "details": ""})
    ticket = db.scalar(select(Ticket))
    assert board.get(f"/tickets/{ticket.id}").status_code == 404


def _ask(client, question, conv_id=None):
    url = f"/chat/{conv_id}/ask" if conv_id else "/chat"
    resp = client.post_form(url, {"question": question})
    return int(resp.headers["location"].split("/")[2].split("#")[0])


def test_escalation_copies_only_the_questions(client_for, docs, llm, db):
    board = client_for(Role.BOARD, "board@example.com")
    conv_id = _ask(board, "What did the board decide about the Lot 7 attorney?")
    assert "Executive minutes" in board.get(f"/chat/{conv_id}").text  # the answer cites it

    form = board.get(f"/chat/{conv_id}/escalate").text
    assert "What did the board decide about the Lot 7 attorney?" in form  # what will be sent
    resp = board.post_form(f"/chat/{conv_id}/escalate", {"subject": "Lot 7", "details": "fyi"})
    ticket_url = resp.headers["location"]

    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    page = mgr.get(ticket_url).text
    assert "What did the board decide about the Lot 7 attorney?" in page
    assert "Here you go" not in page  # the assistant's answer
    assert "Executive minutes" not in page  # the board-only source
    assert f"/chat/{conv_id}" not in page
    assert f"/chat/{conv_id}" in board.get(ticket_url).text  # the asker can go back to it

    # Later questions in the same conversation stay out of the ticket.
    _ask(board, "And the landscaping bids?", conv_id)
    assert "landscaping" not in mgr.get(ticket_url).text
    # Deleting the conversation keeps the ticket's copy.
    board.post_form(f"/chat/{conv_id}/delete")
    assert "Lot 7 attorney?" in mgr.get(ticket_url).text


def test_admin_escalation_shows_board_no_management_content(client_for, docs, llm, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "board_can_view_tickets", True)
    admin = client_for(Role.ADMIN, "admin2@example.com")
    conv_id = _ask(admin, "What did the board vote about Lot 7 attorney?")
    resp = admin.post_form(f"/chat/{conv_id}/escalate", {"subject": "Lot 7"})
    board = client_for(Role.BOARD, "board@example.com")
    page = board.get(resp.headers["location"]).text
    assert "Lot 7 attorney?" in page
    assert "Here you go" not in page and "Pool rules" not in page
