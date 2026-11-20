"""Uploading a month-grid calendar (Excel or CSV) of yearly board and management duties.

The grid below is a real board calendar: months across the top, days down the side, one
task per cell, with hard deadlines filled orange in the spreadsheet.
"""

import calendar
import csv
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import PatternFill
from sqlalchemy import select

from app.models import Duty
from app.permissions import Role
from app.services import duties

# (month, day, task, hard deadline)
BOARD_CALENDAR = [
    (1, 1, "Get insurance renewal", False),
    (1, 5, "Sched. Yearly audit and tax filing", False),
    (1, 15, "Submit pool permit appl.", True),
    (1, 16, "New pool contract", False),
    (1, 17, "Confirm CE-200 filing waiver, www.wcb.ny.gov", False),
    (2, 1, "Sched. winter tree work", False),
    (3, 1, "Sched. Sewer cleaning", False),
    (4, 1, "Sched. Annual meet.", False),
    (4, 15, "Send out ann. Meet. Inv.", False),
    (4, 16, "Crab grass preventer", False),
    (4, 31, "Confirm insurance renewal", True),
    (5, 1, "Schedule pool opening", False),
    (5, 10, "Annual meeting", False),
    (6, 1, "Gutter cleaning", False),
    (8, 2, "Sched. tennis court repair", False),
    (8, 27, "Schedule pool closing", False),
    (9, 1, "Det. Prev. maint. Pool", False),
    (9, 5, "Pool water line emptying", False),
    (9, 18, "Sched. Arborist for winter", False),
    (9, 24, "Inspect clubhouse fireplace", False),
    (9, 30, "Letter to all about fireplaces", False),
    (10, 1, "Draft budget", False),
    (11, 1, "Distr. Draft budget to all", True),
    (11, 5, "Irrigation line emptying", False),
    (11, 24, "Holiday decorations", False),
    (12, 1, "Distr. final budget to all", True),
    (12, 2, "Distr. Maint. Update to all", True),
    (12, 4, "Gutter cleaning", False),
    (12, 15, "Mgt. Co to input maint. Increase for next year", True),
]
ORANGE = PatternFill("solid", fgColor="FFFFC000")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _grid(mark_hard: bool) -> list[list[str]]:
    """The sheet's layout: a title, the legend, the month row, then days 1 to 31."""
    rows = [[""] * 13 for _ in range(36)]
    rows[0][0] = "Board Calendar"
    rows[1][4] = "Hard Deadline"
    for m in range(1, 13):
        rows[4][m] = calendar.month_name[m]
    for day in range(1, 32):
        rows[4 + day][0] = str(day)
    for month, day, task, hard in BOARD_CALENDAR:
        rows[4 + day][month] = f"{task} !" if hard and mark_hard else task
    return rows


def calendar_csv() -> bytes:
    out = io.StringIO()
    csv.writer(out).writerows(_grid(mark_hard=True))
    return out.getvalue().encode()


def calendar_xlsx(vendors_first: bool = False) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Calendar"
    for r, row in enumerate(_grid(mark_hard=False), start=1):
        for c, value in enumerate(row, start=1):
            if value:
                sheet.cell(r, c, int(value) if c == 1 and r > 5 else value)
    sheet["E2"].fill = ORANGE
    for month, day, _, hard in BOARD_CALENDAR:
        if hard:
            sheet.cell(5 + day, 1 + month).fill = ORANGE
    vendors = book.create_sheet("Vendors", 0 if vendors_first else None)
    vendors.append(["Service", "Name", "Email", "Phone"])
    vendors.append(["Pool", "A pool company", "", ""])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _check_board_calendar(parsed: duties.Parsed) -> None:
    assert parsed.grid
    by_title = {f.title: f for f in parsed.rows}
    assert len(by_title) == 29
    assert all(f.repeat == "yearly" and f.first_due.year == 2026 for f in parsed.rows)

    # The same task twice a year becomes two duties.
    assert by_title["Gutter cleaning (Jun 1)"].first_due == date(2026, 6, 1)
    assert by_title["Gutter cleaning (Dec 4)"].first_due == date(2026, 12, 4)

    # April 31 isn't a date: the last day of April, with a warning.
    assert by_title["Confirm insurance renewal"].first_due == date(2026, 4, 30)
    assert parsed.warnings == ["Apr 31 isn't a date, so Apr 30 is used."]

    hard = {f.title for f in parsed.rows if f.remind_days == [14, 7, 0]}
    assert hard == {
        "Submit pool permit appl.",
        "Confirm insurance renewal",
        "Distr. Draft budget to all",
        "Distr. final budget to all",
        "Distr. Maint. Update to all",
        "Mgt. Co to input maint. Increase for next year",
    }
    assert all(f.remind_days == [7, 0] for f in parsed.rows if f.title not in hard)

    management = {f.title for f in parsed.rows if f.owners == ["management"]}
    assert management == {"Mgt. Co to input maint. Increase for next year"}
    assert all(f.owners == ["board"] for f in parsed.rows if f.title not in management)


