"""AWS Lambda entry point.

The same container image backs two functions (see infra/app):

- web: API Gateway sends HTTP requests here; Mangum runs them through the FastAPI app.
- tasks: background work, started by the hourly schedule, by the web function
  (email right after an action, "Check Dropbox now") or by the deploy workflow
  (database migrations). Its events look like {"task": "worker"}.

Only AWS principals with lambda:InvokeFunction can send a task event; an HTTP
request through API Gateway always arrives in the API Gateway event format.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("app.lambda")
logging.getLogger().setLevel(logging.INFO)

_http_handler = None


def _web(event: dict, context: Any) -> dict:
    global _http_handler
    if _http_handler is None:
        from mangum import Mangum

        from app.main import app

        _http_handler = Mangum(app, lifespan="off")
    return _http_handler(event, context)


def _migrate() -> str:
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, pool

    from app.config import get_settings

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))

    # After a rollback, the database can be at a revision this (older) code doesn't have.
    # Migrations must work with the previous release (DEPLOY.md), so leave the schema be;
    # upgrading would fail and stop the deploy of the rollback.
    known = {r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()}
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_heads()
    finally:
        engine.dispose()
    newer = sorted(r for r in current if r not in known)
    if newer:
        msg = f"database is at {', '.join(newer)}, newer than this code; schema left as it is"
        log.warning(msg)
        return msg
    command.upgrade(cfg, "head")
    return "migrated"


def _outbox() -> str:
    from app.db import session_factory
    from app.email.outbox import deliver_pending

    db = session_factory()()
    try:
        return f"sent {deliver_pending(db)}"
    finally:
        db.close()


def _dropbox_sync(actor_id: int | None = None) -> str:
    from app.routes.documents import _run_sync

    _run_sync(actor_id)
    return "synced"


def _worker() -> str:
    from app.worker import run_once

    return f"sent {run_once(force_sync=True)}"


def run_task(event: dict) -> dict:
    task = event.get("task")
    if task == "migrate":
        result = _migrate()
    elif task == "outbox":
        result = _outbox()
    elif task == "dropbox-sync":
        actor = event.get("actor_id")
        result = _dropbox_sync(int(actor) if actor else None)
    elif task == "worker":
        result = _worker()
    else:
        raise ValueError(f"unknown task {task!r}")
    log.info("task %s: %s", task, result)
    return {"task": task, "result": result}


def handler(event: Any, context: Any) -> Any:
    if isinstance(event, dict) and "task" in event:
        return run_task(event)
    return _web(event, context)
