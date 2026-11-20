"""The weekly review: next best actions from the calendar and recent documents."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.email.outbox import deliver_pending
from app.models import (
    EMBEDDING_DIM,
    AuditLog,
    ChatUsage,
    Document,
    DocumentAccess,
    DocumentChunk,
    Duty,
    EmailMessage,
    WeeklyReview,
)
from app.permissions import Role
from app.services import weekly_review as wr
from app.services.reservations import tz

MONDAY = datetime(2027, 3, 1, 9, tzinfo=tz())  # a Monday
TODAY = MONDAY.date()


def _duty(db, title, due, *, hard=False, owners=("board",), repeat="none", details=""):
    duty = Duty(
        title=title,
        details=details,
        owners=list(owners),
        first_due=due,
        repeat=repeat,
        hard_deadline=hard,
    )
    db.add(duty)
    db.commit()
    return duty


def _doc(db, title, text, roles, *, days_ago=1, path=""):
    when = MONDAY.astimezone(UTC) - timedelta(days=days_ago)
    doc = Document(
        title=title,
        filename=f"{title}.pdf",
        content_type="application/pdf",
        size_bytes=len(text),
        sha256="0" * 64,
        storage_key=f"k/{title}",
        status="ready",
        source_path=path,
        updated_at=when,
        access=[DocumentAccess(role=r.value) for r in roles],
    )
    doc.chunks = [DocumentChunk(ordinal=0, page=1, text=text, embedding=[0.0] * EMBEDDING_DIM)]
    db.add(doc)
    db.commit()
    doc.updated_at = when  # onupdate would otherwise move it to now
    db.commit()
    return doc


def _mails(db) -> list[EmailMessage]:
    return list(
        db.scalars(
            select(EmailMessage).where(EmailMessage.kind == "weekly").order_by(EmailMessage.id)
        )
    )


class FakeReviewer:
    """Stands in for the assistant: answers each audience with `answer(audience, prompt)`."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts: dict[str, str] = {}

    def review(self, *, system, prompt):
        audience = "board" if "for its board" in system else "management"
        self.prompts[audience] = prompt
        return wr.ModelReply(
            self.answer(audience, prompt), model="claude-test", input_tokens=1000, output_tokens=200
        )


@pytest.fixture
def reviewer(monkeypatch):
    def install(answer):
        fake = FakeReviewer(answer)
        monkeypatch.setattr(wr, "get_reviewer", lambda: fake)
        return fake

    return install


def _key(prompt: str, text: str) -> str:
    """The id the prompt gives the line that mentions `text`."""
    line = next(ln for ln in prompt.splitlines() if text in ln)
    if line.startswith("<document"):
        return line.split('id="', 1)[1].split('"', 1)[0]
    return line.split(":", 1)[0].lstrip("- ")


def _act(**kw):
    base = {
        "calendar_item": "",
        "documents": [],
        "title": "A thing",
        "action": "Do the thing.",
        "why_you": "",
        "owners": [],
        "signal": "high",
        "due": "",
    }
    return {**base, **kw}


# --- what's required ----------------------------------------------------------------


def test_required_items_are_hard_deadlines_and_this_weeks_duties(db):
    _duty(db, "Pool permit", TODAY + timedelta(days=19), hard=True)
    _duty(db, "Tree work", TODAY + timedelta(days=12))
    _duty(db, "Gutters", TODAY + timedelta(days=6))
    _duty(db, "Too far", TODAY + timedelta(days=30), hard=True)
    _duty(db, "Today", TODAY)  # last week's review covered it
    items = {c.duty.title: c.required for c in wr.calendar_items(db, TODAY)}
    assert items == {"Gutters": True, "Tree work": False, "Pool permit": True}


def test_minutes_are_recognised_by_title_or_folder():
    assert wr.is_minutes(Document(title="Board meeting minutes, Feb 2027", source_path=""))
    assert wr.is_minutes(Document(title="2027-02-10.pdf", source_path="/Board/Minutes/x.pdf"))
    assert not wr.is_minutes(Document(title="Pool rules", source_path="/Everyone/pool.pdf"))


