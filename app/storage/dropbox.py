"""Dropbox storage.

Files are encrypted in the portal before upload (see app/storage/crypto.py), so
Dropbox only ever holds ciphertext under opaque names. Who can read a document
is decided by the portal, never by Dropbox sharing.

Authentication uses a long-lived refresh token from a Dropbox app with
"App folder" access and the files.content.read / files.content.write scopes.
Short-lived access tokens are fetched and refreshed automatically.
Get a refresh token with: python -m app.cli dropbox-auth
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import httpx

from app.storage.base import StorageBackend, StorageError
from app.storage.crypto import Envelope

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"  # noqa: S105 - endpoint, not a secret
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"


class DropboxStorage(StorageBackend):
    name = "dropbox"

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        refresh_token: str,
        master_key: bytes,
        root: str = "/documents",
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
    ) -> None:
        self._creds = (app_key, app_secret, refresh_token)
        self._env = Envelope(master_key)
        self.root = "/" + root.strip("/") if root.strip("/") else ""
        self._http = httpx.Client(timeout=60, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._max_retries = max_retries

    # -- helpers ---------------------------------------------------------------
    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"{self.root}/{digest[:2]}/{digest}.bin"

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
                    raise StorageError(f"Dropbox sign-in failed ({resp.status_code})")
                body = resp.json()
                self._token = body["access_token"]
                self._token_expires = time.monotonic() + float(body.get("expires_in", 14400))
            return self._token

    def _call(
        self,
        url: str,
        *,
        arg: dict | None = None,
        content: bytes | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        refreshed = False
        for attempt in range(self._max_retries + 1):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            if arg is not None:
                headers["Dropbox-API-Arg"] = json.dumps(arg)  # ASCII-only by default
            if content is not None:
                headers["Content-Type"] = "application/octet-stream"
            try:
                resp = self._http.post(url, headers=headers, content=content, json=json_body)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise StorageError("Dropbox is unreachable") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._access_token(force=True)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    break
                time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                continue
            return resp
        raise StorageError(f"Dropbox request failed ({resp.status_code})")

    @staticmethod
    def _not_found(resp: httpx.Response) -> bool:
        return resp.status_code == 409 and "not_found" in resp.text

    # -- interface -----------------------------------------------------------
    def put(self, key: str, data: bytes, content_type: str) -> None:
        resp = self._call(
            f"{CONTENT}/files/upload",
            arg={"path": self._path(key), "mode": "overwrite", "mute": True},
            content=self._env.seal(key, data),
        )
        if resp.status_code != 200:
            raise StorageError(f"Dropbox upload failed ({resp.status_code})")

    def get(self, key: str) -> bytes:
        resp = self._call(f"{CONTENT}/files/download", arg={"path": self._path(key)})
        if self._not_found(resp):
            raise StorageError("object not found")
        if resp.status_code != 200:
            raise StorageError(f"Dropbox download failed ({resp.status_code})")
        return self._env.open(key, resp.content)

    def delete(self, key: str) -> None:
        resp = self._call(f"{API}/files/delete_v2", json_body={"path": self._path(key)})
        if resp.status_code != 200 and not self._not_found(resp):
            raise StorageError(f"Dropbox delete failed ({resp.status_code})")
