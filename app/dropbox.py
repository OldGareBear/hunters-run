"""A small, read-only Dropbox API client.

The portal only lists and downloads files; it never writes, moves or deletes
anything in Dropbox. Give the Dropbox app the files.metadata.read and
files.content.read scopes and nothing else.

Authentication uses a long-lived refresh token (get one with
`python -m app.cli dropbox-auth`); short-lived access tokens are fetched and
refreshed automatically.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import get_settings

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"  # noqa: S105 - endpoint, not a secret
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"


class DropboxError(Exception):
    pass


@dataclass(frozen=True)
class DropboxFile:
    id: str  # stable across renames and moves
    name: str
    path_lower: str
    path_display: str
    content_hash: str
    size: int


class DropboxClient:
    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        refresh_token: str,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ) -> None:
        self._creds = (app_key, app_secret, refresh_token)
        self._http = httpx.Client(timeout=60, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._max_retries = max_retries

    def _access_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() > self._token_expires - 60:
                app_key, app_secret, refresh_token = self._creds
                resp = self._http.post(
                    TOKEN_URL,
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                    auth=(app_key, app_secret),
                )
                if resp.status_code != 200:
                    raise DropboxError(f"Dropbox sign-in failed ({resp.status_code})")
                body = resp.json()
                self._token = body["access_token"]
                self._token_expires = time.monotonic() + float(body.get("expires_in", 14400))
            return self._token

    def _call(
        self, url: str, *, arg: dict | None = None, json_body: dict | None = None
    ) -> httpx.Response:
        refreshed = False
        resp: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            if arg is not None:
                headers["Dropbox-API-Arg"] = json.dumps(arg)  # ASCII-escaped, as required
            try:
                resp = self._http.post(url, headers=headers, json=json_body)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise DropboxError("Dropbox is unreachable") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(force=True)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self._max_retries:
                    time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                    continue
            return resp
        assert resp is not None  # noqa: S101 - loop always runs at least once
        return resp

    @staticmethod
    def _error(resp: httpx.Response, what: str) -> DropboxError:
        summary = ""
        try:
            summary = resp.json().get("error_summary", "")
        except ValueError:
            pass
        if resp.status_code == 409 and "not_found" in summary:
            return DropboxError(f"{what}: not found in Dropbox")
        return DropboxError(f"{what} failed ({resp.status_code} {summary})".strip())

    def list_files(self, root: str) -> Iterator[DropboxFile]:
        """Every file under `root`, recursively (folders themselves are skipped)."""
        path = "/" + root.strip("/") if root.strip("/") else ""
        resp = self._call(
            f"{API}/files/list_folder",
            json_body={"path": path, "recursive": True, "include_deleted": False, "limit": 2000},
        )
        while True:
            if resp.status_code != 200:
                raise self._error(resp, f"Listing {path or '/'}")
            body = resp.json()
            for e in body.get("entries", []):
                if e.get(".tag") == "file":
                    yield DropboxFile(
                        id=e["id"],
                        name=e["name"],
                        path_lower=e["path_lower"],
                        path_display=e.get("path_display", e["path_lower"]),
                        content_hash=e.get("content_hash", e.get("rev", "")),
                        size=int(e.get("size", 0)),
                    )
            if not body.get("has_more"):
                return
            resp = self._call(
                f"{API}/files/list_folder/continue", json_body={"cursor": body["cursor"]}
            )

    def temporary_link(self, file_id: str) -> str:
        """A direct download link for the file that works for 4 hours."""
        resp = self._call(f"{API}/files/get_temporary_link", json_body={"path": file_id})
        if resp.status_code != 200:
            raise self._error(resp, "Download link")
        return resp.json()["link"]

    def download(self, file_id: str) -> bytes:
        resp = self._call(f"{CONTENT}/files/download", arg={"path": file_id})
        if resp.status_code != 200:
            raise self._error(resp, "Download")
        return resp.content


@lru_cache
def get_dropbox() -> DropboxClient:
    s = get_settings()
    if not (s.dropbox_app_key and s.dropbox_app_secret and s.dropbox_refresh_token):
        raise DropboxError("Dropbox isn't connected (set DROPBOX_APP_KEY, _SECRET, _REFRESH_TOKEN)")
    return DropboxClient(
        app_key=s.dropbox_app_key,
        app_secret=s.dropbox_app_secret,
        refresh_token=s.dropbox_refresh_token,
    )