# --- the plain version (no assistant) -------------------------------------------------


def test_plain_version_sends_required_duties_to_their_usual_owners(db, make_user):
    b1 = make_user(Role.BOARD, "b1@example.com", name="Pat")
    make_user(Role.BOARD, "b2@example.com")
    make_user(Role.MANAGEMENT, "m@example.com")
    make_user(Role.HOMEOWNER, "h@example.com")
    make_user(Role.BOARD, "gone@example.com", active=False)
    _duty(db, "Draft budget", TODAY + timedelta(days=5), owners=["b1@example.com"])
    _duty(
        db,
        "Mgt. Co to input increase",
        TODAY + timedelta(days=14),
        hard=True,
        owners=["management"],
    )
    _duty(db, "Tree work", TODAY + timedelta(days=12))  # not required: plain skips it

    assert wr.run_if_due(db, now=MONDAY) == 3  # Pat, the management user, the notify inbox
    mails = {m.to_email: m for m in _mails(db)}
    assert set(mails) == {"b1@example.com", "m@example.com", "office@management.example"}
    review = db.scalar(select(WeeklyReview))
    assert review.status == "sent" and review.mode == "plain" and review.emails == 3
    assert "offline" in review.note
    pat = next(r for r in review.items if r["email"] == b1.email)
    assert [i["title"] for i in pat["items"]] == ["Draft budget"]
    mgmt = next(r for r in review.items if r["email"] == "m@example.com")
    assert mgmt["items"][0]["hard"] is True

    # One review a week: running again, or later in the week, sends nothing.
    assert wr.run_if_due(db, now=MONDAY + timedelta(hours=3)) == 0
    assert wr.run_if_due(db, now=MONDAY + timedelta(days=2)) == 0
    sent = db.scalars(select(AuditLog).where(AuditLog.action == "weekly_review.sent")).all()
    assert len(sent) == 3


def test_email_bodies_are_not_kept_after_sending(db, make_user):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Draft budget", TODAY + timedelta(days=5))
    wr.run_if_due(db, now=MONDAY)
    deliver_pending(db)
    db.expire_all()
    mail = _mails(db)[0]
    assert mail.status == "sent" and mail.text_body == "" and mail.html_body == ""


def test_nothing_due_means_no_email(db, make_user):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Tree work", TODAY + timedelta(days=12))
    assert wr.run_if_due(db, now=MONDAY) == 0
    assert not _mails(db)
    assert db.scalar(select(WeeklyReview)).status == "sent"  # ran, nothing to say


def test_runs_from_the_set_day_and_hour(db, make_user, monkeypatch):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Gutters", TODAY + timedelta(days=3))
    assert wr.run_if_due(db, now=MONDAY.replace(hour=6)) == 0
    monkeypatch.setattr(get_settings(), "weekly_review_day", "tuesday")
    assert wr.run_if_due(db, now=MONDAY) == 0
    assert wr.run_if_due(db, now=MONDAY + timedelta(days=1)) == 1
    monkeypatch.setattr(get_settings(), "weekly_review_enabled", False)
    assert wr.run_if_due(db, now=MONDAY + timedelta(days=8)) == 0


def test_a_hint_that_matches_no_one_goes_to_the_board(db, make_user):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Old member's job", TODAY + timedelta(days=3), owners=["former@example.org"])
    assert wr.run_if_due(db, now=MONDAY) == 1
    assert _mails(db)[0].to_email == "b1@example.com"


# --- with the assistant ---------------------------------------------------------------


