from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import (
    AuditLog,
    EmailBlast,
    EmailMessage,
    Invite,
    PasswordReset,
    User,
    UserSession,
)
from app.permissions import Role
from tests.conftest import PASSWORD, link_from


def test_pages_require_login(client):
    for url in ("/chat", "/documents", "/ask-management", "/admin/users", "/blasts"):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


def test_login_and_logout(client, make_user, db):
    make_user(Role.HOMEOWNER, "owner@example.com")
    client.login("owner@example.com")
    assert client.get("/chat").status_code == 200
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.login"))
    resp = client.post_form("/logout")
    assert resp.status_code == 303
    assert client.get("/chat", follow_redirects=False).status_code == 303


def test_bad_password_and_lockout(client, make_user):
    make_user(Role.HOMEOWNER, "owner@example.com")
    for _ in range(5):
        resp = client.post_form("/login", {"email": "owner@example.com", "password": "wrong"})
        assert resp.status_code == 400
    # Even the right password is refused while locked out.
    resp = client.post_form("/login", {"email": "owner@example.com", "password": PASSWORD})
    assert resp.status_code == 429


def test_unknown_email_gives_same_error(client):
    resp = client.post_form("/login", {"email": "nobody@example.com", "password": "x" * 12})
    assert resp.status_code == 400
    assert "match an active account" in resp.text


