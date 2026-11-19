"""Document upload, indexing, visibility changes and deletion."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.embeddings import get_embedder
from app.ingest.chunk import chunk_pages
from app.ingest.extract import ExtractionError, extract_pages, sniff_content_type
from app.models import Document, DocumentAccess, DocumentChunk, User
from app.permissions import SHAREABLE_ROLES, Role
from app.storage import get_storage

log = logging.getLogger(__name__)


class DocumentError(Exception):
    pass


def clean_roles(roles: Iterable[str]) -> list[str]:
    allowed = {r.value for r in SHAREABLE_ROLES}
    picked = sorted({r for r in roles if r in allowed})
    if not picked:
        raise DocumentError("Choose at least one group who can see this document.")
    return picked


def create_document(
    db: Session,
    *,
    uploader: User,
    title: str,
    description: str,
    filename: str,
    data: bytes,
    declared_type: str | None,
    roles: Iterable[str],
) -> Document:
    s = get_settings()
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise DocumentError(f"Files can be at most {s.max_upload_mb} MB.")
    role_list = clean_roles(roles)
    # Non-admins can't create a document they themselves couldn't see.
    if Role(uploader.role) != Role.ADMIN and uploader.role not in role_list:
        raise DocumentError("Include your own group in who can see this document.")
    try:
        content_type = sniff_content_type(filename, data, declared_type)
    except ExtractionError as exc:
        raise DocumentError(str(exc)) from exc

    safe_name = filename.replace("/", "_").replace("\\", "_")[-200:] or "document"
    doc = Document(
        title=(title.strip() or safe_name)[:300],
        description=description.strip()[:2000],
        filename=safe_name,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        storage_key=f"{uuid.uuid4().hex}",
        status="processing",
        uploaded_by_id=uploader.id,
        access=[DocumentAccess(role=r) for r in role_list],
    )
    db.add(doc)
    db.flush()
    get_storage().put(doc.storage_key, data, content_type)
    index_document(db, doc, data)
    return doc


def read_bytes(doc: Document) -> bytes:
    """The document's file: from Dropbox for synced documents, else from storage."""
    if doc.source == "dropbox":
        from app.dropbox import get_dropbox

        return get_dropbox().download(doc.source_id)
    return get_storage().get(doc.storage_key)


def index_document(db: Session, doc: Document, data: bytes | None = None) -> None:
    """(Re)build the chunks and embeddings for a document."""
    s = get_settings()
    if data is None:
        data = read_bytes(doc)
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    try:
        pages = extract_pages(data, doc.content_type)
        chunks = chunk_pages(pages, s.chunk_size_chars, s.chunk_overlap_chars)
        if not chunks:
            raise ExtractionError("No text could be extracted from this file.")
        # Prefix the title so short chunks still carry their context.
        vectors = get_embedder().embed_documents([f"{doc.title}\n\n{c.text}" for c in chunks])
    except ExtractionError as exc:
        doc.status, doc.error = "failed", str(exc)
        return
    except Exception as exc:
        log.exception("indexing document %s failed", doc.id)
        doc.status, doc.error = "failed", f"Indexing failed: {type(exc).__name__}"
        return
    for c, vec in zip(chunks, vectors, strict=True):
        db.add(
            DocumentChunk(
                document_id=doc.id, ordinal=c.ordinal, page=c.page, text=c.text, embedding=vec
            )
        )
    doc.page_count = max((p.number or 0) for p in pages) or None
    doc.status, doc.error = "ready", ""


def set_visibility(db: Session, doc: Document, roles: Iterable[str], actor: User) -> list[str]:
    role_list = clean_roles(roles)
    if Role(actor.role) != Role.ADMIN and actor.role not in role_list:
        raise DocumentError("You can't remove your own group's access; ask an administrator.")
    doc.access = [DocumentAccess(role=r) for r in role_list]
    doc.updated_at = utcnow()
    return role_list


def soft_delete(db: Session, doc: Document) -> None:
    """Hide immediately and remove indexed text; an uploaded file is purged too.

    The portal never deletes anything in Dropbox."""
    doc.deleted_at = utcnow()
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    if doc.source != "upload":
        return
    try:
        get_storage().delete(doc.storage_key)
    except Exception:  # pragma: no cover - storage hiccup shouldn't block the delete
        log.exception("could not purge stored file for document %s", doc.id)


def list_visible(db: Session, user: User) -> list[Document]:
    q = select(Document).where(Document.deleted_at.is_(None)).order_by(Document.created_at.desc())
    if Role(user.role) != Role.ADMIN:
        q = q.where(
            Document.id.in_(
                select(DocumentAccess.document_id).where(DocumentAccess.role == user.role)
            )
        )
    return list(db.scalars(q))
