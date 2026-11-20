"""The duty calendar: parsing, recurrence and the pages."""

import io
from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models import Duty, EmailMessage
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
        hard_deadline="Yes",
    )
    assert f.title == "File taxes"
    assert f.owners == ["treasurer@example.org", "board", "management"]
    assert f.first_due == date(2027, 3, 31) and f.repeat == "yearly"
    assert f.hard_deadline is True
    assert duties.parse_hard("") is False and duties.parse_hard("x") is True


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("owners", "homeowners", "homeowners can't own"),
        ("owners", "bob", "isn't an email address"),
        ("first_due", "next week", "isn't a date"),
        ("repeat", "fortnightly", "isn't a repeat"),
        ("hard_deadline", "maybe", "isn't yes or no"),
        ("title", "  ", "add a title"),
    ],
)
def test_parse_fields_rejects(field, value, message):
    values = dict(
        title="x", details="", owners="board", first_due="2027-01-01", repeat="", hard_deadline=""
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
    with pytest.raises(duties.DutyError, match="Missing: first_due"):
        duties.parse_csv(b"title\nx\n")
    with pytest.raises(duties.DutyError, match="Unknown columns: due"):
        duties.parse_csv(b"title,owners,first_due,due\nx,board,2027-01-01,y\n")


def test_template_parses():
    rows = duties.parse_csv(duties.template_csv().encode())
    assert [r.repeat for r in rows] == ["monthly", "yearly", "yearly"]


# --- recurrence ---------------------------------------------------------------------


def _dates(repeat, first, start, end):
    duty = Duty(title="x", owners=["board"], first_due=first, repeat=repeat)
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
        "title,owners,first_due,repeat,hard_deadline,remind_days\n"
        "renew the master insurance policy,,2027-07-01,yearly,no,30\n",
    )
    assert "0 added, 1 updated" in board.get("/duties").text
    db.expire_all()
    duty = db.scalar(select(Duty).where(Duty.title.ilike("renew%")))
    assert duty.first_due == date(2027, 7, 1) and duty.hard_deadline is False
    assert duty.owners == []  # owners are optional now
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
        "hard_deadline": "yes",
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
    assert db.get(Duty, duty_id).hard_deadline is True
    assert mgr.post_form(f"/duties/{duty_id}", {**form, "hard_deadline": ""}).status_code == 303
    db.expire_all()
    assert db.get(Duty, duty_id).hard_deadline is False
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