def test_csv_calendar():
    _check_board_calendar(duties.parse_upload("calendar.csv", calendar_csv(), year=2026))


def test_excel_calendar_reads_orange_as_hard_deadline_and_skips_other_sheets():
    for vendors_first in (False, True):
        data = calendar_xlsx(vendors_first=vendors_first)
        _check_board_calendar(duties.parse_upload("calendar.xlsx", data, year=2026))


def test_hard_deadline_color_comes_from_the_legend():
    book = Workbook()
    sheet = book.active
    green = PatternFill("solid", fgColor="FF00B050")
    sheet["A1"] = "Hard Deadline"
    sheet["A1"].fill = green
    for m in range(1, 13):
        sheet.cell(2, 1 + m, calendar.month_abbr[m])
    sheet["A3"], sheet["B3"], sheet["C3"] = 1, "Pay the tax bill", "Plant bulbs"
    sheet["B3"].fill = green
    sheet["C3"].fill = ORANGE  # not the legend's color, so not a hard deadline
    out = io.BytesIO()
    book.save(out)
    parsed = duties.parse_upload("cal.xlsx", out.getvalue(), year=2027)
    days = {f.title: f.remind_days for f in parsed.rows}
    assert days == {"Pay the tax bill": [14, 7, 0], "Plant bulbs": [7, 0]}


def test_calendar_cells_with_two_lines_are_two_tasks():
    text = (
        ",Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sept,Oct,Nov,Dec\n"
        '3rd,,"Walk the grounds\n!File the report",\n'
    )
    parsed = duties.parse_upload("cal.csv", text.encode(), year=2027)
    assert [(f.title, f.first_due, f.remind_days) for f in parsed.rows] == [
        ("File the report", date(2027, 2, 3), [14, 7, 0]),
        ("Walk the grounds", date(2027, 2, 3), [7, 0]),
    ]


def test_calendar_task_without_a_day_saves_nothing():
    text = ",January,February,March,April,May,June\n1,Do this\n,Do that\n"
    try:
        duties.parse_upload("cal.csv", text.encode(), year=2027)
    except duties.DutyError as exc:
        assert exc.errors == ["Row 3 has tasks but no day of the month (1 to 31) beside them."]
    else:
        raise AssertionError("expected an error")


def test_not_a_spreadsheet():
    for name, data in (("cal.xlsx", b"PK\x03\x04 broken"), ("cal.xlsx", b"hello")):
        try:
            duties.parse_upload(name, data, year=2027)
        except duties.DutyError as exc:
            assert "Excel" in str(exc)
        else:
            raise AssertionError("expected an error")


def test_excel_without_a_month_row():
    book = Workbook()
    book.active.append(["title", "owners"])
    out = io.BytesIO()
    book.save(out)
    try:
        duties.parse_upload("x.xlsx", out.getvalue(), year=2027)
    except duties.DutyError as exc:
        assert "month names" in str(exc)
    else:
        raise AssertionError("expected an error")


def test_row_csv_still_works():
    parsed = duties.parse_upload("d.csv", duties.template_csv().encode(), year=2027)
    assert not parsed.grid and len(parsed.rows) == 3


def _upload(client, name, data, kind):
    return client.post_form("/duties/upload", files={"file": (name, io.BytesIO(data), kind)})


def test_uploading_again_keeps_owners_and_details_set_on_the_page(client_for, db):
    board = client_for(Role.BOARD, "board@example.com")
    resp = _upload(board, "Board Calendar.xlsx", calendar_xlsx(), XLSX)
    assert resp.status_code == 303
    page = board.get("/duties").text
    assert "29 added, 0 updated" in page and "Apr 31 isn" in page
    db.expire_all()
    assert len(db.scalars(select(Duty)).all()) == 29

    duty = db.scalar(select(Duty).where(Duty.title == "Draft budget"))
    resp = board.post_form(
        f"/duties/{duty.id}",
        data={
            "title": "Draft budget",
            "details": "Start from last year's actuals.",
            "owners": "treasurer@example.org; board",
            "first_due": duty.first_due.isoformat(),
            "repeat": "yearly",
            "remind_days": "30; 7",
        },
    )
    assert resp.status_code == 303

    resp = _upload(board, "calendar.csv", calendar_csv(), "text/csv")
    assert resp.status_code == 303
    assert "0 added, 29 updated" in board.get("/duties").text
    db.expire_all()
    duty = db.get(Duty, duty.id)
    assert duty.owners == ["treasurer@example.org", "board"]
    assert duty.details == "Start from last year's actuals."
    assert duty.remind_days == [7, 0]  # the calendar sets the reminders
    assert len(db.scalars(select(Duty)).all()) == 29