def _setup_documents(db):
    minutes = _doc(
        db,
        "Board meeting minutes, February",
        "Action: Pat to get two quotes for the clubhouse roof by March 10.",
        [Role.BOARD],
        days_ago=30,  # minutes are read from further back
    )
    secret = _doc(db, "Legal advice on the lawsuit", "Settle before trial.", [Role.BOARD])
    mgmt = _doc(
        db,
        "Pool vendor contract",
        "Renewal notice must be given by March 15.",
        [Role.BOARD, Role.MANAGEMENT],
    )
    stale = _doc(db, "Old newsletter", "Nothing new.", [Role.BOARD], days_ago=30)
    return minutes, secret, mgmt, stale


def test_assistant_actions_are_checked_and_kept_to_their_audience(db, make_user, reviewer):
    pat = make_user(Role.BOARD, "pat@example.com", name="Pat")
    make_user(Role.BOARD, "sam@example.com", name="Sam")
    mgr = make_user(Role.MANAGEMENT, "mgr@example.com", name="Morgan")
    make_user(Role.HOMEOWNER, "home@example.com", name="Homer")
    minutes, secret, contract, _ = _setup_documents(db)
    _duty(db, "Gutters", TODAY + timedelta(days=4))
    _duty(db, "Pool permit", TODAY + timedelta(days=18), hard=True, owners=["management"])
    _duty(db, "Tree work", TODAY + timedelta(days=12))

    def answer(audience, prompt):
        if audience == "board":
            pat_key, sam_key = _key(prompt, "Pat ("), _key(prompt, "Sam (")
            return {
                "actions": [
                    _act(
                        documents=[_key(prompt, 'title="Board meeting minutes')],
                        title="Clubhouse roof quotes",
                        action="Get two quotes, see https://evil.example or call 914-555-0100.",
                        why_you="The February minutes assign this to you",
                        owners=[pat_key, "u9999"],
                        due="2027-03-10",
                    ),
                    _act(calendar_item=_key(prompt, "Gutters"), owners=[sam_key]),
                    _act(calendar_item=_key(prompt, "Tree work"), signal="low", owners=[sam_key]),
                    _act(calendar_item="c99", owners=[sam_key]),  # not a real id
                    _act(documents=["d99999"], owners=[sam_key]),  # not a real document
                    _act(owners=[sam_key]),  # rests on nothing
                    _act(
                        documents=[_key(prompt, 'title="Legal advice')],
                        title="Lawsuit",
                        owners=["n0"],  # the management inbox isn't a board choice
                    ),
                ]
            }
        # Management: the pool permit is required; the assistant forgets it.
        return {
            "actions": [
                _act(
                    documents=[_key(prompt, 'title="Pool vendor contract')],
                    title="Pool contract notice",
                    action="Send the renewal notice.",
                    owners=[_key(prompt, "Morgan (")],
                )
            ]
        }

    fake = reviewer(answer)
    assert wr.run_if_due(db, now=MONDAY) == 4

    # Each audience only saw what it may read, and only its own people.
    board_prompt, mgmt_prompt = fake.prompts["board"], fake.prompts["management"]
    assert "Settle before trial" in board_prompt and 'kind="meeting minutes"' in board_prompt
    assert "Settle before trial" not in mgmt_prompt and "clubhouse roof" not in mgmt_prompt
    assert "Renewal notice" in mgmt_prompt
    assert "Nothing new" not in board_prompt  # 30 days old and not minutes
    assert "Homer" not in board_prompt + mgmt_prompt
    assert "Morgan" not in board_prompt and "Pat" not in mgmt_prompt

    mails = {m.to_email: m for m in _mails(db)}
    assert set(mails) == {
        "pat@example.com",
        "sam@example.com",
        "mgr@example.com",
        "office@management.example",
    }
    review = db.scalar(select(WeeklyReview))
    assert review.mode == "model"
    items = {r["email"]: [i["title"] for i in r["items"]] for r in review.items}
    assert items["pat@example.com"] == ["Clubhouse roof quotes"]
    assert items["sam@example.com"] == ["A thing"]  # the gutters
    # The forgotten hard deadline was added for its usual owners, management.
    assert items["mgr@example.com"] == ["Pool permit", "Pool contract notice"]
    assert items["office@management.example"] == ["Pool permit"]
    pat_items = next(r for r in review.items if r["email"] == pat.email)["items"]
    assert "evil" not in pat_items[0]["action"] and "555" not in pat_items[0]["action"]
    assert pat_items[0]["documents"] == [minutes.title]
    assert all(secret.title not in str(r) for r in review.items if r["email"] != "pat@example.com")
    assert mgr.email in mails and contract.title in str(review.items)

    usage = db.scalars(select(ChatUsage)).all()
    assert len(usage) == 2 and all(u.user_id is None for u in usage)


