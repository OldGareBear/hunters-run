from sqlalchemy import select

from app.models import AuditLog, EmailBlast, EmailMessage
from app.permissions import Role


def _draft(client, roles=("homeowner",), official=False, subject="Pool opens Saturday"):
    data = {
        "subject": subject,
        "body": "Hello neighbors,\n\nThe pool opens Saturday.",
        "roles": list(roles),
    }
    if official:
        data["official"] = "1"
    resp = client.post_form("/blasts", data)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_only_board_and_admin_can_send(client_for):
    for role in (Role.HOMEOWNER, Role.MANAGEMENT):
        c = client_for(role)
        assert c.get("/blasts").status_code == 403
        assert (
            c.post_form(
                "/blasts", {"subject": "x", "body": "y", "roles": ["homeowner"]}
            ).status_code
            == 403
        )


def test_preview_then_send(client_for, make_user, outbox, db):
    board = client_for(Role.BOARD)
    for i in range(3):
        make_user(Role.HOMEOWNER, f"owner{i}@example.com")
    make_user(Role.HOMEOWNER, "inactive@example.com", active=False)
    opted_out = make_user(Role.HOMEOWNER, "quiet@example.com")
    opted_out.email_opt_out_announcements = True
    db.commit()

    bid = _draft(board)
    preview = board.get(f"/blasts/{bid}")
    assert "Send to 3 people" in preview.text
    assert not outbox  # nothing sent at draft time

    resp = board.post_form(f"/blasts/{bid}/send", {"confirm_count": "3"})
    assert resp.status_code == 303
    sent_to = sorted(m.to for m in outbox)
    assert sent_to == ["owner0@example.com", "owner1@example.com", "owner2@example.com"]
    assert all(m.kind == "broadcast" for m in outbox)
    assert all("List-Unsubscribe" in m.headers for m in outbox)
    db.expire_all()
    blast = db.get(EmailBlast, bid)
    assert blast.status == "queued" and blast.recipient_count == 3
    assert (
        db.scalar(select(AuditLog).where(AuditLog.action == "blast.sent")).details["recipients"]
        == 3
    )

    # Sending twice is refused (no duplicate emails).
    board.post_form(f"/blasts/{bid}/send", {"confirm_count": "3"})
    assert len(db.scalars(select(EmailMessage).where(EmailMessage.blast_id == bid)).all()) == 3


def test_official_notice_ignores_opt_out(client_for, make_user, outbox, db):
    board = client_for(Role.BOARD)
    quiet = make_user(Role.HOMEOWNER, "quiet@example.com")
    quiet.email_opt_out_announcements = True
    db.commit()
    bid = _draft(board, official=True, subject="Annual meeting notice")
    board.post_form(f"/blasts/{bid}/send", {"confirm_count": "1"})
    assert [m.to for m in outbox] == ["quiet@example.com"]


def test_audience_change_requires_new_preview(client_for, make_user, outbox):
    board = client_for(Role.BOARD)
    make_user(Role.HOMEOWNER, "owner0@example.com")
    bid = _draft(board)
    make_user(Role.HOMEOWNER, "late@example.com")
    resp = board.post_form(f"/blasts/{bid}/send", {"confirm_count": "1"})
    assert resp.status_code == 303
    assert not outbox


def test_body_is_escaped_in_html(client_for, make_user, outbox):
    board = client_for(Role.BOARD)
    make_user(Role.HOMEOWNER, "owner0@example.com")
    resp = board.post_form(
        "/blasts", {"subject": "Hi", "body": "<script>alert(1)</script>", "roles": ["homeowner"]}
    )
    bid = int(resp.headers["location"].rsplit("/", 1)[1])
    board.post_form(f"/blasts/{bid}/send", {"confirm_count": "1"})
    assert "<script>" not in outbox[0].html
    assert "&lt;script&gt;" in outbox[0].html
