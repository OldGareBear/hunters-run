"""The inbox people cc: emails that land in it become a source for the weekly review.

The HOA keeps a dedicated free Gmail account (INBOX_ADDRESS). Board and management
members add it as a recipient on any chain the weekly review should know about. The
portal reads it over IMAP with an app password, on a schedule (INBOX_FETCH_HOURS) and
just before each weekly review.

What it keeps, and what it never does:

- It opens the mailbox read-only (IMAP EXAMINE) and fetches with BODY.PEEK, so nothing
  in the inbox is marked read, flagged, moved or deleted. The only IMAP commands it
  sends are LOGIN, EXAMINE, SEARCH, FETCH and LOGOUT. It never sends from the account.
- A message is kept only if the inbox's own mail server (INBOX_AUTHSERV_ID, the topmost
  Authentication-Results header) says SPF or DKIM passed for the From address's domain,
  and the From address is an active board, admin or management user. Anything else
  (a vendor's reply, a forged sender, a mailing list) is skipped and not stored.
- Only the text is kept: the plain-text part (or the HTML part as text), no attachments,
  capped at INBOX_MAX_BODY_CHARS. Messages over INBOX_MAX_MESSAGE_MB aren't downloaded.
- Each message is kept once, by its Message-ID, and deleted INBOX_RETENTION_DAYS later.
- Who may see it: board members and admins always; management only when a management
  user or a MANAGEMENT_NOTIFY_EMAILS address was on the chain. Homeowners never.
- Email text is data. The weekly review treats it like any document.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage as Mime
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import InboundEmail, SyncState, User, as_utc
from app.permissions import Role

log = logging.getLogger(__name__)

SOURCE = "inbox"
LOCK_FOR = timedelta(minutes=10)
STAFF_ROLES = (Role.ADMIN.value, Role.BOARD.value, Role.MANAGEMENT.value)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class InboxError(Exception):
    """The inbox couldn't be read (bad password, network, server)."""


# --- Reading the mailbox -------------------------------------------------------------


class Mailbox(Protocol):
    def recent(self, since: date) -> list[tuple[str, str, int]]:
        """(uid, Message-ID, size in bytes) for messages that arrived on or after `since`."""
        ...

    def fetch(self, uid: str) -> bytes: ...

    def close(self) -> None: ...


class GmailMailbox:
    """The inbox over IMAP, read-only."""

    ALLOWED = frozenset({"SEARCH", "FETCH"})

    def __init__(self) -> None:
        s = get_settings()
        try:
            self.conn = imaplib.IMAP4_SSL(
                s.inbox_imap_host,
                s.inbox_imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=60,
            )
            self.conn.login(s.inbox_address, s.inbox_password)
            typ, _ = self.conn.select("INBOX", readonly=True)  # EXAMINE
        except (imaplib.IMAP4.error, OSError) as exc:
            raise InboxError(f"couldn't open the inbox: {exc}") from exc
        if typ != "OK":
            raise InboxError("couldn't open the inbox")

    def _uid(self, command: str, *args):
        if command not in self.ALLOWED:  # nothing that could change the mailbox
            raise ValueError(f"IMAP {command} isn't allowed")
        try:
            typ, data = self.conn.uid(command, *args)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise InboxError(f"reading the inbox failed: {exc}") from exc
        if typ != "OK":
            raise InboxError(f"reading the inbox failed ({command})")
        return data

    def recent(self, since: date) -> list[tuple[str, str, int]]:
        when = f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}"
        data = self._uid("SEARCH", None, "SINCE", when)
        uids = (data[0] or b"").split()
        out: list[tuple[str, str, int]] = []
        for i in range(0, len(uids), 100):
            batch = b",".join(uids[i : i + 100]).decode()
            data = self._uid(
                "FETCH", batch, "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
            )
            for part in data:
                if not isinstance(part, tuple):
                    continue
                meta = part[0].decode(errors="replace")
                uid = re.search(r"UID (\d+)", meta)
                size = re.search(r"RFC822\.SIZE (\d+)", meta)
                if not uid:
                    continue
                headers = BytesHeaderParser(policy=policy.compat32).parsebytes(part[1])
                message_id = (headers.get("Message-ID") or "").strip()
                out.append((uid.group(1), message_id, int(size.group(1)) if size else 0))
        return out

    def fetch(self, uid: str) -> bytes:
        data = self._uid("FETCH", uid, "(BODY.PEEK[])")
        for part in data:
            if isinstance(part, tuple):
                return part[1]
        raise InboxError(f"message {uid} came back empty")

    def close(self) -> None:
        try:
            self.conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


# --- Checking a message ----------------------------------------------------------------

_COMMENT = re.compile(r"\([^()]*\)")


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().strip(".").lower() if "@" in address else ""


