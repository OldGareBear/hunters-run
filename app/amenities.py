"""Bookable amenities and their rules, read from the AMENITIES setting.

AMENITIES is a JSON object of amenity groups. Each group lists its spaces (the
things you actually book, like "Tennis court 1") and the rules that apply to it.
Limits are per household and count across every space in the group, so a
household can't get around them by booking the other court.

Any rule left out takes the default in `Rules` below. A limit set to null means
"no limit". Unknown keys are rejected so a typo can't silently do nothing.

Example:

    {
      "tennis": {
        "label": "Tennis courts",
        "spaces": {"tennis-1": "Tennis court 1", "tennis-2": "Tennis court 2"},
        "max_minutes": 90,
        "gap_minutes": 120
      },
      "clubhouse": {
        "label": "Clubhouse",
        "spaces": {"clubhouse": "Clubhouse"},
        "requires_approval": true,
        "ask_purpose": true,
        "slot_minutes": 60,
        "min_minutes": 120,
        "max_minutes": 480
      }
    }

This module imports nothing from the app so settings can validate it at start-up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from datetime import time

RESERVED_SLUGS = {"manage", "r", "new"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}$")


@dataclass(frozen=True)
class Rules:
    # When the space can be used, in the HOA's local time zone (HOA_TIMEZONE).
    open: str = "07:00"
    close: str = "21:00"
    # Bookings start on this grid and last a multiple of it.
    slot_minutes: int = 30
    min_minutes: int = 60
    max_minutes: int = 90
    # How far ahead people may book (calendar days, today = 0) and the least notice.
    book_ahead_days: int = 7
    min_notice_minutes: int = 0
    # Per-household limits, counted across every space in the group.
    max_upcoming: int | None = 2  # active bookings that haven't ended yet
    max_per_day: int | None = 2
    max_per_week: int | None = 4  # Monday to Sunday
    # No back-to-back play: a household's bookings must be at least this far apart.
    gap_minutes: int = 120
    # A booking the household cancels this close to its start still counts toward
    # the daily and weekly limits, so holding a slot and dropping it late doesn't pay.
    late_cancel_minutes: int = 120
    # Requests wait for management to approve them (clubhouse rentals).
    requires_approval: bool = False
    # Ask what the booking is for (shown to management only).
    ask_purpose: bool = False


GROUP_KEYS = {"label", "spaces", "description", "notes"} | {f.name for f in fields(Rules)}


@dataclass(frozen=True)
class AmenityGroup:
    key: str
    label: str
    spaces: dict[str, str]  # slug -> display name, in order
    rules: Rules
    description: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def open_time(self) -> time:
        return _hhmm(self.rules.open)

    @property
    def close_time(self) -> time:
        return _hhmm(self.rules.close)

    def durations(self) -> list[int]:
        r = self.rules
        return list(range(r.min_minutes, r.max_minutes + 1, r.slot_minutes))


DEFAULT_AMENITIES = {
    "tennis": {
        "label": "Tennis court",
        "spaces": {"tennis": "Tennis court"},
        "description": "Book up to 90 minutes. Bookings are confirmed straight away.",
        "notes": [
            "The court is for tennis and basketball only.",
            "No bikes, skates or pets on the court.",
        ],
        # One court is about 14 hours of play a day, so one booking a day per household
        # keeps evenings and weekends from going to the same few households.
        "max_per_day": 1,
        "max_per_week": 3,
    },
    "clubhouse": {
        "label": "Clubhouse",
        "spaces": {"clubhouse": "Clubhouse"},
        "description": (
            "Request the clubhouse for a private event. Management reviews each request "
            "and will confirm it, along with any fees and rules."
        ),
        "requires_approval": True,
        "ask_purpose": True,
        "open": "08:00",
        "close": "23:00",
        "slot_minutes": 60,
        "min_minutes": 120,
        "max_minutes": 480,
        "book_ahead_days": 180,
        "min_notice_minutes": 14 * 24 * 60,
        "max_upcoming": 1,
        "max_per_day": 1,
        "max_per_week": None,
        "gap_minutes": 0,
        "late_cancel_minutes": 0,
    },
}


def _hhmm(value: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m:
        raise ValueError(f"expected a time like 07:00, got {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour == 24 and minute == 0:
        return time(23, 59, 59)  # "24:00" = end of day
    return time(hour, minute)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute + (1 if t.second else 0)


def parse_amenities(raw: str) -> list[AmenityGroup]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("AMENITIES must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("AMENITIES must be a JSON object of amenity groups")
    groups: list[AmenityGroup] = []
    seen: set[str] = set()
    for key, cfg in data.items():
        where = f"AMENITIES[{key!r}]"
        if not SLUG_RE.match(str(key)):
            raise ValueError(f"{where}: group names use lower-case letters, digits and dashes")
        if not isinstance(cfg, dict):
            raise ValueError(f"{where} must be an object")
        unknown = set(cfg) - GROUP_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown setting(s) {sorted(unknown)}")
        spaces = cfg.get("spaces")
        if not isinstance(spaces, dict) or not spaces:
            raise ValueError(f"{where}: 'spaces' must map slugs to names")
        for slug, name in spaces.items():
            if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
                raise ValueError(f"{where}: {slug!r} can't be used as a space slug")
            if slug in seen:
                raise ValueError(f"{where}: space {slug!r} is listed twice")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{where}: space {slug!r} needs a name")
            seen.add(slug)
        rule_values = {k: v for k, v in cfg.items() if k in {f.name for f in fields(Rules)}}
        try:
            rules = Rules(**rule_values)
        except TypeError as exc:  # pragma: no cover - keys are checked above
            raise ValueError(f"{where}: {exc}") from exc
        _check_rules(where, rules)
        notes = cfg.get("notes", [])
        if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise ValueError(f"{where}: 'notes' must be a list of sentences")
        groups.append(
            AmenityGroup(
                key=key,
                label=str(cfg.get("label") or key),
                spaces={k: v.strip() for k, v in spaces.items()},
                rules=rules,
                description=str(cfg.get("description", "")),
                notes=tuple(notes),
            )
        )
    return groups


def _check_rules(where: str, r: Rules) -> None:
    def positive(name: str, value, allow_none=False, allow_zero=False):
        if value is None and allow_none:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}: {name} must be a whole number")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{where}: {name} must be more than 0")

    for flag in ("requires_approval", "ask_purpose"):
        if not isinstance(getattr(r, flag), bool):
            raise ValueError(f"{where}: {flag} must be true or false")
    positive("slot_minutes", r.slot_minutes)
    positive("min_minutes", r.min_minutes)
    positive("max_minutes", r.max_minutes)
    positive("book_ahead_days", r.book_ahead_days, allow_zero=True)
    positive("min_notice_minutes", r.min_notice_minutes, allow_zero=True)
    positive("gap_minutes", r.gap_minutes, allow_zero=True)
    positive("late_cancel_minutes", r.late_cancel_minutes, allow_zero=True)
    for name in ("max_upcoming", "max_per_day", "max_per_week"):
        positive(name, getattr(r, name), allow_none=True)
    try:
        opens, closes = _minutes(_hhmm(r.open)), _minutes(_hhmm(r.close))
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    if closes <= opens:
        raise ValueError(f"{where}: close must be after open")
    if r.min_minutes % r.slot_minutes or r.max_minutes % r.slot_minutes:
        raise ValueError(f"{where}: min_minutes and max_minutes must be multiples of slot_minutes")
    if r.max_minutes < r.min_minutes:
        raise ValueError(f"{where}: max_minutes must be at least min_minutes")
    if r.min_minutes > closes - opens:
        raise ValueError(f"{where}: min_minutes is longer than the opening hours")
