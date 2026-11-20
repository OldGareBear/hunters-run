"""Recurring board and management duties, and the reminder emails sent to their owners.

A duty has a first due date and a repeat rule (none, weekly, monthly, quarterly, twice
a year or yearly). Its owners are email addresses and/or roles. Reminders go out a set
number of days before each due date (by default 7 days before and on the day), from the
same hourly job that delivers email. A `duty_reminders` row is written in the same
transaction as each email, and its unique key means a reminder never goes out twice.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.email.outbox import enqueue
from app.models import Duty, DutyReminder, User
from app.permissions import Role
from app.services.reservations import tz

log = logging.getLogger(__name__)

# How each repeat rule advances: ("weeks", n) or ("months", n). Labels are what people
# type in the CSV and see on the page.
REPEATS: dict[str, tuple[str, int]] = {
    "none": ("months", 0),
    "weekly": ("weeks", 1),
    "monthly": ("months", 1),
    "quarterly": ("months", 3),
    "twice a year": ("months", 6),
    "yearly": ("months", 12),
}
REPEAT_ALIASES = {
    "": "none",
    "once": "none",
    "one time": "none",
    "annually": "yearly",
    "annual": "yearly",
    "semiannually": "twice a year",
    "semi-annually": "twice a year",
    "every 6 months": "twice a year",
    "every 3 months": "quarterly",
}
OWNER_ROLES = {
    "admin": Role.ADMIN,
    "admins": Role.ADMIN,
    "board": Role.BOARD,
    "management": Role.MANAGEMENT,
}
DEFAULT_REMIND_DAYS = [7, 0]
MAX_LEAD_DAYS = 365
MAX_ROWS = 500
MAX_CSV_BYTES = 200_000
CSV_COLUMNS = ["title", "details", "owners", "first_due", "repeat", "remind_days"]
_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


class DutyError(Exception):
    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


# --- Parsing ---------------------------------------------------------------------


@dataclass
class DutyFields:
    title: str
    details: str = ""
    owners: list[str] = field(default_factory=list)
    first_due: date | None = None
    repeat: str = "none"
    remind_days: list[int] = field(default_factory=lambda: list(DEFAULT_REMIND_DAYS))


def parse_owners(value: str) -> list[str]:
    owners: list[str] = []
    for token in re.split(r"[;,\s]+", value.strip()):
        t = token.strip().lower()
        if not t:
            continue
        if t in ("homeowner", "homeowners"):
            raise ValueError("homeowners can't own a duty (that would email every homeowner)")
        if t in OWNER_ROLES:
            t = OWNER_ROLES[t].value
        elif not _EMAIL.match(t):
            raise ValueError(f'"{token}" isn\'t an email address or one of: board, management')
        if t not in owners:
            owners.append(t)
    if not owners:
        raise ValueError("add at least one owner (an email address, board or management)")
    return owners


def parse_date(value: str) -> date:
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'"{value}" isn\'t a date; use 2026-12-31 or 12/31/2026')


def parse_repeat(value: str) -> str:
    v = " ".join(value.strip().lower().split())
    v = REPEAT_ALIASES.get(v, v)
    if v not in REPEATS:
        raise ValueError(f'"{value}" isn\'t a repeat; use one of: {", ".join(REPEATS)}')
    return v


def parse_remind_days(value: str) -> list[int]:
    if not value.strip():
        return list(DEFAULT_REMIND_DAYS)
    days: set[int] = set()
    for token in re.split(r"[;,\s]+", value.strip()):
        if not token:
            continue
        if not token.isdigit() or int(token) > MAX_LEAD_DAYS:
            raise ValueError(f'"{token}" isn\'t a number of days from 0 to {MAX_LEAD_DAYS}')
        days.add(int(token))
    return sorted(days, reverse=True)


def parse_fields(
    *, title: str, details: str, owners: str, first_due: str, repeat: str, remind_days: str
) -> DutyFields:
    """Validate one duty. Raises ValueError with a message for people."""
    title = " ".join(title.split())[:200]
    if not title:
        raise ValueError("add a title")
    return DutyFields(
        title=title,
        details=details.strip()[:5000],
        owners=parse_owners(owners),
        first_due=parse_date(first_due),
        repeat=parse_repeat(repeat),
        remind_days=parse_remind_days(remind_days),
    )


def parse_csv(data: bytes) -> list[DutyFields]:
    if len(data) > MAX_CSV_BYTES:
        raise DutyError(f"The file is over {MAX_CSV_BYTES // 1000} KB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise DutyError("The file isn't a CSV text file.") from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [(h or "").strip().lower().replace(" ", "_") for h in (reader.fieldnames or [])]
    missing = [c for c in ("title", "owners", "first_due") if c not in header]
    if missing:
        raise DutyError(
            "The first row needs the column names: " + ", ".join(CSV_COLUMNS) + ". "
            f"Missing: {', '.join(missing)}."
        )
    unknown = [h for h in header if h and h not in CSV_COLUMNS]
    if unknown:
        raise DutyError(f"Unknown columns: {', '.join(unknown)}. Use: {', '.join(CSV_COLUMNS)}.")
    reader.fieldnames = header
    rows: list[DutyFields] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        row = {k: (v or "") for k, v in raw.items() if k}
        if not any(v.strip() for v in row.values()):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            raise DutyError(f"The file has more than {MAX_ROWS} duties.")
        try:
            fields = parse_fields(**{c: row.get(c, "") for c in CSV_COLUMNS})
        except ValueError as exc:
            errors.append(f"Row {line}: {exc}.")
            continue
        key = fields.title.lower()
        if key in seen:
            errors.append(f'Row {line}: "{fields.title}" appears twice.')
            continue
        seen.add(key)
        rows.append(fields)
    if errors:
        raise DutyError("Nothing was saved. Fix these rows and upload again.", errors)
    if not rows:
        raise DutyError("The file has no duties in it.")
    return rows


def _apply(duty: Duty, f: DutyFields) -> None:
    duty.title = f.title
    duty.details = f.details
    duty.owners = list(f.owners)
    duty.first_due = f.first_due
    duty.repeat = f.repeat
    duty.remind_days = list(f.remind_days)


def import_rows(db: Session, rows: list[DutyFields], *, by: User) -> tuple[int, int]:
    """Add new duties and update ones with the same title. Returns (added, updated)."""
    existing = {d.title.lower(): d for d in db.scalars(select(Duty))}
    added = updated = 0
    for f in rows:
        duty = existing.get(f.title.lower())
        if duty is None:
            duty = Duty(created_by_id=by.id)
            db.add(duty)
            added += 1
        else:
            updated += 1
        _apply(duty, f)
    return added, updated


def save(db: Session, f: DutyFields, *, by: User, duty: Duty | None = None) -> Duty:
    clash = db.scalar(select(Duty).where(Duty.title.ilike(f.title)))
    if clash is not None and (duty is None or clash.id != duty.id):
        raise DutyError(f'There is already a duty called "{clash.title}".')
    if duty is None:
        duty = Duty(created_by_id=by.id)
        db.add(duty)
    _apply(duty, f)
    return duty


def _csv_cell(value: str) -> str:
    # Spreadsheet apps run cells that start with these as formulas.
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(duties: list[Duty]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for d in duties:
        w.writerow(
            [
                _csv_cell(d.title),
                _csv_cell(d.details),
                "; ".join(d.owners),
                d.first_due.isoformat(),
                d.repeat,
                "; ".join(str(n) for n in d.remind_days),
            ]
        )
    return out.getvalue()


TEMPLATE_ROWS = [
    ["Review the monthly financial statements", "", "board", "2027-01-15", "monthly", "7; 0"],
    [
        "Renew the master insurance policy",
        "Get two quotes first.",
        "management",
        "2027-06-01",
        "yearly",
        "60; 14; 0",
    ],
    [
        "File the annual corporate report",
        "",
        "treasurer@example.org; board",
        "2027-03-31",
        "yearly",
        "30; 7",
    ],
]


def template_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    w.writerows(TEMPLATE_ROWS)
    return out.getvalue()


# --- Dates ------------------------------------------------------------------------


def _add_months(start: date, months: int) -> date:
    """Same day of the month, or the month's last day when it's shorter (Jan 31 -> Feb 28)."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def occurrences(duty: Duty, start: date, end: date) -> Iterator[date]:
    """Due dates of `duty` from `start` to `end`, inclusive."""
    unit, step = REPEATS.get(duty.repeat, ("months", 0))
    first = duty.first_due
    if step == 0:
        if start <= first <= end:
            yield first
        return
    if unit == "weeks":
        n = max(0, -(-(start - first).days // (7 * step)))
        d = first + timedelta(weeks=n * step)
        while d <= end:
            yield d
            d += timedelta(weeks=step)
        return
    months_apart = (start.year - first.year) * 12 + start.month - first.month
    n = max(0, months_apart // step - 1)
    while True:
        d = _add_months(first, n * step)
        if d > end:
            return
        if d >= start:
            yield d
        n += 1


def next_due(duty: Duty, today: date) -> date | None:
    return next(occurrences(duty, today, today + timedelta(days=366 * 2)), None)


def local_today() -> date:
    return datetime.now(tz()).date()


@dataclass
class Upcoming:
    duty: Duty
    due: date


def upcoming(db: Session, today: date, days: int = 90) -> list[Upcoming]:
    end = today + timedelta(days=days)
    items = [
        Upcoming(d, due) for d in db.scalars(select(Duty)) for due in occurrences(d, today, end)
    ]
    return sorted(items, key=lambda u: (u.due, u.duty.title.lower()))


# --- Reminders --------------------------------------------------------------------


def recipients(db: Session, duty: Duty) -> list[str]:
    emails: set[str] = set()
    roles = [o for o in duty.owners if "@" not in o]
    emails.update(o for o in duty.owners if "@" in o)
    if roles:
        users = db.scalars(select(User).where(User.role.in_(roles), User.is_active.is_(True)))
        emails.update(u.email for u in users)
    return sorted(emails)


def due_reminders(duty: Duty, today: date) -> list[tuple[date, int]]:
    """(due date, lead days) of the reminders that should have gone out by `today`.

    For each coming due date, the reminder is the latest one whose day has arrived. If
    the job didn't run on a reminder's day it goes out late rather than not at all, and
    a reminder that a later one has replaced is skipped.
    """
    leads = sorted(set(duty.remind_days or DEFAULT_REMIND_DAYS))
    out = []
    for due in occurrences(duty, today, today + timedelta(days=max(leads))):
        days_left = (due - today).days
        lead = next((n for n in leads if n >= days_left), None)
        if lead is not None:
            out.append((due, lead))
    return out


def _subject(duty: Duty, due: date, today: date) -> str:
    days = (due - today).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return f"Reminder: {duty.title} is due {when}"


def send_reminders(db: Session, *, now: datetime | None = None) -> int:
    """Queue reminder emails that are due. Safe to run any number of times."""
    s = get_settings()
    now = (now or datetime.now(tz())).astimezone(tz())
    if now.hour < s.duty_reminder_hour:
        return 0
    today = now.date()
    sent = 0
    for duty in list(db.scalars(select(Duty).order_by(Duty.id))):
        for due, lead in due_reminders(duty, today):
            done = db.scalar(
                select(DutyReminder.id).where(
                    DutyReminder.duty_id == duty.id,
                    DutyReminder.due_on == due,
                    DutyReminder.lead_days == lead,
                )
            )
            if done:
                continue
            to = recipients(db, duty)
            if not to:
                log.warning("duty %s has no one to remind", duty.id)
                continue
            try:
                with db.begin_nested():
                    db.add(DutyReminder(duty_id=duty.id, due_on=due, lead_days=lead))
                    db.flush()
                    enqueue(
                        db,
                        to=", ".join(to),
                        subject=_subject(duty, due, today),
                        template="duty_reminder",
                        kind="duty",
                        title=duty.title,
                        details=duty.details,
                        due=f"{due:%A, %B} {due.day}, {due.year}",
                        when=_subject(duty, due, today).split(" is due ", 1)[1],
                        repeat=duty.repeat,
                        link=f"{s.base_url.rstrip('/')}/duties",
                    )
            except IntegrityError:
                continue  # another worker sent it first
            sent += 1
    db.commit()
    return sent
