"""Append-only audit log for security-relevant and governance actions."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditLog, User


def audit(
    db: Session,
    action: str,
    *,
    actor: User | None = None,
    target_type: str = "",
    target_id: Any = "",
    request: Request | None = None,
    **details: Any,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id != "" else "",
        details=details,
        ip=(request.client.host if request is not None and request.client else ""),
    )
    db.add(entry)
    return entry
