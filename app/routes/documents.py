from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.models import Document, User
from app.permissions import (
    VISIBILITY_PRESETS,
    Perm,
    can_manage_document,
    can_view_document,
)
from app.security.auth import require_perm, require_user
from app.services import documents as doc_service
from app.storage import get_storage
from app.web import redirect, render

router = APIRouter(prefix="/documents")


def _visible(db: Session, user: User, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    # 404 (not 403) so users can't probe for documents they can't see.
    if doc is None or not can_view_document(user, doc):
        raise HTTPException(404)
    return doc


def _manageable(db: Session, user: User, doc_id: int) -> Document:
    doc = _visible(db, user, doc_id)
    if not can_manage_document(user, doc):
        raise HTTPException(403)
    return doc


@router.get("")
def list_documents(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return render(request, "documents/list.html", documents=doc_service.list_visible(db, user))


@router.get("/upload")
def upload_form(request: Request, user: User = Depends(require_perm(Perm.DOCS_UPLOAD))):
    return render(
        request,
        "documents/upload.html",
        presets=VISIBILITY_PRESETS,
        max_mb=get_settings().max_upload_mb,
    )


@router.post("")
def upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    roles: list[str] = Form(default=[]),
    user: User = Depends(require_perm(Perm.DOCS_UPLOAD)),
    db: Session = Depends(get_db),
):
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    try:
        doc = doc_service.create_document(
            db,
            uploader=user,
            title=title,
            description=description,
            filename=file.filename or "document",
            data=data,
            declared_type=file.content_type,
            roles=roles,
        )
    except doc_service.DocumentError as exc:
        db.rollback()
        return render(
            request,
            "documents/upload.html",
            400,
            error=str(exc),
            presets=VISIBILITY_PRESETS,
            max_mb=get_settings().max_upload_mb,
            title=title,
            description=description,
            roles=roles,
        )
    audit(
        db,
        "document.uploaded",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
        title=doc.title,
        roles=doc.role_names,
        status=doc.status,
    )
    db.commit()
    if doc.status == "failed":
        return redirect(
            f"/documents/{doc.id}", f"Uploaded, but indexing failed: {doc.error}", "error"
        )
    return redirect(f"/documents/{doc.id}", "Uploaded and indexed for the assistant.")


@router.get("/{doc_id}")
def show(
    request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    doc = _visible(db, user, doc_id)
    return render(
        request, "documents/show.html", doc=doc, can_manage=can_manage_document(user, doc)
    )


@router.get("/{doc_id}/download")
def download(
    request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    doc = _visible(db, user, doc_id)
    data = get_storage().get(doc.storage_key)
    audit(
        db,
        "document.downloaded",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
    )
    db.commit()
    disposition = "inline" if doc.content_type == "application/pdf" else "attachment"
    return Response(
        data,
        media_type=doc.content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(doc.filename)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{doc_id}/visibility")
def update_visibility(
    request: Request,
    doc_id: int,
    roles: list[str] = Form(default=[]),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    doc = _manageable(db, user, doc_id)
    before = doc.role_names
    try:
        after = doc_service.set_visibility(db, doc, roles, user)
    except doc_service.DocumentError as exc:
        return redirect(f"/documents/{doc.id}", str(exc), "error")
    audit(
        db,
        "document.visibility_changed",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
        before=before,
        after=after,
    )
    db.commit()
    return redirect(f"/documents/{doc.id}", "Visibility updated.")


@router.post("/{doc_id}/reindex")
def reindex(
    request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    doc = _manageable(db, user, doc_id)
    doc_service.index_document(db, doc)
    audit(
        db,
        "document.reindexed",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
        status=doc.status,
    )
    db.commit()
    return redirect(
        f"/documents/{doc.id}",
        "Re-indexed." if doc.status == "ready" else f"Indexing failed: {doc.error}",
        "ok" if doc.status == "ready" else "error",
    )


@router.post("/{doc_id}/delete")
def delete(
    request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    doc = _manageable(db, user, doc_id)
    doc_service.soft_delete(db, doc)
    audit(
        db,
        "document.deleted",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
        title=doc.title,
    )
    db.commit()
    return redirect("/documents", f"Deleted “{doc.title}”.")
