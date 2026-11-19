"""Amenity bookings: the tennis rules, clubhouse approvals, privacy and permissions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.amenities import parse_amenities
from app.config import get_settings
from app.models import EmailMessage, Reservation
from app.permissions import Role
from app.services import accounts
from app.services import reservations as svc

TZ = ZoneInfo(get_settings().hoa_timezone)


def L(day: int, hour: int, minute: int = 0, month: int = 3) -> datetime:  # noqa: N802
    """Local time in March 2027. March 1, 2027 is a Monday."""
    return datetime(2027, month, day, hour, minute, tzinfo=TZ)


MONDAY_6AM = L(1, 6)


@pytest.fixture
def owner(make_user, db):
    def _make(email: str, unit: str = ""):
        user = make_user(Role.HOMEOWNER, email)
        user.unit = unit
        db.commit()
        return user

    return _make


def book(db, user, space, start, minutes=60, now=MONDAY_6AM, **kw):
    r = svc.book(db, user=user, space=space, start=start, minutes=minutes, now=now, **kw)
    db.commit()
    return r


def refused(db, user, space, start, minutes=60, now=MONDAY_6AM, **kw) -> str:
    with pytest.raises(svc.ReservationError) as exc:
        svc.book(db, user=user, space=space, start=start, minutes=minutes, now=now, **kw)
    db.rollback()
    return str(exc.value)


# --- Tennis rules (default: one court) ----------------------------------------------


def test_tennis_booking_is_confirmed_and_takes_the_slot(db, owner):
    a, b = owner("a@example.com", "1A"), owner("b@example.com", "2B")
    r = book(db, a, "tennis", L(1, 7))
    assert r.status == "confirmed" and r.household == "unit:1a"
    assert "already booked" in refused(db, b, "tennis", L(1, 7, 30))
    assert book(db, b, "tennis", L(1, 8)).status == "confirmed"


def test_one_court_one_booking_a_day_and_three_a_week(db, owner):
    a = owner("a@example.com", "1A")
    book(db, a, "tennis", L(1, 7))
    assert "used its 1 booking" in refused(db, a, "tennis", L(1, 18))
    book(db, a, "tennis", L(2, 7))
    assert "upcoming" in refused(db, a, "tennis", L(3, 7))  # two upcoming already
    later = L(2, 20)  # both are over
    book(db, a, "tennis", L(3, 7), now=later)
    msg = refused(db, a, "tennis", L(4, 7), now=L(3, 20))
    assert "3 bookings" in msg and "week of March 1" in msg
    assert book(db, a, "tennis", L(8, 7), now=L(3, 20)).status == "confirmed"  # next week


def test_people_in_one_unit_share_the_limits(db, owner):
    a1, a2 = owner("a1@example.com", "12B"), owner("a2@example.com", " 12b ")
    book(db, a1, "tennis", L(1, 7))
    assert "used its 1 booking" in refused(db, a2, "tennis", L(1, 18))
    # Without a unit, each person is their own household.
    solo = owner("solo@example.com")
    assert book(db, solo, "tennis", L(1, 8)).household == f"user:{solo.id}"


def test_late_cancellation_still_counts_toward_the_daily_limit(db, owner):
    a = owner("a@example.com", "1A")
    evening = book(db, a, "tennis", L(1, 18))
    svc.cancel(db, evening, by=a, now=L(1, 17))  # an hour before: late
    db.commit()
    assert evening.counts_after_cancel
    assert "used its 1 booking" in refused(db, a, "tennis", L(1, 20), now=L(1, 17))


def test_early_cancellation_frees_the_limit(db, owner):
    a = owner("a@example.com", "1A")
    evening = book(db, a, "tennis", L(1, 18))
    svc.cancel(db, evening, by=a, now=L(1, 12))
    db.commit()
    assert not evening.counts_after_cancel
    assert book(db, a, "tennis", L(1, 19), now=L(1, 12)).status == "confirmed"


@pytest.mark.parametrize(
    ("start", "minutes", "message"),
    [
        (L(1, 6, 30), 60, "The tennis court is open 7:00 AM to 9:00 PM"),
        (L(1, 20, 30), 60, "open 7:00 AM to 9:00 PM"),
        (L(1, 7, 15), 60, "start every 30 minutes"),
        (L(1, 7), 45, "steps of 30 minutes"),
        (L(1, 7), 120, "1 hour to 1 hour 30 minutes"),
        (L(1, 7), 30, "1 hour to 1 hour 30 minutes"),
        (L(9, 7), 60, "up to 7 days ahead"),
        (L(1, 7), 60, "already started or passed"),
    ],
)
def test_tennis_time_rules(db, owner, start, minutes, message):
    a = owner("a@example.com", "1A")
    now = L(1, 7, 10) if "passed" in message else MONDAY_6AM
    assert message in refused(db, a, "tennis", start, minutes, now=now)


# --- Groups with several spaces (e.g. an HOA with two courts) ------------------------


@pytest.fixture
def two_courts(monkeypatch):
    groups = parse_amenities(
        json.dumps(
            {
                "tennis": {
                    "label": "Tennis courts",
                    "spaces": {"court-1": "Court 1", "court-2": "Court 2"},
                }
            }
        )
    )
    monkeypatch.setattr(svc, "groups", lambda: groups)


def test_no_back_to_back_bookings_on_any_court(db, owner, two_courts):
    a, b = owner("a@example.com", "1A"), owner("b@example.com", "2B")
    book(db, a, "court-1", L(1, 7))  # 7:00 to 8:00
    assert book(db, b, "court-2", L(1, 12)).status == "confirmed"
    assert "back-to-back" in refused(db, a, "court-2", L(1, 8))
    assert "back-to-back" in refused(db, a, "court-1", L(1, 9, 30))
    assert "already has a booking then" in refused(db, a, "court-2", L(1, 7, 30))
    assert book(db, a, "court-1", L(1, 10)).status == "confirmed"  # exactly 2 hours later
    assert "used its 2 bookings" in refused(db, a, "court-2", L(1, 18), now=L(1, 12))


def test_limits_count_across_courts(db, owner, two_courts):
    a = owner("a@example.com", "1A")
    book(db, a, "court-1", L(1, 7))
    book(db, a, "court-2", L(1, 18))
    msg = refused(db, a, "court-1", L(2, 7))
    assert "2 upcoming bookings for the tennis courts" in msg
    assert "Tennis courts are open" in refused(db, a, "court-1", L(3, 6))


def test_cancel_is_limited_to_the_household_and_management(db, owner, make_user):
    a, b = owner("a@example.com", "1A"), owner("b@example.com", "2B")
    partner = owner("a2@example.com", "1a")
    mgr = make_user(Role.MANAGEMENT, "manager@example.com")
    r = book(db, a, "tennis", L(1, 7))
    with pytest.raises(svc.ReservationError):
        svc.cancel(db, r, by=b, now=MONDAY_6AM)
    svc.cancel(db, r, by=partner, now=MONDAY_6AM)
    assert r.status == "cancelled"
    r2 = book(db, a, "tennis", L(2, 10))
    svc.cancel(db, r2, by=mgr, reason="Court repairs", now=MONDAY_6AM)
    db.commit()
    mail = db.scalars(select(EmailMessage).where(EmailMessage.kind == "reservation")).one()
    assert mail.to_email == "a@example.com" and "Court repairs" in mail.text_body


# --- Clubhouse ---------------------------------------------------------------------


def test_clubhouse_requests_wait_for_management(db, owner, make_user):
    make_user(Role.MANAGEMENT, "manager@example.com")
    mgr = db.scalar(select(accounts.User).where(accounts.User.email == "manager@example.com"))
    a, b = owner("a@example.com", "1A"), owner("b@example.com", "2B")
    party = L(1, 14, month=4)
    assert "what the booking is for" in refused(db, a, "clubhouse", party, 180)
    assert "at least 14 days ahead" in refused(db, a, "clubhouse", L(10, 14), 180, purpose="Party")
    assert "2 hours to 8 hours" in refused(db, a, "clubhouse", party, 60, purpose="Party")

    r = book(db, a, "clubhouse", party, 180, purpose="Birthday party, 25 guests")
    assert r.status == "pending"
    mails = list(db.scalars(select(EmailMessage).where(EmailMessage.kind == "reservation")))
    assert sorted(m.to_email for m in mails) == ["manager@example.com", "office@management.example"]
    assert "Birthday party" in mails[0].text_body
    # A pending request holds the time.
    assert "already booked" in refused(db, b, "clubhouse", party, 120, purpose="Meeting")

    svc.decide(db, r, by=mgr, approve=True, note="The deposit is due a week before.")
    db.commit()
    assert r.status == "confirmed"
    sent = db.scalars(select(EmailMessage).where(EmailMessage.to_email == "a@example.com")).one()
    assert "confirmed" in sent.subject and "deposit" in sent.text_body
    with pytest.raises(svc.ReservationError):
        svc.decide(db, r, by=mgr, approve=False)


def test_declined_request_frees_the_time(db, owner, make_user):
    mgr = make_user(Role.MANAGEMENT, "manager@example.com")
    a, b = owner("a@example.com", "1A"), owner("b@example.com", "2B")
    party = L(1, 14, month=4)
    r = book(db, a, "clubhouse", party, 180, purpose="Party")
    svc.decide(db, r, by=mgr, approve=False, note="Booked for the annual meeting.")
    db.commit()
    assert book(db, b, "clubhouse", party, 120, purpose="Book club").status == "pending"


# --- Closures ----------------------------------------------------------------------


def test_management_can_close_a_space_but_not_over_bookings(db, owner, make_user):
    mgr = make_user(Role.MANAGEMENT, "manager@example.com")
    a = owner("a@example.com", "1A")
    book(db, a, "tennis", L(2, 7))
    assert "Cancel them first" in refused(
        db, mgr, "tennis", L(2, 7), 240, block=True, purpose="Resurfacing"
    )
    closure = book(db, mgr, "tennis", L(3, 7), 240, block=True, purpose="Resurfacing")
    assert closure.kind == "block" and closure.status == "confirmed"
    assert "already booked" in refused(db, a, "tennis", L(3, 9))
    assert "Only management" in refused(db, a, "tennis", L(4, 7), block=True, purpose="x")


# --- Pages and permissions -----------------------------------------------------------


def _tomorrow(hour: int) -> str:
    day = datetime.now(TZ).date() + timedelta(days=1)
    return f"{day.isoformat()}T{hour:02d}:00"


def test_booking_through_the_pages_and_privacy(client_for, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    owner.user.unit = "7C"
    db.merge(owner.user)
    db.commit()
    other = client_for(Role.HOMEOWNER, "other@example.com")
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")

    form = owner.get(f"/reservations/tennis/new?start={_tomorrow(10)}")
    assert form.status_code == 200 and "1 hour 30 minutes" in form.text
    resp = owner.post_form("/reservations/tennis", {"start": _tomorrow(10), "minutes": "60"})
    assert resp.status_code == 303
    r = db.scalar(select(Reservation))
    assert r.status == "confirmed"
    week = resp.headers["location"]
    assert "Yours" in owner.get(week).text
    seen = other.get(week).text
    assert "Booked" in seen and "7C" not in seen and "owner@example.com" not in seen
    assert "7C" in mgr.get(week).text

    # Rule errors come back on the form.
    again = owner.post_form("/reservations/tennis", {"start": _tomorrow(15), "minutes": "60"})
    assert again.status_code == 400 and "used its 1 booking" in again.text

    assert other.get(f"/reservations/r/{r.id}").status_code == 404
    assert other.post_form(f"/reservations/r/{r.id}/cancel").status_code == 404
    assert owner.post_form(f"/reservations/r/{r.id}/cancel").status_code == 303
    db.expire_all()
    assert db.get(Reservation, r.id).status == "cancelled"


def test_homeowners_cannot_block_or_approve(client_for, db):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    assert "Tennis court" in owner.get("/reservations").text
    page = owner.get("/reservations/clubhouse")
    assert page.status_code == 200
    assert 'small">10:00 PM</th>' in page.text and 'small">11:00 PM</th>' not in page.text
    assert (
        "Close Clubhouse" not in owner.get(f"/reservations/clubhouse/new?start={_tomorrow(9)}").text
    )
    assert owner.get("/reservations/no-such-court").status_code == 404
    data = {"start": _tomorrow(10), "minutes": "60", "block": "1", "purpose": "x"}
    assert owner.post_form("/reservations/tennis", data).status_code == 403
    assert owner.get("/reservations/manage").status_code == 403
    assert owner.post_form("/reservations/r/1/decide", {"decision": "approve"}).status_code == 403


def test_clubhouse_request_and_approval_through_the_pages(client_for, db, outbox):
    owner = client_for(Role.HOMEOWNER, "owner@example.com")
    mgr = client_for(Role.MANAGEMENT, "manager@example.com")
    day = (datetime.now(TZ).date() + timedelta(days=30)).isoformat()
    resp = owner.post_form(
        "/reservations/clubhouse",
        {"start": f"{day}T14:00", "minutes": "180", "purpose": "Graduation party"},
    )
    assert resp.status_code == 303
    r = db.scalar(select(Reservation))
    assert r.status == "pending"
    assert {m.to for m in outbox} == {"manager@example.com", "office@management.example"}
    page = mgr.get("/reservations/manage")
    assert "Graduation party" in page.text
    outbox.clear()
    resp = mgr.post_form(f"/reservations/r/{r.id}/decide", {"decision": "approve", "note": ""})
    assert resp.status_code == 303
    assert [m.to for m in outbox] == ["owner@example.com"]
    db.expire_all()
    assert db.get(Reservation, r.id).status == "confirmed"


def test_management_sets_units_but_not_for_admins(client_for, make_user, db):
    board = client_for(Role.BOARD, "board@example.com")
    owner = make_user(Role.HOMEOWNER, "owner@example.com")
    admin = make_user(Role.ADMIN, "admin@example.com")
    assert board.post_form(f"/admin/users/{owner.id}/unit", {"unit": " 4D "}).status_code == 303
    db.expire_all()
    assert db.get(accounts.User, owner.id).unit == "4D"
    assert board.post_form(f"/admin/users/{admin.id}/unit", {"unit": "1"}).status_code == 403


def test_invite_carries_the_unit(db, make_user):
    admin = make_user(Role.ADMIN, "admin@example.com")
    inv = accounts.create_invite(
        db, email="new@example.com", role=Role.HOMEOWNER, invited_by=admin, unit="9F"
    )
    user = accounts.accept_invite(db, inv, full_name="New", password="a long password!")
    assert user.unit == "9F"


def test_bad_amenity_settings_are_rejected():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="unknown setting"):
        Settings(amenities='{"tennis": {"spaces": {"t1": "Court"}, "max_minuts": 90}}')
    with pytest.raises(ValidationError, match="multiples"):
        Settings(amenities='{"tennis": {"spaces": {"t1": "Court"}, "min_minutes": 45}}')
    with pytest.raises(ValidationError, match="can't be used"):
        Settings(amenities='{"x": {"spaces": {"manage": "Court"}}}')
    with pytest.raises(ValidationError, match="time zone"):
        Settings(hoa_timezone="Mars/Olympus")
