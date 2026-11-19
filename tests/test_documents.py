import io
from pathlib import Path

from sqlalchemy import select

from app.models import AuditLog, Document, DocumentChunk
from app.permissions import Role

BOARD_TEXT = b"# Executive session\n\nThe board voted to refer Lot 7 to collections counsel."
PUBLIC_TEXT = b"# Pool rules\n\nThe pool is open from 10 AM to 8 PM daily."


def upload(client, data=PUBLIC_TEXT, name="pool.md", roles=("homeowner", "board"), title=""):
    return client.post_form(
        "/documents",
        {"title": title or name, "description": "", "roles": list(roles)},
        files={"file": (name, io.BytesIO(data), "text/markdown")},
    )


def doc_id(resp) -> int:
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_homeowner_cannot_upload(client_for):
    owner = client_for(Role.HOMEOWNER)
    assert owner.get("/documents/upload").status_code == 403
    assert upload(owner).status_code == 403


def test_upload_index_and_download(client_for, db):
    board = client_for(Role.BOARD)
    resp = upload(board)
    assert resp.status_code == 303
    doc = db.get(Document, doc_id(resp))
    assert doc.status == "ready"
    assert doc.role_names == ["board", "homeowner"]
    assert db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)).all()
    got = board.get(f"/documents/{doc.id}/download")
    assert got.status_code == 200 and got.content == PUBLIC_TEXT
    assert got.headers["cache-control"] == "private, no-store"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "document.downloaded"))


def test_file_is_encrypted_at_rest(client_for, db):
    board = client_for(Role.BOARD)
    upload(board, data=BOARD_TEXT, name="minutes.md", roles=("board",))
    import os

    root = Path(os.environ["LOCAL_STORAGE_DIR"])
    blobs = [p.read_bytes() for p in root.rglob("*.bin")]
    assert blobs
    assert all(b"collections counsel" not in blob for blob in blobs)


def test_board_only_document_is_invisible_to_homeowners(client_for, db):
    board = client_for(Role.BOARD)
    owner = client_for(Role.HOMEOWNER)
    resp = upload(
        board,
        data=BOARD_TEXT,
        name="minutes.md",
        roles=("board",),
        title="Executive session minutes",
    )
    did = doc_id(resp)
    assert owner.get(f"/documents/{did}").status_code == 404
    assert owner.get(f"/documents/{did}/download").status_code == 404
    assert "Executive session minutes" not in owner.get("/documents").text
    assert "Executive session minutes" in board.get("/documents").text


def test_management_cannot_see_board_only(client_for):
    board = client_for(Role.BOARD)
    mgmt = client_for(Role.MANAGEMENT)
    did = doc_id(upload(board, data=BOARD_TEXT, name="minutes.md", roles=("board",)))
    assert mgmt.get(f"/documents/{did}").status_code == 404


def test_admin_sees_everything(client_for):
    board = client_for(Role.BOARD)
    admin = client_for(Role.ADMIN)
    did = doc_id(upload(board, data=BOARD_TEXT, name="minutes.md", roles=("board",)))
    assert admin.get(f"/documents/{did}").status_code == 200


def test_uploader_must_keep_own_access(client_for):
    board = client_for(Role.BOARD)
    resp = upload(board, roles=("homeowner",))
    assert resp.status_code == 400
    assert "Include your own group" in resp.text


def test_visibility_change(client_for, db):
    board = client_for(Role.BOARD)
    owner = client_for(Role.HOMEOWNER)
    did = doc_id(upload(board, roles=("board",)))
    assert owner.get(f"/documents/{did}").status_code == 404
    board.post_form(f"/documents/{did}/visibility", {"roles": ["board", "homeowner"]})
    assert owner.get(f"/documents/{did}").status_code == 200
    # Homeowners can't manage it.
    assert (
        owner.post_form(f"/documents/{did}/visibility", {"roles": ["homeowner"]}).status_code == 403
    )


def test_rejects_unsupported_and_oversized(client_for):
    board = client_for(Role.BOARD)
    resp = upload(board, data=b"MZ\x90\x00binary", name="evil.exe")
    assert resp.status_code == 400
    # A file renamed to .pdf without PDF content is rejected too.
    resp = upload(board, data=b"not really a pdf", name="fake.pdf")
    assert resp.status_code == 400


def test_delete_removes_chunks_and_file(client_for, db):
    board = client_for(Role.BOARD)
    did = doc_id(upload(board))
    resp = board.post_form(f"/documents/{did}/delete")
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Document, did).deleted_at is not None
    assert not db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == did)).all()
    assert board.get(f"/documents/{did}").status_code == 404
