"""Recurring board and management duties, and the reminder emails sent to their owners.

Duties come in from the page, from a CSV with one duty per row, or from a month-grid
calendar (Excel or CSV): months across the top, days of the month down the side, and a
task in each cell that's due that day every year. See `parse_upload`.

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
import zipfile
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
MAX_XLSX_BYTES = 2_000_000
MAX_XLSX_UNZIPPED_BYTES = 30_000_000
HARD_DEADLINE_REMIND_DAYS = [14, 7, 0]
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
    # From a month-grid calendar, which has no owners or details: on a re-upload the
    # duty keeps the owners and details set on the Duties page.
    from_grid: bool = False


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


def _decode(data: bytes) -> str:
    if len(data) > MAX_CSV_BYTES:
        raise DutyError(f"The file is over {MAX_CSV_BYTES // 1000} KB.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise DutyError("The file isn't a CSV text file.") from exc


def parse_csv(data: bytes) -> list[DutyFields]:
    """A CSV with one duty per row and the columns in CSV_COLUMNS."""
    return _parse_rows(_decode(data))


def _parse_rows(text: str) -> list[DutyFields]:
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


# --- Month-grid calendars ----------------------------------------------------------

MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
MONTHS["sept"] = 9
HARD_MARK = "!"
HARD_LEGEND = "hard deadline"
DEFAULT_HARD_FILL = "FFC000"  # the orange of "Hard Deadline" in the board's sheet
_MANAGEMENT_TASK = re.compile(r"^\s*(mgt|mgmt|management)\.?\s+co(mpany)?\b", re.I)
GRID_HEADER_ROWS = 15


@dataclass
class Cell:
    text: str
    hard: bool = False


@dataclass
class Parsed:
    rows: list[DutyFields]
    warnings: list[str] = field(default_factory=list)
    grid: bool = False


def _month(value: str) -> int | None:
    return MONTHS.get(value.strip().rstrip(".").lower())


def _find_header(grid: list[list[Cell]]) -> tuple[int, dict[int, int]] | None:
    """The row with the month names in it, and which column holds which month."""
    for r, row in enumerate(grid[:GRID_HEADER_ROWS]):
        months = {c: m for c, cell in enumerate(row) if (m := _month(cell.text))}
        if len(months) >= 6 and len(set(months.values())) == len(months):
            return r, months
    return None


def _day(value: str) -> int | None:
    v = value.strip()
    if v.endswith(".0"):
        v = v[:-2]
    v = re.sub(r"(st|nd|rd|th)$", "", v.lower())
    return int(v) if v.isdigit() and 1 <= int(v) <= 31 else None


def _grid_title(text: str) -> tuple[str, bool]:
    """The task's title, and whether a "!" before or after it marks a hard deadline."""
    t = " ".join(text.split())
    hard = t.startswith(HARD_MARK) or t.endswith(HARD_MARK)
    return t.strip(HARD_MARK + " ")[:200], hard


def default_owner(title: str) -> str:
    return Role.MANAGEMENT.value if _MANAGEMENT_TASK.match(title) else Role.BOARD.value


