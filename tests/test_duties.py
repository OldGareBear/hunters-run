"""The duty calendar: parsing, recurrence, the pages and the reminder emails."""

import io
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Duty, DutyReminder, EmailMessage
from app.permissions import Role
from app.services import duties
from app.services.reservations import tz


def _duty(db, **kw) -> Duty:
    values = dict(
        title="Review financials",
        details="",
        owners=["board"],
        first_due=date(2027, 1, 15),
        repeat="monthly",
        remind_days=[7, 0],
    )
    values.update(kw)
    duty = Duty(**values)
    db.add(duty)
    db.commit()
    return duty


def _at(y, m, d, hour=9) -> datetime:
    return datetime(y, m, d, hour, tzinfo=tz())


def _queued(db) -> list[EmailMessage]:
    return list(db.scalars(select(EmailMessage).where(EmailMessage.kind == "duty")))


# --- parsing ------------------------------------------------------------------------


def test_parse_fields():
    f = duties.parse_fields(
        title="  File  taxes ",
        details="",
        owners="Treasurer@Example.org; board, management",
        first_due="3/31/2027",
        repeat="Annually",
        remind_days="",
    )
    assert f.title == "File taxes"
    assert f.owners == ["treasurer@example.org", "board", "management"]
    assert f.first_due == date(2027, 3, 31) and f.repeat == "yearly"
    assert f.remind_days == [7, 0]
    assert duties.parse_remind_days("0, 30; 7 7") == [30, 7, 0]


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("owners", "homeowners", "homeowners can't own"),
        ("owners", "bob", "isn't an email address"),
        ("owners", " ; ", "at least one owner"),
        ("first_due", "next week", "isn't a date"),
        ("repeat", "fortnightly", "isn't a repeat"),
        ("remind_days", "400", "from 0 to 365"),
        ("title", "  ", "add a title"),
    ],
)
def test_parse_fields_rejects(field, value, message):
    values = dict(
        title="x", details="", owners="board", first_due="2027-01-01", repeat="", remind_days=""
    )
    values[field] = value
    with pytest.raises(ValueError, match=message):
        duties.parse_fields(**values)


def test_parse_csv_reports_every_bad_row_and_saves_nothing():
    data = (
        b"Title,Owners,First Due,Repeat\n"
        b"Good one,board,2027-01-01,monthly\n"
        b"Bad owner,homeowner,2027-01-01,monthly\n"
        b",,,\n"
        b"Bad date,board,someday,monthly\n"
        b"good ONE,management,2027-02-01,yearly\n"
    )
    with pytest.raises(duties.DutyError) as exc:
        duties.parse_csv(data)
    assert exc.value.errors == [
        "Row 3: homeowners can't own a duty (that would email every homeowner).",
        'Row 5: "someday" isn\'t a date; use 2026-12-31 or 12/31/2026.',
        'Row 6: "good ONE" appears twice.',
    ]


def test_parse_csv_needs_the_columns():
    with pytest.raises(duties.DutyError, match="Missing: owners, first_due"):
        duties.parse_csv(b"title\nx\n")
    with pytest.raises(duties.DutyError, match="Unknown columns: due"):
        duties.parse_csv(b"title,owners,first_due,due\nx,board,2027-01-01,y\n")


def test_template_parses():
    rows = duties.parse_csv(duties.template_csv().encode())
    assert [r.repeat for r in rows] == ["monthly", "yearly", "yearly"]


# --- recurrence ---------------------------------------------------------------------


def _dates(repeat, first, start, end):
    duty = Duty(title="x", owners=["board"], first_due=first, repeat=repeat, remind_days=[0])
    return list(duties.occurrences(duty, start, end))


def test_monthly_keeps_the_day_or_the_months_last_day():
    got = _dates("monthly", date(2027, 1, 31), date(2027, 1, 1), date(2027, 5, 31))
    assert got == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
        date(2027, 4, 30),
        date(2027, 5, 31),
    ]


