"""Keep the document library in step with the board's Dropbox folder.

Dropbox is the source of truth. Each run lists every file under DROPBOX_ROOT and:
- adds and indexes new files in a shared subfolder (see DROPBOX_FOLDER_ROLES),
- re-indexes files whose contents changed,
- updates who can see a file when it is moved to another subfolder,
- removes documents whose file was deleted or moved out of every shared folder.

Files are tracked by their Dropbox id, which survives renames and moves. The
portal keeps no copy of the file itself: downloads are fetched from Dropbox at
request time, after the portal's own permission check.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import get_settings
from app.db import utcnow
from app.dropbox import DropboxClient, DropboxError, DropboxFile, get_dropbox
from app.ingest.extract import EXTENSIONS, ExtractionError, sniff_content_type
from app.models import Document, DocumentAccess, SyncState, User
from app.services.documents import index_document, soft_delete

log = logging.getLogger(__name__)
SOURCE = "dropbox"
LOCK_FOR = timedelta(minutes=30)


class SyncBusy(Exception):
    """Another sync is already running."""


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    access_changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[dict] = field(default_factory=list)  # {"path", "reason"}
    failed: list[dict] = field(default_factory=list)  # {"path", "reason"}
    error: str = ""

    def summary(self) -> str:
        parts = [
            f"{len(self.added)} added",
            f"{len(self.updated)} updated",
            f"{len(self.access_changed)} moved between folders",
            f"{len(self.removed)} removed",
        ]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.failed:
            parts.append(f"{len(self.failed)} couldn't be read")
        return ", ".join(parts)


def roles_for(path_lower: str) -> list[str] | None:
    """Groups who may see a file at this Dropbox path, or None if it isn't shared."""
    s = get_settings()
    root = "/".join(p for p in s.dropbox_root.lower().split("/") if p)
    parts = [p for p in path_lower.split("/") if p]
    root_parts = root.split("/") if root else []
    if parts[: len(root_parts)] != root_parts:
        return None
    folder = "/".join(parts[len(root_parts) : -1])  # path of the containing folder
    for prefix, roles in s.dropbox_folder_map:  # deepest first
        if folder == prefix or folder.startswith(prefix + "/"):
            return roles
    return None


def _acquire(db: Session) -> bool:
    now = utcnow()
    if db.get(SyncState, SOURCE) is None:
        db.add(SyncState(source=SOURCE, last_summary={}, last_error=""))
        db.commit()
    claimed = db.execute(
        update(SyncState)
        .where(SyncState.source == SOURCE)
        .where((SyncState.locked_until.is_(None)) | (SyncState.locked_until < now))
        .values(locked_until=now + LOCK_FOR, last_started_at=now)
    ).rowcount
    db.commit()
    return bool(claimed)


def _release(db: Session, result: SyncResult) -> None:
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


def _renew(db: Session) -> None:
    db.execute(
        update(SyncState).where(SyncState.source == SOURCE).values(locked_until=utcnow() + LOCK_FOR)
    )


def last_state(db: Session) -> SyncState | None:
    return db.get(SyncState, SOURCE)


def _title(name: str) -> str:
    return PurePosixPath(name).stem.replace("_", " ").strip()[:300] or name[:300]


def _set_access(doc: Document, roles: list[str]) -> None:
    doc.access = [DocumentAccess(role=r) for r in roles]


def _ingest(db: Session, doc: Document, f: DropboxFile, client: DropboxClient) -> None:
    data = client.download(f.id)
    doc.content_type = sniff_content_type(f.name, data, None)
    doc.size_bytes = len(data)
    doc.sha256 = hashlib.sha256(data).hexdigest()
    doc.source_rev = f.content_hash
    doc.status = "processing"
    db.flush()
    index_document(db, doc, data)


