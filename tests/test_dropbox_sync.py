"""The board-managed Dropbox folder: client, sync, permissions and the web pages.

Runs against a fake Dropbox API, so no network or real account is involved.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.dropbox import DropboxClient, DropboxError
from app.models import AuditLog, Document, DocumentChunk
from app.permissions import Role
from app.rag.retrieval import retrieve
from app.services import documents as doc_service
from app.services import dropbox_sync

BOARD_TEXT = b"# Executive session\n\nThe board voted to refer Lot 7 to collections counsel."
POOL_TEXT = b"# Pool rules\n\nThe pool is open from 10 AM to 8 PM daily."


class FakeDropbox:
    """Just enough of the Dropbox HTTP API: token refresh, list_folder(+continue), download,
    get_temporary_link."""

    def __init__(self, page_size: int = 2):
        self.files: dict[str, dict] = {}  # id -> {"path": display path, "data": bytes}
        self.token_calls = 0
        self.valid_tokens: set[str] = set()
        self.fail_next: list[int] = []
        self.page_size = page_size
        self.downloads = 0
        self._next_id = 0
        self.missing_root = False
        self.unreadable: set[str] = set()  # ids whose download fails
        self.move_on_download: dict[str, str] = {}  # id -> where it is by the time it's read

    # test helpers -------------------------------------------------------------
    def add(self, path: str, data: bytes) -> str:
        self._next_id += 1
        fid = f"id:file{self._next_id}"
        self.files[fid] = {"path": path, "data": data}
        return fid

    def move(self, fid: str, new_path: str) -> None:
        self.files[fid]["path"] = new_path

    def edit(self, fid: str, data: bytes) -> None:
        self.files[fid]["data"] = data

    def remove(self, fid: str) -> None:
        del self.files[fid]

    # API ------------------------------------------------------------------------
    def _meta(self, fid: str) -> dict:
        f = self.files[fid]
        return {
            ".tag": "file",
            "id": fid,
            "name": f["path"].rsplit("/", 1)[1],
            "path_lower": f["path"].lower(),
            "path_display": f["path"],
            "content_hash": hashlib.sha256(f["data"]).hexdigest(),
            "size": len(f["data"]),
        }

    def _entries(self):
        out = []
        for fid, f in sorted(self.files.items()):
            out.append(self._meta(fid))
            out.append({".tag": "folder", "path_lower": f["path"].lower().rsplit("/", 1)[0]})
        return out

    def _page(self, start: int):
        entries = self._entries()
        chunk = entries[start : start + self.page_size]
        more = start + self.page_size < len(entries)
        return httpx.Response(
            200, json={"entries": chunk, "cursor": str(start + self.page_size), "has_more": more}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form == {"grant_type": "refresh_token", "refresh_token": "refresh-placeholder"}
            assert request.headers["authorization"].startswith("Basic ")
            self.token_calls += 1
            token = f"token-{self.token_calls}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 14400})
        if request.headers.get("authorization", "")[7:] not in self.valid_tokens:
            return httpx.Response(401, json={"error_summary": "expired_access_token/"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"})
        if url.endswith("/files/list_folder"):
            body = json.loads(request.content)
            assert body["recursive"] is True
            if self.missing_root:
                return httpx.Response(409, json={"error_summary": "path/not_found/.."})
            return self._page(0)
        if url.endswith("/files/list_folder/continue"):
            return self._page(int(json.loads(request.content)["cursor"]))
        if url.endswith("/files/download"):
            fid = json.loads(request.headers["dropbox-api-arg"])["path"]
            if fid not in self.files:
                return httpx.Response(409, json={"error_summary": "path/not_found/"})
            if fid in self.unreadable:
                return httpx.Response(409, json={"error_summary": "path/restricted_content/"})
            if fid in self.move_on_download:
                self.move(fid, self.move_on_download.pop(fid))
            self.downloads += 1
            # Dropbox describes the file as it is now in a header (ASCII-escaped JSON).
            header = json.dumps(self._meta(fid))
            content = self.files[fid]["data"]
            return httpx.Response(200, content=content, headers={"Dropbox-API-Result": header})
        if url.endswith("/files/get_temporary_link"):
            fid = json.loads(request.content)["path"]
            if fid not in self.files:
                return httpx.Response(409, json={"error_summary": "path/not_found/"})
            link = f"https://dl.dropboxusercontent.com/{fid}"
            return httpx.Response(200, json={"metadata": self._meta(fid), "link": link})
        raise AssertionError(f"the portal must not call {url}")  # never writes to Dropbox


@pytest.fixture
def dropbox(monkeypatch):
    fake = FakeDropbox()
    client = DropboxClient(
        app_key="app-placeholder",
        app_secret="secret-placeholder",
        refresh_token="refresh-placeholder",
        transport=httpx.MockTransport(fake),
    )
    s = get_settings()
    monkeypatch.setattr(s, "document_source", "dropbox")
    monkeypatch.setattr(s, "dropbox_root", "/HOA Portal")
    import app.dropbox as dbx

    monkeypatch.setattr(dbx, "get_dropbox", lambda for_request=False: client)
    monkeypatch.setattr(dropbox_sync, "get_dropbox", lambda: client)
    fake.client = client
    return fake


def _docs(db):
    db.expire_all()
    return {
        d.source_path: d for d in db.scalars(select(Document).where(Document.source == "dropbox"))
    }


# --- client -------------------------------------------------------------------


def test_client_pages_through_listing_and_refreshes_tokens(dropbox):
    for i in range(5):
        dropbox.add(f"/HOA Portal/Everyone/doc{i}.md", b"x")
    files = list(dropbox.client.list_files("/HOA Portal"))
    assert len(files) == 5  # folders skipped, all pages read
    assert dropbox.token_calls == 1
    dropbox.valid_tokens.clear()  # Dropbox expired the token early
    dropbox.fail_next = [429, 503]  # and is briefly overloaded
    assert dropbox.client.download(files[0].id) == b"x"
    assert dropbox.token_calls == 2


def test_client_reports_missing_folder(dropbox):
    dropbox.missing_root = True
    with pytest.raises(DropboxError, match="not found"):
        list(dropbox.client.list_files("/HOA Portal"))


# --- folder -> roles --------------------------------------------------------------


@pytest.mark.parametrize(
    "path,roles",
    [
        ("/hoa portal/everyone/rules.pdf", ["board", "homeowner", "management"]),
        ("/hoa portal/everyone/2026/budget.pdf", ["board", "homeowner", "management"]),
        ("/hoa portal/board/minutes.pdf", ["board"]),
        ("/hoa portal/board and management/contract.pdf", ["board", "management"]),
        ("/hoa portal/management/vendors.pdf", ["management"]),
        ("/hoa portal/readme.pdf", None),  # loose files in the root are ignored
        ("/hoa portal/drafts/x.pdf", None),
        ("/hoa portal/boardroom/x.pdf", None),  # "board" must be a whole folder name
        ("/somewhere else/board/x.pdf", None),
    ],
)
def test_roles_for_path(dropbox, path, roles):
    assert dropbox_sync.roles_for(path) == roles


# --- sync ---------------------------------------------------------------------------


def test_sync_adds_indexes_and_applies_folder_permissions(dropbox, db, make_user):
    dropbox.add("/HOA Portal/Board/Executive minutes.md", BOARD_TEXT)
    dropbox.add("/HOA Portal/Everyone/Pool rules.md", POOL_TEXT)
    dropbox.add("/HOA Portal/Everyone/photo.jpg", b"\xff\xd8")
    dropbox.add("/HOA Portal/Inbox/scan.pdf", b"%PDF-1.4")

    result = dropbox_sync.sync(db)
    assert result.error == ""
    assert sorted(result.added) == [
        "/HOA Portal/Board/Executive minutes.md",
        "/HOA Portal/Everyone/Pool rules.md",
    ]
    assert {s["reason"] for s in result.skipped} == {
        "unsupported file type",
        "not in a shared folder",
    }
    docs = _docs(db)
    minutes = docs["/HOA Portal/Board/Executive minutes.md"]
    assert minutes.title == "Executive minutes"
    assert minutes.status == "ready" and minutes.role_names == ["board"]
    assert db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == minutes.id)).all()

    owner, board = make_user(Role.HOMEOWNER), make_user(Role.BOARD)
    q = "What did the board vote about Lot 7 collections counsel?"
    assert "Executive minutes" not in {s.title for s in retrieve(db, owner, q, min_score=-1)}
    assert "Executive minutes" in {s.title for s in retrieve(db, board, q, min_score=-1)}

    # A second run with nothing changed downloads nothing.
    before = dropbox.downloads
    again = dropbox_sync.sync(db)
    assert again.unchanged == 2 and not again.added and dropbox.downloads == before


def test_moving_a_file_changes_who_can_see_it(dropbox, db, make_user):
    fid = dropbox.add("/HOA Portal/Everyone/Minutes.md", BOARD_TEXT)
    dropbox_sync.sync(db)
    owner = make_user(Role.HOMEOWNER)
    assert retrieve(db, owner, "Lot 7 collections counsel", min_score=-1)

    dropbox.move(fid, "/HOA Portal/Board/Minutes.md")
    result = dropbox_sync.sync(db)
    assert result.access_changed == ["/HOA Portal/Board/Minutes.md"]
    assert dropbox.downloads == 1  # same contents: permissions change without re-reading
    doc = _docs(db)["/HOA Portal/Board/Minutes.md"]
    assert doc.role_names == ["board"]
    assert retrieve(db, owner, "Lot 7 collections counsel", min_score=-1) == []
    assert db.scalar(
        select(AuditLog).where(AuditLog.action == "document.access_changed_in_dropbox")
    ).details["after"] == ["board"]


def test_edited_file_is_reindexed(dropbox, db, make_user):
    fid = dropbox.add("/HOA Portal/Everyone/Pool rules.md", POOL_TEXT)
    dropbox_sync.sync(db)
    dropbox.edit(fid, b"# Pool rules\n\nThe pool now closes at 9 PM on weekends.")
    result = dropbox_sync.sync(db)
    assert result.updated == ["/HOA Portal/Everyone/Pool rules.md"]
    owner = make_user(Role.HOMEOWNER)
    texts = " ".join(s.text for s in retrieve(db, owner, "When does the pool close?", min_score=-1))
    assert "9 PM" in texts and "8 PM" not in texts


def test_deleted_or_moved_out_files_are_removed_and_can_come_back(dropbox, db, make_user):
    a = dropbox.add("/HOA Portal/Everyone/A.md", POOL_TEXT)
    b = dropbox.add("/HOA Portal/Everyone/B.md", BOARD_TEXT)
    dropbox_sync.sync(db)
    dropbox.remove(a)
    dropbox.move(b, "/HOA Portal/Archive/B.md")
    result = dropbox_sync.sync(db)
    assert sorted(result.removed) == ["/HOA Portal/Everyone/A.md", "/HOA Portal/Everyone/B.md"]
    owner = make_user(Role.HOMEOWNER)
    assert retrieve(db, owner, "pool Lot 7", min_score=-1) == []

    dropbox.move(b, "/HOA Portal/Everyone/B.md")  # moved back
    result = dropbox_sync.sync(db)
    assert result.updated == ["/HOA Portal/Everyone/B.md"]
    assert _docs(db)["/HOA Portal/Everyone/B.md"].deleted_at is None


def test_empty_listing_does_not_wipe_the_library(dropbox, db):
    fid = dropbox.add("/HOA Portal/Everyone/A.md", POOL_TEXT)
    dropbox_sync.sync(db)
    dropbox.remove(fid)
    result = dropbox_sync.sync(db)
    assert "looks empty" in result.error and result.removed == []
    assert _docs(db)["/HOA Portal/Everyone/A.md"].deleted_at is None


def test_unreadable_file_is_reported_and_others_continue(dropbox, db):
    dropbox.add("/HOA Portal/Everyone/fake.pdf", b"not really a pdf")
    dropbox.add("/HOA Portal/Everyone/Pool rules.md", POOL_TEXT)
    result = dropbox_sync.sync(db)
    assert [f["path"] for f in result.failed] == ["/HOA Portal/Everyone/fake.pdf"]
    assert result.added == ["/HOA Portal/Everyone/Pool rules.md"]


def test_missing_folder_is_an_error_not_a_wipe(dropbox, db):
    dropbox.add("/HOA Portal/Everyone/A.md", POOL_TEXT)
    dropbox_sync.sync(db)
    dropbox.missing_root = True
    result = dropbox_sync.sync(db)
    assert "not found" in result.error
    assert _docs(db)["/HOA Portal/Everyone/A.md"].deleted_at is None


def test_only_one_sync_runs_at_a_time(dropbox, db):
    assert dropbox_sync._acquire(db)
    with pytest.raises(dropbox_sync.SyncBusy):
        dropbox_sync.sync(db)


# --- web ------------------------------------------------------------------------------


def test_web_flow_on_dropbox(dropbox, client_for, db):
    dropbox.add("/HOA Portal/Board/Minutes.md", BOARD_TEXT)
    board = client_for(Role.BOARD)
    owner = client_for(Role.HOMEOWNER)

    page = board.get("/documents/upload")
    assert "/HOA Portal/Board" in page.text and "Check Dropbox now" in page.text
    assert owner.post_form("/documents/sync").status_code == 403
    resp = board.post_form("/documents/sync")
    assert resp.status_code == 303  # the sync ran as a background task
    doc = _docs(db)["/HOA Portal/Board/Minutes.md"]
    assert "1 added" in board.get("/documents/upload").text

    # Downloads come straight from Dropbox, after the portal's permission check.
    assert board.get(f"/documents/{doc.id}/download").content == BOARD_TEXT
    assert owner.get(f"/documents/{doc.id}/download").status_code == 404
    assert owner.get(f"/documents/{doc.id}").status_code == 404

    # Who-can-see and delete are managed in Dropbox, not in the portal.
    resp = board.post_form(f"/documents/{doc.id}/visibility", {"roles": ["homeowner", "board"]})
    assert "managed in Dropbox" in board.get(resp.headers["location"]).text
    board.post_form(f"/documents/{doc.id}/delete")
    db.expire_all()
    assert db.get(Document, doc.id).deleted_at is None
    assert db.get(Document, doc.id).role_names == ["board"]

    # Uploading through the portal is turned off.
    resp = board.post_form(
        "/documents",
        {"title": "x", "roles": ["board"]},
        files={"file": ("x.md", b"hi", "text/markdown")},
    )
    assert resp.status_code == 400 and "Dropbox folder" in resp.text


def test_sync_button_is_hidden_in_upload_mode(client_for):
    assert client_for(Role.BOARD).post_form("/documents/sync").status_code == 404


# --- a file moved to a more private folder ------------------------------------------

SETTLEMENT = b"Settlement with the Lot 7 owner: \x93confidential\x94"  # Windows-1252, not UTF-8


@pytest.mark.parametrize("failure", ["unreadable text", "download error"])
def test_move_with_an_unreadable_edit_still_takes_away_access(
    dropbox, client_for, db, make_user, failure
):
    fid = dropbox.add("/HOA Portal/Everyone/Notes.txt", POOL_TEXT)
    assert dropbox_sync.sync(db).error == ""
    owner = client_for(Role.HOMEOWNER)
    doc_id = _docs(db)["/HOA Portal/Everyone/Notes.txt"].id
    assert owner.get(f"/documents/{doc_id}").status_code == 200

    # The board moves it to Board/ and replaces it with something the portal can't read.
    dropbox.move(fid, "/HOA Portal/Board/Notes.txt")
    if failure == "unreadable text":
        dropbox.edit(fid, SETTLEMENT)
    else:
        dropbox.edit(fid, b"# Notes\n\nNew text.")
        dropbox.unreadable.add(fid)
    result = dropbox_sync.sync(db)
    assert [f["path"] for f in result.failed] == ["/HOA Portal/Board/Notes.txt"]

    doc = _docs(db)["/HOA Portal/Board/Notes.txt"]
    assert doc.role_names == ["board"] and doc.status == "failed"
    assert retrieve(db, make_user(Role.HOMEOWNER, "h2@example.com"), "pool open") == []
    assert owner.get(f"/documents/{doc_id}").status_code == 404

    # Once the file is fixed, the next sync reads it.
    dropbox.unreadable.clear()
    dropbox.edit(fid, b"# Notes\n\nReadable again.")
    assert dropbox_sync.sync(db).updated == ["/HOA Portal/Board/Notes.txt"]
    assert _docs(db)["/HOA Portal/Board/Notes.txt"].status == "ready"


def test_restored_file_that_cant_be_read_stays_hidden(dropbox, db):
    fid = dropbox.add("/HOA Portal/Everyone/A.md", POOL_TEXT)
    dropbox.add("/HOA Portal/Everyone/B.md", POOL_TEXT)
    dropbox_sync.sync(db)
    dropbox.move(fid, "/HOA Portal/Archive/A.md")
    dropbox_sync.sync(db)
    dropbox.move(fid, "/HOA Portal/Board/A.md")
    dropbox.unreadable.add(fid)
    dropbox_sync.sync(db)
    doc = _docs(db)["/HOA Portal/Board/A.md"]
    assert doc.deleted_at is not None and doc.role_names == ["board"]


def test_passing_dropbox_error_on_an_edit_keeps_the_old_text(dropbox, db, make_user):
    homeowner = make_user(Role.HOMEOWNER)
    fid = dropbox.add("/HOA Portal/Everyone/Pool rules.md", POOL_TEXT)
    assert dropbox_sync.sync(db).error == ""
    # A small edit in the same folder, and Dropbox has a bad minute while it's read.
    dropbox.edit(fid, POOL_TEXT + b"\n\nGuests must sign in.")
    dropbox.unreadable.add(fid)
    result = dropbox_sync.sync(db)
    assert [f["path"] for f in result.failed] == ["/HOA Portal/Everyone/Pool rules.md"]
    doc = _docs(db)["/HOA Portal/Everyone/Pool rules.md"]
    assert doc.status == "ready" and doc.error == ""
    assert retrieve(db, homeowner, "pool open")  # still answers from the old text

    dropbox.unreadable.clear()  # the next sync tries again
    assert dropbox_sync.sync(db).updated == ["/HOA Portal/Everyone/Pool rules.md"]
    texts = " ".join(s.text for s in retrieve(db, homeowner, "guests sign in", min_score=-1))
    assert "Guests must sign in" in texts


@pytest.mark.parametrize("known", [False, True])
def test_file_moved_while_being_read_waits_for_the_next_sync(dropbox, db, make_user, known):
    homeowner = make_user(Role.HOMEOWNER)
    fid = dropbox.add("/HOA Portal/Everyone/Notes.md", POOL_TEXT)
    if known:
        dropbox_sync.sync(db)
    dropbox.edit(fid, BOARD_TEXT)
    # Listed under Everyone, but in Board/ by the time the sync downloads it.
    dropbox.move_on_download[fid] = "/HOA Portal/Board/Notes.md"
    result = dropbox_sync.sync(db)
    assert [f["path"] for f in result.failed] == ["/HOA Portal/Everyone/Notes.md"]
    assert all("collections" not in s.text for s in retrieve(db, homeowner, "Lot 7", min_score=-1))
    docs = _docs(db)
    if known:  # still the old text, for the old folder's audience, until the next sync
        assert docs["/HOA Portal/Everyone/Notes.md"].status == "ready"
    else:
        assert docs == {}

    dropbox_sync.sync(db)
    doc = _docs(db)["/HOA Portal/Board/Notes.md"]
    assert doc.role_names == ["board"] and doc.status == "ready"
    assert all("collections" not in s.text for s in retrieve(db, homeowner, "Lot 7", min_score=-1))


def test_download_checks_the_folder_the_file_is_in_now(dropbox, client_for, db):
    fid = dropbox.add("/HOA Portal/Everyone/Minutes.md", BOARD_TEXT)
    dropbox_sync.sync(db)
    doc_id = _docs(db)["/HOA Portal/Everyone/Minutes.md"].id
    owner = client_for(Role.HOMEOWNER)
    assert owner.get(f"/documents/{doc_id}/download").content == BOARD_TEXT

    # Moved to Board/ in Dropbox; the portal hasn't synced yet.
    dropbox.move(fid, "/HOA Portal/Board/Minutes.md")
    assert owner.get(f"/documents/{doc_id}/download").status_code == 404
    # The refused download started a sync, which has caught up.
    assert _docs(db)["/HOA Portal/Board/Minutes.md"].role_names == ["board"]
    board = client_for(Role.BOARD)
    assert board.get(f"/documents/{doc_id}/download").content == BOARD_TEXT

    # Moved out of the shared folders altogether: nobody downloads it, admins included.
    dropbox.move(fid, "/HOA Portal/Drafts/Minutes.md")
    assert board.get(f"/documents/{doc_id}/download").status_code == 404
    admin = client_for(Role.ADMIN)
    assert admin.get(f"/documents/{doc_id}/download").status_code == 404


def test_file_that_grew_since_the_sync_goes_by_link(dropbox, client_for, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "download_redirect_over_mb", 1)
    fid = dropbox.add("/HOA Portal/Everyone/Budget.md", POOL_TEXT)
    dropbox_sync.sync(db)
    doc_id = _docs(db)["/HOA Portal/Everyone/Budget.md"].id
    dropbox.edit(fid, POOL_TEXT + b"x" * (2 * 1024 * 1024))  # too big for a Lambda response
    resp = client_for(Role.HOMEOWNER).get(f"/documents/{doc_id}/download", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"https://dl.dropboxusercontent.com/{fid}"


def test_reindex_of_a_moved_file_syncs_instead(dropbox, client_for, db, make_user):
    fid = dropbox.add("/HOA Portal/Everyone/Notes.md", POOL_TEXT)
    dropbox_sync.sync(db)
    doc_id = _docs(db)["/HOA Portal/Everyone/Notes.md"].id
    dropbox.move(fid, "/HOA Portal/Board/Notes.md")
    dropbox.edit(fid, BOARD_TEXT)
    mgr = client_for(Role.MANAGEMENT)
    assert mgr.post_form(f"/documents/{doc_id}/reindex").status_code == 303
    owner = make_user(Role.HOMEOWNER, "h2@example.com")
    assert retrieve(db, owner, "Lot 7 collections counsel", min_score=-1) == []
    assert _docs(db)["/HOA Portal/Board/Notes.md"].role_names == ["board"]


def test_reindex_from_the_command_line_skips_moved_files(dropbox, db):
    fid = dropbox.add("/HOA Portal/Everyone/Notes.md", POOL_TEXT)
    dropbox_sync.sync(db)
    dropbox.move(fid, "/HOA Portal/Board/Notes.md")
    dropbox.edit(fid, BOARD_TEXT)
    doc = _docs(db)["/HOA Portal/Everyone/Notes.md"]
    with pytest.raises(DropboxError, match="moved"):
        doc_service.index_document(db, doc)


def test_sync_report_hides_names_of_files_the_viewer_cant_see(dropbox, client_for, db):
    dropbox.add("/HOA Portal/Board/2026-03 executive session.pdf", b"not a pdf")
    dropbox.add("/HOA Portal/Management/vendor notes.pdf", b"not a pdf")
    dropbox.add("/HOA Portal/Drafts/settlement draft.md", POOL_TEXT)
    dropbox.add("/HOA Portal/Everyone/Pool rules.md", POOL_TEXT)
    dropbox_sync.sync(db)
    mgr = client_for(Role.MANAGEMENT).get("/documents/upload").text
    assert "vendor notes" in mgr
    assert "executive session" not in mgr and "1 in folders you can't see" in mgr
    assert "settlement draft" not in mgr and "1 skipped" in mgr
    admin = client_for(Role.ADMIN).get("/documents/upload").text
    assert "executive session" in admin and "settlement draft" in admin


def test_web_requests_wait_briefly_for_dropbox(monkeypatch):
    import app.dropbox as dbx

    waits = []
    monkeypatch.setattr(dbx.time, "sleep", waits.append)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 14400})
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "300"})

    client = DropboxClient(
        app_key="k",
        app_secret="s",
        refresh_token="r",
        transport=httpx.MockTransport(handler),
        **dbx.REQUEST_LIMITS,
    )
    with pytest.raises(DropboxError):
        client.download("id:x")
    assert len(calls) == 2 and waits == [2.0]  # one retry, after 2 s rather than 5 minutes
