from sqlalchemy import select

from app.models import Ticket
from app.permissions import Role


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
    resp = owner.post_form(f"/tickets/{ticket.id}/reply", {"body": "x", "internal": "1"})
    assert resp.status_code == 303
    assert "Only management" in owner.get(resp.headers["location"]).text


def test_board_ticket_visibility_is_off_by_default(client_for, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    board = client_for(Role.BOARD, "board@example.com")
    owner.post_form("/tickets", {"subject": "Neighbor dispute", "details": ""})
    ticket = db.scalar(select(Ticket))
    assert board.get(f"/tickets/{ticket.id}").status_code == 404