def sync(
    db: Session, *, actor: User | None = None, client: DropboxClient | None = None
) -> SyncResult:
    s = get_settings()
    if not _acquire(db):
        raise SyncBusy("A Dropbox sync is already running.")
    result = SyncResult()
    try:
        client = client or get_dropbox()
        files = list(client.list_files(s.dropbox_root))
        existing = {
            d.source_id: d for d in db.scalars(select(Document).where(Document.source == SOURCE))
        }
        active = [d for d in existing.values() if d.deleted_at is None]
        if not files and active:
            # An empty listing more likely means a wrong folder or lost access than a
            # board that deleted everything. Don't wipe the library on that signal.
            result.error = (
                f"The Dropbox folder {s.dropbox_root} looks empty, so nothing was removed. "
                "Check DROPBOX_ROOT and the app's access."
            )
            return result

        max_bytes = s.dropbox_max_file_mb * 1024 * 1024
        seen: set[str] = set()
        for f in files:
            roles = roles_for(f.path_lower)
            if roles is None:
                result.skipped.append({"path": f.path_display, "reason": "not in a shared folder"})
                continue
            if PurePosixPath(f.name).suffix.lower() not in EXTENSIONS:
                result.skipped.append({"path": f.path_display, "reason": "unsupported file type"})
                continue
            if f.size > max_bytes:
                result.skipped.append(
                    {"path": f.path_display, "reason": f"larger than {s.dropbox_max_file_mb} MB"}
                )
                continue
            seen.add(f.id)
            doc = existing.get(f.id)
            try:
                if doc is None:
                    doc = Document(
                        title=_title(f.name),
                        description="",
                        filename=f.name[:300],
                        content_type="application/octet-stream",
                        size_bytes=f.size,
                        sha256="",
                        storage_key=f"dropbox:{f.id}",
                        source=SOURCE,
                        source_id=f.id,
                        source_path=f.path_display[:1000],
                        status="processing",
                    )
                    _set_access(doc, roles)
                    db.add(doc)
                    _ingest(db, doc, f, client)
                    audit(
                        db,
                        "document.added_from_dropbox",
                        actor=actor,
                        target_type="document",
                        target_id=doc.id,
                        path=f.path_display,
                        roles=roles,
                        status=doc.status,
                    )
                    result.added.append(f.path_display)
                else:
                    changed = False
                    restored = doc.deleted_at is not None
                    if doc.source_path != f.path_display:
                        doc.source_path = f.path_display[:1000]
                        doc.filename = f.name[:300]
                        doc.title = _title(f.name)
                        changed = True
                    if doc.role_names != roles:
                        before = doc.role_names
                        _set_access(doc, roles)
                        audit(
                            db,
                            "document.access_changed_in_dropbox",
                            actor=actor,
                            target_type="document",
                            target_id=doc.id,
                            path=f.path_display,
                            before=before,
                            after=roles,
                        )
                        if not restored:
                            result.access_changed.append(f.path_display)
                        changed = True
                    if restored or doc.source_rev != f.content_hash:
                        doc.deleted_at = None
                        _ingest(db, doc, f, client)
                        result.updated.append(f.path_display)
                        changed = True
                    if changed:
                        doc.updated_at = utcnow()
                    else:
                        result.unchanged += 1
                _renew(db)  # a first import of many files can take a while
                db.commit()
            except (DropboxError, ExtractionError) as exc:
                db.rollback()
                result.failed.append({"path": f.path_display, "reason": str(exc)})
                log.warning("dropbox sync: %s: %s", f.path_display, exc)

        for doc in active:
            if doc.source_id not in seen:
                soft_delete(db, doc)
                audit(
                    db,
                    "document.removed_from_dropbox",
                    actor=actor,
                    target_type="document",
                    target_id=doc.id,
                    path=doc.source_path,
                )
                result.removed.append(doc.source_path)
        db.commit()
        return result
    except DropboxError as exc:
        db.rollback()
        result.error = str(exc)
        log.error("dropbox sync failed: %s", exc)
        return result
    finally:
        db.rollback()  # nothing uncommitted is wanted here; clears a failed transaction
        audit(db, "dropbox.synced", actor=actor, summary=result.summary(), error=result.error)
        _release(db, result)
