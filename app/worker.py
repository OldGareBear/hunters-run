"""Background worker: delivers the email outbox and prunes expired rows.

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

from app.db import session_factory, utcnow
from app.email.outbox import deliver_pending
from app.models import LoginAttempt, PasswordReset, UserSession

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
    db.commit()


def run_once() -> int:
    db = session_factory()()
    try:
        sent = deliver_pending(db)
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