def test_post_without_csrf_is_rejected(client, make_user):
    make_user(Role.HOMEOWNER, "owner@example.com")
    client.get("/login")
    resp = client.post(
        "/login", data={"email": "owner@example.com", "password": PASSWORD}, follow_redirects=False
    )
    assert resp.status_code == 403
    resp = client.post(
        "/login",
        data={"email": "owner@example.com", "password": PASSWORD, "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_open_redirect_is_blocked(client, make_user):
    make_user(Role.HOMEOWNER, "owner@example.com")
    resp = client.post_form(
        "/login",
        {"email": "owner@example.com", "password": PASSWORD, "next": "https://evil.example/"},
    )
    assert resp.headers["location"] == "/"
    resp = client.get("/login?next=//evil.example", follow_redirects=False)
    assert "evil" not in resp.text


def test_invite_flow(client_for, client, outbox, db):
    admin = client_for(Role.ADMIN, "admin@example.com")
    resp = admin.post_form("/admin/invites", {"email": "New.Board@Example.com", "role": "board"})
    assert resp.status_code == 303
    invite_mail = [m for m in outbox if m.to == "new.board@example.com"]
    assert len(invite_mail) == 1
    path = link_from(invite_mail[0].text)

    # Only the hash is stored.
    token = path.rsplit("/", 1)[1]
    assert db.scalar(select(Invite)).token_hash != token

    page = client.get(path)
    assert page.status_code == 200 and "Board member" in page.text
    resp = client.post_form(
        path, {"full_name": "New Board", "password": "short", "password_confirm": "short"}
    )
    assert resp.status_code == 400
    resp = client.post_form(
        path, {"full_name": "New Board", "password": PASSWORD, "password_confirm": PASSWORD}
    )
    assert resp.status_code == 303
    user = db.scalar(select(User).where(User.email == "new.board@example.com"))
    assert user.role == "board"
    # Signed in straight away, and the link no longer works.
    assert client.get("/blasts").status_code == 200
    assert client.get(path).status_code == 404


def test_board_can_only_invite_homeowners(client_for):
    board = client_for(Role.BOARD)
    assert (
        board.post_form("/admin/invites", {"email": "x@example.com", "role": "admin"}).status_code
        == 403
    )
    assert (
        board.post_form(
            "/admin/invites", {"email": "x@example.com", "role": "homeowner"}
        ).status_code
        == 303
    )


def test_homeowner_cannot_invite(client_for):
    owner = client_for(Role.HOMEOWNER)
    assert owner.get("/admin/users").status_code == 403
    assert (
        owner.post_form(
            "/admin/invites", {"email": "x@example.com", "role": "homeowner"}
        ).status_code
        == 403
    )


def test_password_reset_flow(client, make_user, outbox, db):
    make_user(Role.HOMEOWNER, "owner@example.com")
    client.post_form("/forgot", {"email": "owner@example.com"})
    client.post_form("/forgot", {"email": "nobody@example.com"})  # no email, same page
    mails = [m for m in outbox if "Reset" in m.subject]
    assert [m.to for m in mails] == ["owner@example.com"]
    path = link_from(mails[0].text)
    new_password = "a brand new passphrase"
    resp = client.post_form(path, {"password": new_password, "password_confirm": new_password})
    assert resp.status_code == 303
    assert client.get(path).status_code == 404  # single use
    client.login("owner@example.com", new_password)


def test_deactivation_ends_sessions(client_for, db):
    admin = client_for(Role.ADMIN, "admin@example.com")
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    assert owner.get("/chat").status_code == 200
    resp = admin.post_form(f"/admin/users/{owner.user.id}/active", {"active": "0"})
    assert resp.status_code == 303
    assert owner.get("/chat", follow_redirects=False).status_code == 303
    assert not db.scalars(select(UserSession).where(UserSession.user_id == owner.user.id)).all()


def test_role_change_takes_effect_immediately(client_for):
    admin = client_for(Role.ADMIN, "admin@example.com")
    member = client_for(Role.BOARD, "member@example.com")
    assert member.get("/blasts").status_code == 200
    admin.post_form(f"/admin/users/{member.user.id}/role", {"role": "homeowner"})
    # Old session was revoked; signing in again gives homeowner access only.
    member.login("member@example.com")
    assert member.get("/blasts").status_code == 403


def test_last_admin_cannot_be_demoted(client_for, make_user, db):
    admin = client_for(Role.ADMIN, "admin@example.com")
    other = make_user(Role.ADMIN, "admin2@example.com")
    admin.post_form(f"/admin/users/{other.id}/active", {"active": "0"})
    # Now admin@ is the only active admin; another admin can't exist to demote them,
    # and admins can't change themselves.
    resp = admin.post_form(f"/admin/users/{admin.user.id}/role", {"role": "board"})
    assert resp.status_code == 400


def test_reset_emails_are_limited_per_hour(client, make_user, outbox, db):
    make_user(Role.HOMEOWNER, "owner@example.com")
    for _ in range(5):
        assert client.post_form("/forgot", {"email": "owner@example.com"}).status_code == 200
    assert len([m for m in outbox if "Reset" in m.subject]) == 3
    for r in db.scalars(select(PasswordReset)):  # an hour later it works again
        r.created_at = r.created_at - timedelta(hours=1, minutes=1)
    db.commit()
    client.post_form("/forgot", {"email": "owner@example.com"})
    assert len([m for m in outbox if "Reset" in m.subject]) == 4


def test_a_completed_reset_ends_a_lockout(client, make_user, outbox):
    make_user(Role.HOMEOWNER, "owner@example.com")
    for _ in range(5):  # someone else guessing at the address
        client.post_form("/login", {"email": "owner@example.com", "password": "wrong"})
    assert (
        client.post_form("/login", {"email": "owner@example.com", "password": PASSWORD}).status_code
        == 429
    )
    client.post_form("/forgot", {"email": "owner@example.com"})
    path = link_from(outbox[-1].text)
    new_password = "a brand new passphrase"
    client.post_form(path, {"password": new_password, "password_confirm": new_password})
    client.login("owner@example.com", new_password)


def test_changing_the_password_ends_reset_links(client_for, outbox):
    c = client_for(Role.HOMEOWNER, "owner@example.com")
    c.post_form("/forgot", {"email": "owner@example.com"})
    path = link_from(outbox[-1].text)
    assert c.get(path).status_code == 200
    new = "another long passphrase"
    resp = c.post_form("/account/password", {"current": PASSWORD, "new": new, "confirm": new})
    assert resp.status_code == 303
    assert c.get(path).status_code == 404


def test_sign_in_links_are_not_kept_in_the_outbox(client, client_for, make_user, outbox, db):
    make_user(Role.HOMEOWNER, "owner@example.com")
    client.post_form("/forgot", {"email": "owner@example.com"})
    admin = client_for(Role.ADMIN, "admin@example.com")
    admin.post_form("/admin/invites", {"email": "new@example.com", "role": "homeowner"})
    assert {m.to for m in outbox} >= {"owner@example.com", "new@example.com"}  # delivered
    stored = db.scalars(select(EmailMessage).where(EmailMessage.kind.in_(("reset", "invite"))))
    stored = list(stored)
    assert {m.kind for m in stored} == {"reset", "invite"}
    assert all(m.status == "sent" and m.text_body == m.html_body == "" for m in stored)


def test_prune_drops_old_delivered_email(db):
    from app.worker import prune

    old = datetime.now(UTC) - timedelta(days=31)
    blast = EmailBlast(subject="s", body="b", audience_roles=["homeowner"], status="queued")
    db.add(blast)
    db.flush()
    for kind, status, blast_id in [
        ("escalation", "sent", None),
        ("escalation", "failed", None),
        ("blast", "sent", blast.id),
    ]:
        db.add(
            EmailMessage(
                to_email="x@example.com",
                subject=kind,
                text_body="x",
                kind=kind,
                status=status,
                blast_id=blast_id,
                created_at=old,
                sent_at=old if status == "sent" else None,
            )
        )
    db.add(
        EmailMessage(
            to_email="y@example.com",
            subject="new",
            text_body="x",
            kind="escalation",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    db.commit()
    prune(db)
    left = sorted((m.subject, m.status) for m in db.scalars(select(EmailMessage)))
    assert left == [("blast", "sent"), ("escalation", "failed"), ("new", "sent")]
