from __future__ import annotations

from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.audit import audit
from app.background import run_later
from app.config import get_settings
from app.db import get_db, session_factory, utcnow
from app.dropbox import DropboxError
from app.models import Document, User, as_utc
from app.permissions import (
    VISIBILITY_PRESETS,
    Perm,
    Role,
    can_manage_document,
    can_view_document,
)
from app.security.auth import require_perm, require_user
from app.services import documents as doc_service
from app.services import dropbox_sync
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


IN_DROPBOX = (
    "This document is managed in Dropbox. To change who can see it, move it to another "
    "folder there; to remove it, delete it there."
)


def _dropbox_mode() -> bool:
    return get_settings().document_source == "dropbox"


def _dropbox_page(request: Request, db: Session, user: User, status_code: int = 200, **ctx):
    s = get_settings()
    state = dropbox_sync.last_state(db)
    return render(
        request,
        "documents/dropbox.html",
        status_code,
        root=s.dropbox_root,
        folders=[(name, roles) for name, roles in _folder_display()],
        state=state,
        summary=_summary_for(user, state.last_summary if state else {}),
        **ctx,
    )


def _summary_for(user: User, summary: dict | None) -> dict:
    """The last sync's report, without the names of files the viewer couldn't open.

    A file's name can say a lot (an executive-session date, a dispute). Failed files are
    listed only to people who could see them; skipped ones (outside every shared folder,
    so seen by nobody) only to admins.
    """
    r = dict(summary or {})
    admin = Role(user.role) == Role.ADMIN
    failed = r.get("failed") or []
    r["failed"] = [
        f for f in failed if admin or user.role in (dropbox_sync.roles_for(f["path"].lower()) or [])
    ]
    r["failed_hidden"] = len(failed) - len(r["failed"])
    r["skipped_count"] = len(r.get("skipped") or [])
    if not admin:
        r["skipped"] = []
    return r


def _folder_display() -> list[tuple[str, list[str]]]:
    import json

    raw = json.loads(get_settings().dropbox_folder_roles)
    return [(str(name).strip("/"), sorted(set(roles))) for name, roles in raw.items()]


def _run_sync(actor_id: int | None = None) -> None:
    db = session_factory()()
    try:
        actor = db.get(User, actor_id) if actor_id else None
        dropbox_sync.sync(db, actor=actor)
    except dropbox_sync.SyncBusy:
        pass
    finally:
        db.close()


@router.get("")
def list_documents(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return render(
        request,
        "documents/list.html",
        documents=doc_service.list_visible(db, user),
        dropbox_mode=_dropbox_mode(),
        sync_state=dropbox_sync.last_state(db) if _dropbox_mode() else None,
    )


@router.get("/upload")
def upload_form(
    request: Request,
    user: User = Depends(require_perm(Perm.DOCS_UPLOAD)),
    db: Session = Depends(get_db),
):
    if _dropbox_mode():
        return _dropbox_page(request, db, user)
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
    if _dropbox_mode():
        return _dropbox_page(
            request,
            db,
            user,
            400,
            error="Add documents in the Dropbox folder instead of uploading here.",
        )
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
    return redirect(f"/documents/{doc.id}", "Uploaded and indexed for Hunter.")


@router.post("/sync")
def sync_now(
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_perm(Perm.DOCS_MANAGE)),
    db: Session = Depends(get_db),
):
    if not _dropbox_mode():
        raise HTTPException(404)
    state = dropbox_sync.last_state(db)
    if state and state.locked_until and as_utc(state.locked_until) > utcnow():
        return redirect("/documents/upload", "A sync is already running.")
    run_later(background, "dropbox-sync", _run_sync, actor_id=user.id)
    return redirect(
        "/documents/upload",
        "Checking Dropbox now. New and changed files show up here when it finishes.",
    )


@router.get("/{doc_id}")
def show(
    request: Request, doc_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    doc = _visible(db, user, doc_id)
    return render(
        request, "documents/show.html", doc=doc, can_manage=can_manage_document(user, doc)
    )


def _not_found(request: Request):
    return render(
        request,
        "error.html",
        404,
        title="Not found",
        message="That page doesn't exist, or you don't have access to it.",
    )


@router.get("/{doc_id}/download")
def download(
    request: Request,
    background: BackgroundTasks,
    doc_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    doc = _visible(db, user, doc_id)
    limit = get_settings().download_redirect_over_mb * 1024 * 1024
    link = data = None
    try:
        if doc.source == "dropbox":
            from app.dropbox import get_dropbox

            client = get_dropbox(for_request=True)
            if limit and doc.size_bytes > limit:
                link, now = client.temporary_link_with_meta(doc.source_id)
            else:
                data, now = client.download_with_meta(doc.source_id)
                if limit and len(data) > limit:  # replaced by a bigger file since the sync
                    data = None
                    link, now = client.temporary_link_with_meta(doc.source_id)
            # Dropbox serves the file by id, wherever it is now. Check the folder it is
            # in now, not the one the last sync saw.
            roles = dropbox_sync.roles_for(now.path_lower)
            if roles != doc.role_names:
                run_later(background, "dropbox-sync", _run_sync)
            if roles is None or (Role(user.role) != Role.ADMIN and user.role not in roles):
                return _not_found(request)
        else:
            data = doc_service.read_bytes(doc)
    except DropboxError as exc:
        raise HTTPException(
            503, "Couldn't fetch this file from Dropbox. Try again shortly."
        ) from exc
    audit(
        db,
        "document.downloaded",
        actor=user,
        target_type="document",
        target_id=doc.id,
        request=request,
    )
    db.commit()
    if link:
        # Too big to pass through the portal; send the browser to Dropbox's copy.
        return RedirectResponse(link, 303, headers={"Cache-Control": "private, no-store"})
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
    if doc.source == "dropbox":
        return redirect(f"/documents/{doc.id}", IN_DROPBOX, "error")
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
    request: Request,
    background: BackgroundTasks,
    doc_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    doc = _manageable(db, user, doc_id)
    if doc.source == "dropbox":
        # Re-reading the file by id alone could index contents from a folder with another
        # audience; a sync re-reads it together with where it is now.
        run_later(background, "dropbox-sync", _run_sync, actor_id=user.id)
        return redirect(
            f"/documents/{doc.id}", "Checking Dropbox now. The document updates when it finishes."
        )
    try:
        doc_service.index_document(db, doc)
    except DropboxError as exc:
        return redirect(f"/documents/{doc.id}", f"Couldn't fetch the file: {exc}", "error")
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
    if doc.source == "dropbox":
        return redirect(f"/documents/{doc.id}", IN_DROPBOX, "error")
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