def test_other_repeats():
    assert _dates("quarterly", date(2026, 3, 15), date(2027, 1, 1), date(2027, 12, 31)) == [
        date(2027, 3, 15),
        date(2027, 6, 15),
        date(2027, 9, 15),
        date(2027, 12, 15),
    ]
    assert _dates("weekly", date(2027, 1, 1), date(2027, 1, 9), date(2027, 1, 22)) == [
        date(2027, 1, 15),
        date(2027, 1, 22),
    ]
    assert _dates("yearly", date(2028, 2, 29), date(2029, 1, 1), date(2032, 12, 31)) == [
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]
    assert _dates("twice a year", date(2027, 1, 10), date(2027, 1, 11), date(2027, 12, 31)) == [
        date(2027, 7, 10)
    ]
    assert _dates("none", date(2027, 1, 10), date(2027, 1, 11), date(2028, 1, 1)) == []


# --- reminders ----------------------------------------------------------------------


def test_reminders_go_out_before_and_on_the_day_once_each(db, make_user):
    make_user(Role.BOARD, "b1@example.com")
    make_user(Role.BOARD, "b2@example.com")
    make_user(Role.BOARD, "gone@example.com", active=False)
    _duty(db, owners=["board", "treasurer@example.org"])

    assert duties.send_reminders(db, now=_at(2027, 1, 7)) == 0  # 8 days out
    assert duties.send_reminders(db, now=_at(2027, 1, 8)) == 1  # 7 days out
    assert duties.send_reminders(db, now=_at(2027, 1, 8, 15)) == 0  # same day, again
    assert duties.send_reminders(db, now=_at(2027, 1, 12)) == 0
    assert duties.send_reminders(db, now=_at(2027, 1, 15)) == 1  # on the day
    assert duties.send_reminders(db, now=_at(2027, 1, 15)) == 0

    mails = _queued(db)
    assert [m.subject for m in mails] == [
        "Reminder: Review financials is due in 7 days",
        "Reminder: Review financials is due today",
    ]
    assert mails[0].to_email == "b1@example.com, b2@example.com, treasurer@example.org"
    assert "Friday, January 15, 2027" in mails[0].text_body
    # Next month's occurrence gets its own reminders.
    assert duties.send_reminders(db, now=_at(2027, 2, 8)) == 1


def test_a_missed_reminder_goes_out_late_once(db):
    _duty(db, owners=["a@example.org"], remind_days=[14, 7, 0])
    assert duties.send_reminders(db, now=_at(2027, 1, 10)) == 1  # the 7-day one, 5 days out
    assert _queued(db)[0].subject.endswith("due in 5 days")
    assert duties.send_reminders(db, now=_at(2027, 1, 11)) == 0
    sent = db.scalars(select(DutyReminder.lead_days)).all()
    assert sent == [7]  # the skipped 14-day reminder isn't sent after the fact


def test_nothing_before_the_reminder_hour(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "duty_reminder_hour", 7)
    _duty(db, owners=["a@example.org"])
    assert duties.send_reminders(db, now=_at(2027, 1, 15, 6)) == 0
    assert duties.send_reminders(db, now=_at(2027, 1, 15, 7)) == 1


def test_a_reminder_already_recorded_is_not_sent_again(db):
    duty = _duty(db, owners=["a@example.org"])
    db.add(DutyReminder(duty_id=duty.id, due_on=date(2027, 1, 15), lead_days=0))
    db.commit()
    assert duties.send_reminders(db, now=_at(2027, 1, 15)) == 0
    assert not _queued(db)


def test_no_one_to_remind_is_skipped(db):
    _duty(db, owners=["management"])  # no management accounts
    assert duties.send_reminders(db, now=_at(2027, 1, 15)) == 0
    assert db.scalar(select(DutyReminder)) is None  # tried again once someone joins


def test_worker_runs_reminders_and_survives_a_failure(db, monkeypatch):
    from app import worker

    calls = []
    monkeypatch.setattr(duties, "send_reminders", lambda db: calls.append(1) or 0)
    worker.run_once(force_sync=True)
    assert calls == [1]

    def boom(db):
        raise RuntimeError("bad duty")

    monkeypatch.setattr(duties, "send_reminders", boom)
    worker.run_once(force_sync=True)  # email delivery still runs