def parse_grid(grid: list[list[Cell]], *, year: int) -> Parsed:
    """A month-grid calendar: every task repeats yearly on its month and day.

    Owners default to the board, or to management for tasks that start "Mgt. Co". Hard
    deadlines (orange in Excel, or "!" in a CSV) get an extra reminder 14 days ahead. A
    day the month doesn't have (April 31) moves to the month's last day, with a warning.
    """
    found = _find_header(grid)
    if found is None:
        raise DutyError("The calendar needs a row with the month names across the top.")
    header, months = found
    first_month_col = min(months)
    day_cols = [c for c in range(first_month_col) if c not in months] or [0]
    entries: list[tuple[int, int, str, bool]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for r in range(header + 1, len(grid)):
        row = grid[r]
        cells = {c: row[c] for c in months if c < len(row) and row[c].text.strip()}
        if not cells:
            continue
        day = next((d for c in day_cols if c < len(row) and (d := _day(row[c].text))), None)
        if day is None:
            errors.append(f"Row {r + 1} has tasks but no day of the month (1 to 31) beside them.")
            continue
        for c, cell in sorted(cells.items(), key=lambda kv: months[kv[0]]):
            month = months[c]
            last = calendar.monthrange(year, month)[1]
            if day > (29 if month == 2 else last):
                warnings.append(
                    f"{calendar.month_abbr[month]} {day} isn't a date, so "
                    f"{calendar.month_abbr[month]} {last} is used."
                )
            for line in cell.text.splitlines():
                title, marked = _grid_title(line)
                if title and title.lower() != HARD_LEGEND:
                    entries.append((month, day, title, cell.hard or marked))
    if errors:
        raise DutyError("Nothing was saved. Fix these rows and upload again.", errors)
    if not entries:
        raise DutyError("The calendar has no tasks in it.")
    if len(entries) > MAX_ROWS:
        raise DutyError(f"The calendar has more than {MAX_ROWS} tasks.")

    counts: dict[str, int] = {}
    for _, _, title, _ in entries:
        counts[title.lower()] = counts.get(title.lower(), 0) + 1
    rows: list[DutyFields] = []
    seen: set[str] = set()
    for month, day, title, hard in sorted(entries):
        if counts[title.lower()] > 1:  # the same task twice a year: tell them apart
            title = f"{title} ({calendar.month_abbr[month]} {day})"
        if title.lower() in seen:
            continue  # the same task twice on the same day
        seen.add(title.lower())
        due = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        rows.append(
            DutyFields(
                title=title,
                owners=[default_owner(title)],
                first_due=due,
                repeat="yearly",
                remind_days=list(HARD_DEADLINE_REMIND_DAYS if hard else DEFAULT_REMIND_DAYS),
                from_grid=True,
            )
        )
    return Parsed(rows, warnings, grid=True)


def _csv_grid(text: str) -> list[list[Cell]]:
    return [[Cell(v or "") for v in row] for row in csv.reader(io.StringIO(text))]


def _fill_key(cell) -> tuple | None:
    fill = cell.fill
    if fill is None or fill.fill_type != "solid" or fill.fgColor is None:
        return None
    color = fill.fgColor
    if color.type == "rgb" and isinstance(color.rgb, str):
        rgb = color.rgb[-6:].upper()
        return None if rgb == "FFFFFF" or color.rgb.startswith("00") else ("rgb", rgb)
    if color.type == "theme":
        return ("theme", color.theme, round(color.tint or 0, 3))
    if color.type == "indexed":
        return ("indexed", color.indexed)
    return None


def _xlsx_grid(data: bytes) -> list[list[Cell]]:
    if len(data) > MAX_XLSX_BYTES:
        raise DutyError(f"The file is over {MAX_XLSX_BYTES // 1_000_000} MB.")
    from openpyxl import load_workbook

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_XLSX_UNZIPPED_BYTES:
                raise DutyError("The spreadsheet is too big to read.")
        book = load_workbook(io.BytesIO(data), data_only=True)
    except DutyError:
        raise
    except Exception as exc:  # openpyxl raises many kinds of errors on a bad file
        raise DutyError("The file isn't an Excel spreadsheet (.xlsx) that can be read.") from exc
    for sheet in book.worksheets:  # the first sheet with months across the top
        values: list[list[tuple[str, tuple | None]]] = []
        for row in sheet.iter_rows(max_row=min(sheet.max_row, 200), max_col=40):
            values.append([("" if c.value is None else str(c.value), _fill_key(c)) for c in row])
        texts = [[Cell(t) for t, _ in row] for row in values]
        found = _find_header(texts)
        if found is None:
            continue
        header = found[0]
        legend = next(
            (
                key
                for row in values
                for text, key in row
                if key and text.strip().lower() == HARD_LEGEND
            ),
            ("rgb", DEFAULT_HARD_FILL),
        )
        return [
            [Cell(text, hard=r > header and key == legend) for text, key in row]
            for r, row in enumerate(values)
        ]
    raise DutyError("No sheet in the file has the month names across the top.")


def is_xlsx(filename: str, data: bytes) -> bool:
    return filename.lower().endswith(".xlsx") or data[:4] == b"PK\x03\x04"


def parse_upload(filename: str, data: bytes, *, year: int | None = None) -> Parsed:
    """Read an upload: a month-grid calendar (Excel or CSV), or a CSV of duty rows."""
    year = year or local_today().year
    if is_xlsx(filename, data):
        return parse_grid(_xlsx_grid(data), year=year)
    text = _decode(data)
    grid = _csv_grid(text)
    if _find_header(grid) is not None:
        return parse_grid(grid, year=year)
    return Parsed(_parse_rows(text))


def _apply(duty: Duty, f: DutyFields, *, keep_owners: bool = False) -> None:
    duty.title = f.title
    if not keep_owners:
        duty.details = f.details
        duty.owners = list(f.owners)
    duty.first_due = f.first_due
    duty.repeat = f.repeat
    duty.remind_days = list(f.remind_days)


def import_rows(db: Session, rows: list[DutyFields], *, by: User) -> tuple[int, int]:
    """Add new duties and update ones with the same title. Returns (added, updated).

    A row from a month-grid calendar keeps an existing duty's owners and details.
    """
    existing = {d.title.lower(): d for d in db.scalars(select(Duty))}
    added = updated = 0
    for f in rows:
        duty = existing.get(f.title.lower())
        is_new = duty is None
        if duty is None:
            duty = Duty(created_by_id=by.id)
            db.add(duty)
            existing[f.title.lower()] = duty
            added += 1
        else:
            updated += 1
        # A calendar has no owners: it sets the date and reminders, and the owners and
        # details set on the Duties page stay.
        _apply(duty, f, keep_owners=f.from_grid and not is_new)
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