def test_assistant_failure_falls_back_to_the_plain_version(db, make_user, monkeypatch):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Gutters", TODAY + timedelta(days=4))

    class Broken:
        def review(self, **kw):
            raise wr.ReviewFailed("the assistant stopped early (max_tokens)", input_tokens=5)

    monkeypatch.setattr(wr, "get_reviewer", lambda: Broken())
    assert wr.run_if_due(db, now=MONDAY) == 1
    review = db.scalar(select(WeeklyReview))
    assert review.mode == "plain" and "max_tokens" in review.note
    assert db.scalar(select(ChatUsage)).input_tokens == 5  # billed, so counted


def test_over_budget_uses_the_plain_version(db, make_user, reviewer, monkeypatch):
    make_user(Role.BOARD, "b1@example.com")
    _duty(db, "Gutters", TODAY + timedelta(days=4))
    monkeypatch.setattr(get_settings(), "chat_monthly_budget_usd", 0.000001)
    fake = reviewer(lambda audience, prompt: {"actions": []})
    assert wr.run_if_due(db, now=MONDAY) == 1
    assert fake.prompts == {}
    assert "budget" in db.scalar(select(WeeklyReview)).note


def test_validate_drops_bad_dates_and_unknown_people():
    duty = Duty(title="x", owners=["board"], first_due=TODAY, hard_deadline=False)
    item = wr.CalendarItem("c1", duty, TODAY + timedelta(days=3), required=True)
    aud = wr.Audience("board", [wr.Person("u1", "a@example.com", "A", "board")], [])
    data = {
        "actions": [
            _act(calendar_item="c1", owners=["u2"], due="1999-01-01", title="  "),
            _act(calendar_item="c1", owners=["u1"]),  # the same item twice
            "not an action",
        ]
    }
    [a] = wr.validate(data, aud, [item], TODAY)
    assert a.owners == ["u1"]  # unknown owner replaced by the usual owners
    assert a.due == item.due and a.title == "x"
    assert wr.validate({"actions": "nope"}, aud, [item], TODAY) == []


# --- the page and the worker --------------------------------------------------------


def test_admins_can_preview_and_others_cant_see_it(db, client_for, make_user):
    make_user(Role.BOARD, "b1@example.com", name="Pat")
    _duty(db, "Gutters", date.today() + timedelta(days=3))
    admin = client_for(Role.ADMIN, "admin2@example.com")
    assert "No weekly review has run yet" in admin.get("/admin/weekly-review").text
    page = admin.post_form("/admin/weekly-review/preview")
    assert page.status_code == 200 and "Gutters" in page.text and "Plain version" in page.text
    assert not _mails(db)  # a preview sends nothing
    assert db.scalar(select(WeeklyReview)) is None
    assert client_for(Role.BOARD, "b9@example.com").get("/admin/weekly-review").status_code == 403
    assert (
        client_for(Role.MANAGEMENT, "m9@example.com").get("/admin/weekly-review").status_code == 403
    )


def test_worker_runs_the_review_and_survives_a_failure(db, monkeypatch):
    from app import worker

    calls = []
    monkeypatch.setattr(wr, "run_if_due", lambda db: calls.append(1) or 0)
    worker.run_once(force_sync=True)
    assert calls == [1]

    def boom(db):
        raise RuntimeError("bad review")

    monkeypatch.setattr(wr, "run_if_due", boom)
    worker.run_once(force_sync=True)  # email delivery still runs
