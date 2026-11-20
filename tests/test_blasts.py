import threading

import pytest
from sqlalchemy import select

from app import db as app_db
from app.models import AuditLog, EmailBlast, EmailMessage, User
from app.permissions import Role
from app.services import blasts as blast_service


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


def test_bad_confirm_count_is_a_page_not_a_422(client_for, make_user, outbox):
    board = client_for(Role.BOARD)
    make_user(Role.HOMEOWNER, "owner0@example.com")
    bid = _draft(board)
    resp = board.post_form(f"/blasts/{bid}/send", {"confirm_count": "one"})
    assert resp.status_code == 303 and not outbox


def test_concurrent_sends_queue_once(client_for, make_user, db):
    if app_db.get_engine().dialect.name != "postgresql":
        pytest.skip("needs Postgres row locks")
    for i in range(3):
        make_user(Role.HOMEOWNER, f"owner{i}@example.com")
    board = client_for(Role.BOARD, "board@example.com")
    bid = _draft(board)
    barrier = threading.Barrier(2)
    errors = []

    def send():
        s = app_db.session_factory()()
        try:
            blast = s.get(EmailBlast, bid)
            sender = s.scalar(select(User).where(User.email == "board@example.com"))
            _ = blast.status
            barrier.wait()
            blast_service.queue_blast(s, blast, sender)
            s.commit()
        except blast_service.BlastError as exc:
            errors.append(exc)
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    n = len(db.scalars(select(EmailMessage).where(EmailMessage.kind == "blast")).all())
    assert n == 3
    assert len(errors) == 1
