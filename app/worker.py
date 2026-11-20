"""Background worker: runs the weekly review, delivers the email outbox, syncs the Dropbox
folder and prunes expired rows.

Run: python -m app.worker            (loops forever)
     python -m app.worker --once     (one pass, e.g. from cron)
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import delete

from app.config import get_settings
from app.db import session_factory, utcnow
from app.email.outbox import deliver_pending
from app.models import EmailMessage, LoginAttempt, PasswordReset, UserSession

log = logging.getLogger("app.worker")
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def prune(db) -> None:
    now = utcnow()
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    db.execute(delete(PasswordReset).where(PasswordReset.expires_at < now - timedelta(days=1)))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < now - timedelta(days=30)))
    # Delivered email after 30 days. Announcements' messages stay: their page counts them.
    db.execute(
        delete(EmailMessage).where(
            EmailMessage.status == "sent",
            EmailMessage.blast_id.is_(None),
            EmailMessage.sent_at < now - timedelta(days=30),
        )
    )
    db.commit()


_last_sync = 0.0
_last_review = 0.0
REVIEW_CHECK_EVERY_SECONDS = 15 * 60


def maybe_run_weekly_review(db, now: float, *, force: bool = False) -> None:
    """Run the weekly review once it's due (checked every 15 minutes, or now when forced)."""
    global _last_review
    if not force and _last_review and now - _last_review < REVIEW_CHECK_EVERY_SECONDS:
        return
    _last_review = now
    from app.services import weekly_review

    try:
        queued = weekly_review.run_if_due(db)
    except Exception:  # a bad review mustn't stop email delivery
        db.rollback()
        log.exception("weekly review failed")
        return
    if queued:
        log.info("weekly review: queued %d emails", queued)


def maybe_sync(db, now: float, *, force: bool = False) -> None:
    """Sync the Dropbox folder every DROPBOX_SYNC_MINUTES (several workers are safe)."""
    global _last_sync
    s = get_settings()
    if s.document_source != "dropbox":
        return
    if not force and now - _last_sync < s.dropbox_sync_minutes * 60:
        return
    _last_sync = now
    from app.services import dropbox_sync

    try:
        result = dropbox_sync.sync(db)
    except dropbox_sync.SyncBusy:
        return
    if result.error:
        log.error("dropbox sync: %s", result.error)
    else:
        log.info("dropbox sync: %s", result.summary())


def run_once(*, force_sync: bool = False) -> int:
    """One pass. force_sync syncs Dropbox now, e.g. from a schedule that runs hourly."""
    db = session_factory()()
    try:
        maybe_run_weekly_review(db, time.monotonic(), force=force_sync)
        sent = deliver_pending(db)
        maybe_sync(db, time.monotonic(), force=force_sync)
        prune(db)
        return sent
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        log.info("sent %d emails", run_once())
        return
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    log.info("worker started")
    while not _stop:
        try:
            sent = run_once()
            if sent:
                log.info("sent %d emails", sent)
        except Exception:
            log.exception("worker pass failed")
        time.sleep(args.interval)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