# --- pages --------------------------------------------------------------------------


def _upload(client, text: str):
    return client.post_form(
        "/duties/upload", files={"file": ("duties.csv", io.BytesIO(text.encode()), "text/csv")}
    )


def test_upload_adds_and_updates_by_title(client_for, db):
    board = client_for(Role.BOARD, "board@example.com")
    resp = _upload(board, duties.template_csv())
    assert resp.status_code == 303
    assert "3 added, 0 updated" in board.get("/duties").text
    resp = _upload(
        board,
        "title,owners,first_due,repeat,remind_days\n"
        "renew the master insurance policy,management,2027-07-01,yearly,30\n",
    )
    assert "0 added, 1 updated" in board.get("/duties").text
    db.expire_all()
    duty = db.scalar(select(Duty).where(Duty.title.ilike("renew%")))
    assert duty.first_due == date(2027, 7, 1) and duty.remind_days == [30]
    assert duty.title == "renew the master insurance policy"
    assert len(db.scalars(select(Duty)).all()) == 3


def test_a_bad_upload_saves_nothing_and_says_why(client_for, db):
    board = client_for(Role.BOARD, "board@example.com")
    resp = _upload(board, "title,owners,first_due\nA,board,2027-01-01\nB,homeowners,2027-01-01\n")
    assert resp.status_code == 400
    assert "Nothing was saved" in resp.text and "Row 3" in resp.text
    assert db.scalar(select(Duty)) is None


def test_add_edit_and_delete(client_for, db):
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    form = {
        "title": "Pool inspection",
        "details": "",
        "owners": "management",
        "first_due": "2027-05-01",
        "repeat": "yearly",
        "remind_days": "30; 7",
    }
    assert mgr.post_form("/duties", form).status_code == 303
    duty_id = db.scalar(select(Duty.id))
    assert mgr.get(f"/duties/{duty_id}").status_code == 200
    dup = mgr.post_form("/duties", {**form, "title": "pool INSPECTION"})
    assert dup.status_code == 400 and "already a duty" in dup.text
    bad = mgr.post_form(f"/duties/{duty_id}", {**form, "owners": "nobody"})
    assert bad.status_code == 400 and "isn&#39;t an email address" in bad.text
    assert mgr.post_form(f"/duties/{duty_id}", {**form, "repeat": "quarterly"}).status_code == 303
    db.expire_all()
    assert db.get(Duty, duty_id).repeat == "quarterly"
    assert mgr.post_form(f"/duties/{duty_id}/delete").status_code == 303
    db.expire_all()
    assert db.get(Duty, duty_id) is None


def test_list_shows_whats_coming(client_for, db):
    today = duties.local_today()
    _duty(db, title="Gutter cleaning", first_due=today, repeat="none")
    _duty(db, title="Far off", first_due=date(today.year + 2, 1, 1), repeat="none")
    page = client_for(Role.ADMIN, "admin2@example.com").get("/duties").text
    upcoming = page.split("All duties")[0]
    assert "Gutter cleaning" in upcoming and "Today" in upcoming
    assert "Far off" not in upcoming


def test_export_round_trips_and_defuses_formulas(client_for, db):
    _duty(db, title="=HYPERLINK(1)", details="+cmd", owners=["board", "a@example.org"])
    board = client_for(Role.BOARD, "board@example.com")
    resp = board.get("/duties/duties.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK(1)" in resp.text and "'+cmd" in resp.text
    rows = duties.parse_csv(resp.text.encode())
    assert rows[0].owners == ["board", "a@example.org"] and rows[0].repeat == "monthly"


def test_homeowners_cant_see_duties(client_for):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    assert owner.get("/duties").status_code == 403
    assert _upload(owner, duties.template_csv()).status_code == 403
    assert owner.get("/duties/template.csv").status_code == 403