def _aligned(a: str, b: str) -> bool:
    """Same domain, or one a subdomain of the other (relaxed alignment)."""
    a, b = a.lower().strip("."), b.lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def auth_results(header: str) -> tuple[str, list[dict[str, str]]]:
    """An Authentication-Results header as (authserv-id, [{method, result, props...}])."""
    text = header
    while _COMMENT.search(text):
        text = _COMMENT.sub(" ", text)
    parts = [p.strip() for p in text.split(";")]
    serv = parts[0].split()[0].lower() if parts and parts[0].split() else ""
    results = []
    for part in parts[1:]:
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, result = tokens[0].split("=", 1)
        entry = {"method": method.lower(), "result": result.lower()}
        for t in tokens[1:]:
            if "=" in t:
                k, v = t.split("=", 1)
                entry[k.lower()] = v.strip('"').lower()
        results.append(entry)
    return serv, results


def authenticated(msg: Mime, from_address: str) -> bool:
    """SPF or DKIM passed for the From domain, per the inbox's own mail server.

    Only the topmost Authentication-Results header counts, and only if the inbox's server
    wrote it: a sender can add headers of their own further down."""
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False
    serv, results = auth_results(str(headers[0]))
    if serv != get_settings().inbox_authserv_id.lower():
        return False
    domain = _domain(from_address)
    for r in results:
        if r["result"] != "pass":
            continue
        if r["method"] == "dkim":
            signer = r.get("header.d") or _domain(r.get("header.i", ""))
            if _aligned(signer, domain):
                return True
        elif r["method"] == "spf":
            mailfrom = r.get("smtp.mailfrom", "")
            if _aligned(_domain(mailfrom) or mailfrom, domain):
                return True
    return False


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return "".join(parser.out)


def body_text(msg: Mime, limit: int) -> str:
    """The message's text: the first plain-text part, else the HTML part as text. Never
    an attachment."""
    plain = html = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        kind = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        if not isinstance(content, str):
            continue
        if kind == "text/plain" and plain is None:
            plain = content
        elif kind == "text/html" and html is None:
            html = content
    text = plain if plain is not None else _html_text(html or "")
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out[:limit]


@dataclass
class Checked:
    keep: bool
    reason: str = ""  # why it was skipped
    message_id: str = ""
    from_email: str = ""
    from_name: str = ""
    sender: User | None = None
    subject: str = ""
    body: str = ""
    audience: str = "board"
    sent_at: datetime | None = None


def _staff(db: Session) -> dict[str, User]:
    users = db.scalars(select(User).where(User.is_active.is_(True), User.role.in_(STAFF_ROLES)))
    return {u.email.lower(): u for u in users}


def check(raw: bytes, staff: dict[str, User]) -> Checked:
    """Decide whether to keep one message, and what to keep of it."""
    s = get_settings()
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id or len(message_id) > 500:
        return Checked(False, "no Message-ID")
    froms = msg.get_all("From") or []
    addresses = getaddresses([str(f) for f in froms])
    if len(froms) != 1 or len(addresses) != 1:
        return Checked(False, "not one sender", message_id)
    name, address = addresses[0][0], addresses[0][1].strip().lower()
    if (msg.get("Auto-Submitted") or "no").strip().lower() != "no":
        return Checked(False, "an automatic message", message_id)
    sender = staff.get(address)
    if sender is None:
        return Checked(False, "not from a board or management member", message_id)
    if not authenticated(msg, address):
        return Checked(False, "the sender couldn't be verified", message_id)
    on_chain = {
        a.strip().lower()
        for _, a in getaddresses([str(v) for h in ("To", "Cc") for v in msg.get_all(h) or []])
    }
    on_chain.add(address)
    management = {e for e, u in staff.items() if u.role == Role.MANAGEMENT.value}
    management |= {e.lower() for e in s.management_notify_list}
    try:
        sent_at = as_utc(parsedate_to_datetime(str(msg.get("Date", ""))))
    except (TypeError, ValueError):
        sent_at = None
    return Checked(
        True,
        message_id=message_id,
        from_email=address,
        from_name=(name or sender.display_name)[:200],
        sender=sender,
        subject=" ".join(str(msg.get("Subject", "")).split())[:500],
        body=body_text(msg, s.inbox_max_body_chars),
        audience="all" if on_chain & management else "board",
        sent_at=sent_at,
    )


# --- Fetching ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    looked_at: int = 0
    kept: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        if self.error:
            return f"failed: {self.error}"
        skipped = ", ".join(f"{n} {r}" for r, n in sorted(self.skipped.items()))
        out = f"{self.kept} kept of {self.looked_at} new"
        return f"{out} (skipped: {skipped})" if skipped else out


