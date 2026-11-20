"""Amenity reservations: the booking rules, approvals and the calendar grid.

All times are stored in UTC. Opening hours, days and weeks are worked out in the
HOA's own time zone (HOA_TIMEZONE), so "7:00" means 7:00 at the HOA all year.
Every limit is per household (people who share a unit) and counts across all
spaces in an amenity group.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.amenities import AmenityGroup
from app.config import get_settings
from app.db import utcnow
from app.email.outbox import enqueue
from app.models import RESERVATION_ACTIVE, Reservation, User
from app.permissions import Perm, has_perm
from app.services.escalation import management_recipients


class ReservationError(Exception):
    pass


# --- Configuration and time helpers ------------------------------------------------


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().hoa_timezone)


def groups() -> list[AmenityGroup]:
    return get_settings().amenity_groups


def find_space(slug: str) -> tuple[AmenityGroup, str] | None:
    for group in groups():
        if slug in group.spaces:
            return group, group.spaces[slug]
    return None


def group_by_key(key: str) -> AmenityGroup | None:
    return next((g for g in groups() if g.key == key), None)


def space_name(r: Reservation) -> str:
    group = group_by_key(r.amenity_group)
    return group.spaces.get(r.space, r.space) if group else r.space


def local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz())


def at(day: date, t: time) -> datetime:
    """A local wall-clock time on `day`, as an aware datetime."""
    return datetime.combine(day, t, tzinfo=tz())


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    return _utc(at(day, time())), _utc(at(day + timedelta(days=1), time()))


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def fmt_time(dt: datetime) -> str:
    return local(dt).strftime("%-I:%M %p")


def fmt_when(start: datetime, end: datetime) -> str:
    s, e = local(start), local(end)
    return f"{s.strftime('%a, %b %-d')}, {fmt_time(s)} to {fmt_time(e)}"


def fmt_minutes(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if not hours:
        return f"{mins} minutes"
    label = f"{hours} hour" + ("s" if hours != 1 else "")
    return f"{label} {mins} minutes" if mins else label


def parse_local(value: str) -> datetime:
    """'2027-03-01T18:00' (the HOA's local time) -> aware datetime."""
    try:
        naive = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ReservationError("That start time isn't valid.") from exc
    return naive.replace(tzinfo=tz())


def rule_summary(group: AmenityGroup) -> list[str]:
    """The rules in plain sentences, for the booking pages."""
    r = group.rules
    out = [f"Open {fmt_time(at(date.today(), group.open_time))} to {_close_label(group)}."]
    if r.min_minutes == r.max_minutes:
        out.append(f"Bookings last {fmt_minutes(r.min_minutes)}.")
    else:
        out.append(f"Bookings last {fmt_minutes(r.min_minutes)} to {fmt_minutes(r.max_minutes)}.")
    if r.book_ahead_days:
        out.append(f"You can book up to {r.book_ahead_days} days ahead.")
    else:
        out.append("You can book for today only.")
    if r.min_notice_minutes >= 24 * 60:
        out.append(f"Book at least {r.min_notice_minutes // (24 * 60)} days ahead.")
    elif r.min_notice_minutes:
        out.append(f"Book at least {fmt_minutes(r.min_notice_minutes)} ahead.")
    scope = "across all " + group.label.lower() if len(group.spaces) > 1 else ""
    limits = []
    if r.max_upcoming is not None:
        limits.append(f"{r.max_upcoming} upcoming at a time")
    if r.max_per_day is not None:
        limits.append(f"{r.max_per_day} a day")
    if r.max_per_week is not None:
        limits.append(f"{r.max_per_week} a week (Monday to Sunday)")
    if limits:
        out.append(f"Each household can have {', '.join(limits)}{' ' + scope if scope else ''}.")
    if r.gap_minutes:
        out.append(
            f"No back-to-back bookings: leave at least {fmt_minutes(r.gap_minutes)} between "
            "your household's bookings."
        )
    if r.late_cancel_minutes:
        out.append(
            f"A booking cancelled less than {fmt_minutes(r.late_cancel_minutes)} before it "
            "starts still counts toward your daily and weekly limits."
        )
    if r.requires_approval:
        out.append("Management reviews each request before it's confirmed.")
    return out


def _close_label(group: AmenityGroup) -> str:
    c = group.close_time
    return "midnight" if c >= time(23, 59) else fmt_time(at(date.today(), c))


# --- Queries ---------------------------------------------------------------------


def _lock(db: Session, group_key: str) -> None:
    """Serialise bookings for one amenity group until the transaction ends.

    On Postgres this is a transaction-scoped advisory lock, so two people pressing
    Book on the same slot at the same moment can't both get it. SQLite (dev and
    tests) only allows one writer at a time anyway.
    """
    if db.get_bind().dialect.name == "postgresql":
        key = zlib.crc32(f"reservations:{group_key}".encode())
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def _overlapping(db: Session, space: str, start: datetime, end: datetime, exclude: int | None):
    q = select(Reservation).where(
        Reservation.space == space,
        Reservation.status.in_(RESERVATION_ACTIVE),
        Reservation.starts_at < _utc(end),
        Reservation.ends_at > _utc(start),
    )
    if exclude:
        q = q.where(Reservation.id != exclude)
    return list(db.scalars(q.order_by(Reservation.starts_at)))


def _household_q(group: AmenityGroup, household: str):
    return select(Reservation).where(
        Reservation.amenity_group == group.key,
        Reservation.household == household,
        Reservation.kind == "booking",
    )


def _counted(group: AmenityGroup, household: str, since: datetime, until: datetime):
    """Bookings that count toward the daily/weekly limits in [since, until)."""
    return _household_q(group, household).where(
        or_(
            Reservation.status.in_(RESERVATION_ACTIVE),
            Reservation.counts_after_cancel.is_(True),
        ),
        Reservation.starts_at >= _utc(since),
        Reservation.starts_at < _utc(until),
    )


def _count(db: Session, q) -> int:
    return db.scalar(select(func.count()).select_from(q.subquery())) or 0


# --- Rules -----------------------------------------------------------------------


def check_slot(
    group: AmenityGroup, start: datetime, minutes: int, now: datetime, *, block: bool = False
) -> datetime:
    """Check the time itself (hours, grid, length, booking window). Returns the end."""
    r = group.rules
    start = local(start)
    end = start + timedelta(minutes=minutes)
    day = start.date()
    opens, closes = at(day, group.open_time), at(day, group.close_time)
    if start < opens or end > closes:
        raise ReservationError(
            f"{group.label} are open {fmt_time(opens)} to {_close_label(group)}."
            if len(group.spaces) > 1
            else f"The {group.label.lower()} is open {fmt_time(opens)} to {_close_label(group)}."
        )
    offset = int((start - opens).total_seconds() // 60)
    if offset % r.slot_minutes or (start - opens).total_seconds() % 60:
        raise ReservationError(
            f"Bookings start every {fmt_minutes(r.slot_minutes)} from {fmt_time(opens)}."
        )
    if minutes <= 0 or minutes % r.slot_minutes:
        raise ReservationError(f"Choose a length in steps of {fmt_minutes(r.slot_minutes)}.")
    if start <= local(now):
        raise ReservationError("That time has already started or passed.")
    if block:
        return end
    if minutes < r.min_minutes or minutes > r.max_minutes:
        raise ReservationError(
            f"Bookings last {fmt_minutes(r.min_minutes)} to {fmt_minutes(r.max_minutes)}."
            if r.min_minutes != r.max_minutes
            else f"Bookings last {fmt_minutes(r.min_minutes)}."
        )
    last_day = local(now).date() + timedelta(days=r.book_ahead_days)
    if day > last_day:
        raise ReservationError(
            f"You can book up to {r.book_ahead_days} days ahead, so up to "
            f"{last_day.strftime('%A, %B %-d')}."
        )
    if r.min_notice_minutes and start < local(now) + timedelta(minutes=r.min_notice_minutes):
        notice = r.min_notice_minutes
        ahead = f"{notice // 1440} days" if notice >= 1440 else fmt_minutes(notice)
        raise ReservationError(f"Please book at least {ahead} ahead.")
    return end


def check_household(
    db: Session,
    group: AmenityGroup,
    household: str,
    start: datetime,
    end: datetime,
    now: datetime,
) -> None:
    """The per-household limits and the back-to-back rule."""
    r = group.rules
    scope = f" for the {group.label.lower()}"
    gap = timedelta(minutes=r.gap_minutes)
    near = db.scalars(
        _household_q(group, household)
        .where(
            Reservation.status.in_(RESERVATION_ACTIVE),
            Reservation.starts_at < _utc(end + gap),
            Reservation.ends_at > _utc(start - gap),
        )
        .order_by(Reservation.starts_at)
    ).first()
    if near is not None:
        when = fmt_when(near.start, near.end)
        if near.start < end and near.end > start:
            raise ReservationError(f"Your household already has a booking then ({when}).")
        raise ReservationError(
            f"No back-to-back bookings: leave at least {fmt_minutes(r.gap_minutes)} between "
            f"your household's bookings. You have {space_name(near)} on {when}."
        )
    if r.max_upcoming is not None:
        upcoming = _count(
            db,
            _household_q(group, household).where(
                Reservation.status.in_(RESERVATION_ACTIVE), Reservation.ends_at > _utc(now)
            ),
        )
        if upcoming >= r.max_upcoming:
            raise ReservationError(
                f"Your household already has {upcoming} upcoming "
                f"booking{'s' if upcoming != 1 else ''}{scope}, the most it can hold at once. "
                "You can book again once one is over or cancelled."
            )
    day = local(start).date()
    if r.max_per_day is not None:
        since, until = _day_bounds(day)
        if _count(db, _counted(group, household, since, until)) >= r.max_per_day:
            raise ReservationError(
                f"Your household has used its {r.max_per_day} "
                f"booking{'s' if r.max_per_day != 1 else ''}{scope} on "
                f"{day.strftime('%A, %B %-d')}."
            )
    if r.max_per_week is not None:
        monday = week_start(day)
        since, _ = _day_bounds(monday)
        until, _ = _day_bounds(monday + timedelta(days=7))
        if _count(db, _counted(group, household, since, until)) >= r.max_per_week:
            raise ReservationError(
                f"Your household has used its {r.max_per_week} "
                f"booking{'s' if r.max_per_week != 1 else ''}{scope} for the week of "
                f"{monday.strftime('%B %-d')}."
            )


# --- Actions ---------------------------------------------------------------------


def book(
    db: Session,
    *,
    user: User,
    space: str,
    start: datetime,
    minutes: int,
    purpose: str = "",
    block: bool = False,
    now: datetime | None = None,
) -> Reservation:
    now = now or utcnow()
    found = find_space(space)
    if found is None:
        raise ReservationError("That amenity doesn't exist.")
    group, name = found
    if block and not has_perm(user, Perm.RESERVATIONS_MANAGE):
        raise ReservationError("Only management can block time.")
    if not block and not has_perm(user, Perm.RESERVATIONS_BOOK):
        raise ReservationError("Your account can't make bookings.")
    purpose = purpose.strip()[:2000]
    if group.rules.ask_purpose and not block and len(purpose) < 3:
        raise ReservationError("Tell management briefly what the booking is for.")
    if block and not purpose:
        raise ReservationError("Add a reason, for example 'Court resurfacing'.")

    end = check_slot(group, start, minutes, now, block=block)
    _lock(db, group.key)
    clash = _overlapping(db, space, start, end, None)
    if clash:
        if block:
            whens = "; ".join(fmt_when(c.start, c.end) for c in clash[:5])
            raise ReservationError(
                f"{name} already has bookings in that time ({whens}). "
                "Cancel them first, so the people affected are told."
            )
        raise ReservationError(f"{name} is already booked for part of that time.")
    household = "block" if block else user.household
    if not block:
        check_household(db, group, household, start, end, now)

    pending = group.rules.requires_approval and not block
    r = Reservation(
        amenity_group=group.key,
        space=space,
        kind="block" if block else "booking",
        user_id=user.id,
        household=household,
        starts_at=_utc(start),
        ends_at=_utc(end),
        status="pending" if pending else "confirmed",
        purpose=purpose,
    )
    db.add(r)
    db.flush()
    if pending:
        for email in management_recipients(db):
            enqueue(
                db,
                to=email,
                subject=f"[Booking request] {name}, {fmt_when(r.start, r.end)}",
                template="reservation_request",
                kind="reservation",
                space=name,
                when=fmt_when(r.start, r.end),
                requester=user.display_name,
                requester_email=user.email,
                unit=user.unit,
                purpose=purpose,
                link=_link(r),
            )
    return r


def can_view(user: User, r: Reservation) -> bool:
    if has_perm(user, Perm.RESERVATIONS_MANAGE):
        return True
    return r.kind == "booking" and (r.user_id == user.id or r.household == user.household)


def cancel(
    db: Session, r: Reservation, *, by: User, reason: str = "", now: datetime | None = None
) -> None:
    now = now or utcnow()
    manager = has_perm(by, Perm.RESERVATIONS_MANAGE)
    own = r.kind == "booking" and (r.user_id == by.id or r.household == by.household)
    if not (own or manager):
        raise ReservationError("You can only cancel your own household's bookings.")
    if not r.is_active:
        raise ReservationError("This booking isn't active.")
    if r.end <= now:
        raise ReservationError("This booking is already over.")
    group = group_by_key(r.amenity_group)
    reason = reason.strip()[:2000]
    r.status = "cancelled"
    r.cancelled_by_id = by.id
    r.cancelled_at = _utc(now)
    r.cancel_reason = reason
    if own and group and r.kind == "booking":
        late = timedelta(minutes=group.rules.late_cancel_minutes)
        r.counts_after_cancel = bool(late) and r.start - now < late
    if not own and r.kind == "booking" and r.user and r.user.is_active:
        enqueue(
            db,
            to=r.user.email,
            subject=f"Your booking for {space_name(r)} was cancelled",
            template="reservation_cancelled",
            kind="reservation",
            space=space_name(r),
            when=fmt_when(r.start, r.end),
            by=by.display_name,
            reason=reason,
            link=f"{get_settings().base_url.rstrip('/')}/reservations",
        )


def decide(
    db: Session,
    r: Reservation,
    *,
    by: User,
    approve: bool,
    note: str = "",
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    if not has_perm(by, Perm.RESERVATIONS_MANAGE):
        raise ReservationError("Only management can approve requests.")
    if r.status != "pending":
        raise ReservationError("This request has already been decided.")
    if approve and r.start <= now:
        raise ReservationError("This request's start time has passed. Decline it instead.")
    r.status = "confirmed" if approve else "declined"
    r.decided_by_id = by.id
    r.decided_at = _utc(now)
    r.decision_note = note.strip()[:2000]
    if r.user and r.user.is_active:
        enqueue(
            db,
            to=r.user.email,
            subject=(
                f"Your {space_name(r)} booking is confirmed"
                if approve
                else f"Your {space_name(r)} request was declined"
            ),
            template="reservation_decision",
            kind="reservation",
            approved=approve,
            space=space_name(r),
            when=fmt_when(r.start, r.end),
            note=r.decision_note,
            link=_link(r),
        )


def _link(r: Reservation) -> str:
    return f"{get_settings().base_url.rstrip('/')}/reservations/r/{r.id}"


# --- Lists and the calendar grid ---------------------------------------------------


def upcoming_for(db: Session, user: User, now: datetime | None = None) -> list[Reservation]:
    now = now or utcnow()
    q = select(Reservation).where(
        Reservation.kind == "booking",
        or_(Reservation.household == user.household, Reservation.user_id == user.id),
        Reservation.status.in_(RESERVATION_ACTIVE),
        Reservation.ends_at > _utc(now),
    )
    return list(db.scalars(q.order_by(Reservation.starts_at)))


def pending_requests(db: Session) -> list[Reservation]:
    q = select(Reservation).where(Reservation.status == "pending")
    return list(db.scalars(q.order_by(Reservation.starts_at)))


def upcoming_all(db: Session, now: datetime | None = None, limit: int = 200) -> list[Reservation]:
    now = now or utcnow()
    q = select(Reservation).where(
        Reservation.status == "confirmed", Reservation.ends_at > _utc(now)
    )
    return list(db.scalars(q.order_by(Reservation.starts_at).limit(limit)))


@dataclass
class Cell:
    state: str  # past | unavailable | free | booked | yours | pending | blocked
    label: str = ""
    href: str = ""


@dataclass
class Week:
    group: AmenityGroup
    space: str
    name: str
    start: date
    days: list[date]
    times: list[str]
    rows: list[list[Cell]]
    prev: date | None
    next: date | None


def week_view(
    db: Session, space: str, start_day: date, viewer: User, now: datetime | None = None
) -> Week:
    now = now or utcnow()
    found = find_space(space)
    if found is None:
        raise ReservationError("That amenity doesn't exist.")
    group, name = found
    r = group.rules
    manager = has_perm(viewer, Perm.RESERVATIONS_MANAGE)
    monday = week_start(start_day)
    days = [monday + timedelta(days=i) for i in range(7)]
    since, _ = _day_bounds(monday)
    until, _ = _day_bounds(monday + timedelta(days=7))
    bookings = list(
        db.scalars(
            select(Reservation).where(
                Reservation.space == space,
                Reservation.status.in_(RESERVATION_ACTIVE),
                Reservation.starts_at < _utc(until),
                Reservation.ends_at > _utc(since),
            )
        )
    )
    today = local(now).date()
    last_day = today + timedelta(days=365 if manager else r.book_ahead_days)
    notice_until = local(now) + timedelta(minutes=0 if manager else r.min_notice_minutes)
    opens = group.open_time
    slots_per_day = int(
        (at(today, group.close_time) - at(today, opens)).total_seconds() // 60 // r.slot_minutes
    )
    times = [
        fmt_time(at(today, opens) + timedelta(minutes=i * r.slot_minutes))
        for i in range(slots_per_day)
    ]
    rows: list[list[Cell]] = []
    for i in range(slots_per_day):
        row = []
        for day in days:
            start = at(day, opens) + timedelta(minutes=i * r.slot_minutes)
            end = start + timedelta(minutes=r.slot_minutes)
            hit = next((b for b in bookings if b.start < end and b.end > start), None)
            row.append(_cell(hit, start, viewer, manager, now, notice_until, last_day, space))
        rows.append(row)
    first_week = week_start(today)
    prev = monday - timedelta(days=7) if monday > first_week else None
    nxt = monday + timedelta(days=7)
    return Week(
        group=group,
        space=space,
        name=name,
        start=monday,
        days=days,
        times=times,
        rows=rows,
        prev=prev,
        next=nxt if nxt <= last_day else None,
    )


def _cell(hit, start, viewer, manager, now, notice_until, last_day, space) -> Cell:
    if hit is not None:
        href = f"/reservations/r/{hit.id}"
        if hit.kind == "block":
            return Cell("blocked", hit.purpose if manager else "Closed", href if manager else "")
        mine = hit.household == viewer.household or hit.user_id == viewer.id
        if mine:
            return Cell("pending" if hit.status == "pending" else "yours", "Yours", href)
        if manager:
            who = (hit.user.unit or hit.user.display_name) if hit.user else "Former user"
            return Cell("pending" if hit.status == "pending" else "booked", who, href)
        return Cell("booked", "Requested" if hit.status == "pending" else "Booked")
    if start <= local(now):
        return Cell("past")
    if start.date() > last_day or start < notice_until:
        return Cell("unavailable")
    return Cell("free", "Free", f"/reservations/{space}/new?start={start:%Y-%m-%dT%H:%M}")