def _claim(db: Session) -> bool:
    now = utcnow()
    if db.get(SyncState, SOURCE) is None:
        db.add(SyncState(source=SOURCE, last_summary={}, last_error=""))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    claimed = db.execute(
        update(SyncState)
        .where(SyncState.source == SOURCE)
        .where((SyncState.locked_until.is_(None)) | (SyncState.locked_until < now))
        .values(locked_until=now + LOCK_FOR, last_started_at=now)
    ).rowcount
    db.commit()
    return bool(claimed)


def _release(db: Session, result: FetchResult) -> None:
    db.execute(
        update(SyncState)
        .where(SyncState.source == SOURCE)
        .values(
            locked_until=None,
            last_finished_at=utcnow(),
            last_summary=asdict(result),
            last_error=result.error,
        )
    )
    db.commit()


def last_state(db: Session) -> SyncState | None:
    return db.get(SyncState, SOURCE)


def due(db: Session, now: datetime | None = None) -> bool:
    """Whether the scheduled read is due (INBOX_FETCH_HOURS since the last one)."""
    s = get_settings()
    if not s.inbox_enabled or not s.inbox_fetch_hours:
        return False
    state = last_state(db)
    last = as_utc(state.last_finished_at) if state else None
    now = now or utcnow()
    return last is None or now - last >= timedelta(hours=s.inbox_fetch_hours)


def fetch(db: Session, *, mailbox: Mailbox | None = None) -> FetchResult | None:
    """Read new messages from the inbox and keep the ones that pass the checks.

    Returns None when the inbox is off or another worker is reading it."""
    s = get_settings()
    if not s.inbox_enabled:
        return None
    if not _claim(db):
        return None
    result = FetchResult()
    box: Mailbox | None = None
    try:
        box = mailbox or GmailMailbox()
        since = (utcnow() - timedelta(days=s.inbox_lookback_days)).date()
        known = set(
            db.scalars(
                select(InboundEmail.message_id).where(
                    InboundEmail.received_at >= utcnow() - timedelta(days=s.inbox_lookback_days + 2)
                )
            )
        )
        staff = _staff(db)
        max_bytes = s.inbox_max_message_mb * 1024 * 1024
        for uid, message_id, size in box.recent(since):
            if message_id and message_id in known:
                continue
            result.looked_at += 1
            if size > max_bytes:
                result.skip("too big")
                continue
            checked = check(box.fetch(uid), staff)
            if not checked.keep:
                result.skip(checked.reason)
                continue
            if checked.message_id in known or db.scalar(
                select(InboundEmail.id).where(InboundEmail.message_id == checked.message_id)
            ):
                known.add(checked.message_id)
                continue
            try:
                with db.begin_nested():
                    db.add(
                        InboundEmail(
                            message_id=checked.message_id,
                            from_email=checked.from_email,
                            from_name=checked.from_name,
                            sender_id=checked.sender.id if checked.sender else None,
                            subject=checked.subject,
                            body=checked.body,
                            audience=checked.audience,
                            sent_at=checked.sent_at,
                        )
                    )
            except IntegrityError:  # kept by another worker meanwhile
                continue
            known.add(checked.message_id)
            result.kept += 1
        db.commit()
    except InboxError as exc:
        db.rollback()
        result.error = str(exc)[:500]
    except Exception as exc:
        db.rollback()
        log.exception("inbox: reading failed")
        result.error = f"reading failed: {type(exc).__name__}"
    finally:
        if box is not None:
            box.close()
        _release(db, result)
    if result.error:
        log.error("inbox: %s", result.error)
    else:
        log.info("inbox: %s", result.summary())
    return result


def prune(db: Session) -> None:
    """Delete kept emails INBOX_RETENTION_DAYS after they were read in."""
    cutoff = utcnow() - timedelta(days=get_settings().inbox_retention_days)
    db.execute(delete(InboundEmail).where(InboundEmail.received_at < cutoff))


# --- Who sees what ----------------------------------------------------------------------


def may_see(user: User | None, email: InboundEmail) -> bool:
    if user is None or not user.is_active:
        return False
    if user.role in (Role.ADMIN.value, Role.BOARD.value):
        return True
    return user.role == Role.MANAGEMENT.value and email.audience == "all"


def visible_audiences(role: str) -> tuple[str, ...]:
    """The `audience` values a role may see."""
    if role in (Role.ADMIN.value, Role.BOARD.value):
        return ("all", "board")
    if role == Role.MANAGEMENT.value:
        return ("all",)
    return ()


def recent(db: Session, role: str, since: datetime) -> list[InboundEmail]:
    """Kept emails a role may see, sent (or read in) since `since`, newest first."""
    audiences = visible_audiences(role)
    if not audiences:
        return []
    rows = db.scalars(
        select(InboundEmail)
        .where(InboundEmail.audience.in_(audiences), InboundEmail.received_at >= since)
        .order_by(InboundEmail.received_at.desc())
    ).all()
    return sorted(rows, key=lambda e: as_utc(e.sent_at or e.received_at), reverse=True)
