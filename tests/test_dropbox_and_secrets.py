"""Dropbox storage (against a fake Dropbox API) and loading settings from Secrets Manager."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from app.config import SECRETS_ID_ENV, SecretsManagerSource, Settings
from app.permissions import Role
from app.storage.base import StorageError
from app.storage.dropbox import DropboxStorage

KEY = b"k" * 32


class FakeDropbox:
    """Just enough of the Dropbox HTTP API: token refresh, upload, download, delete."""

    def __init__(self, token_lifetime: int = 14400):
        self.files: dict[str, bytes] = {}
        self.token_calls = 0
        self.valid_tokens: set[str] = set()
        self.token_lifetime = token_lifetime
        self.fail_next: list[int] = []

    def revoke_tokens(self):
        self.valid_tokens.clear()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/token"):
            self.token_calls += 1
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == "refresh-placeholder"
            assert request.headers["authorization"].startswith("Basic ")
            token = f"token-{self.token_calls}"
            self.valid_tokens.add(token)
            return httpx.Response(
                200, json={"access_token": token, "expires_in": self.token_lifetime}
            )
        if request.headers.get("authorization", "")[7:] not in self.valid_tokens:
            return httpx.Response(401, json={"error_summary": "expired_access_token/"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"})
        if url.endswith("/files/upload"):
            arg = json.loads(request.headers["dropbox-api-arg"])
            assert arg["mode"] == "overwrite"
            self.files[arg["path"]] = request.content
            return httpx.Response(200, json={"path_display": arg["path"]})
        if url.endswith("/files/download"):
            path = json.loads(request.headers["dropbox-api-arg"])["path"]
            if path not in self.files:
                return httpx.Response(409, json={"error_summary": "path/not_found/"})
            return httpx.Response(200, content=self.files[path])
        if url.endswith("/files/delete_v2"):
            path = json.loads(request.content)["path"]
            if self.files.pop(path, None) is None:
                return httpx.Response(409, json={"error_summary": "path_lookup/not_found/"})
            return httpx.Response(200, json={})
        return httpx.Response(404)


def make_store(fake: FakeDropbox, key: bytes = KEY) -> DropboxStorage:
    return DropboxStorage(
        app_key="app-placeholder",
        app_secret="secret-placeholder",
        refresh_token="refresh-placeholder",
        master_key=key,
        root="/documents",
        transport=httpx.MockTransport(fake),
    )


def test_round_trip_stores_only_ciphertext_under_opaque_names():
    fake = FakeDropbox()
    store = make_store(fake)
    store.put("doc-key-1", b"Board-only executive minutes", "text/plain")
    [(path, blob)] = fake.files.items()
    assert path.startswith("/documents/") and path.endswith(".bin")
    assert "doc-key-1" not in path
    assert b"executive" not in blob
    assert store.get("doc-key-1") == b"Board-only executive minutes"
    assert fake.token_calls == 1  # the access token is reused


def test_wrong_key_or_tampering_is_detected():
    fake = FakeDropbox()
    make_store(fake).put("k1", b"data", "text/plain")
    with pytest.raises(StorageError):
        make_store(fake, key=b"x" * 32).get("k1")
    path = next(iter(fake.files))
    fake.files[path] = fake.files[path][:-1] + bytes([fake.files[path][-1] ^ 1])
    with pytest.raises(StorageError):
        make_store(fake).get("k1")


def test_expired_token_is_refreshed_and_transient_errors_retried():
    fake = FakeDropbox()
    store = make_store(fake)
    store.put("k1", b"one", "text/plain")
    fake.revoke_tokens()  # Dropbox expired the access token early
    assert store.get("k1") == b"one"
    assert fake.token_calls == 2
    fake.fail_next = [429, 503]
    store.put("k2", b"two", "text/plain")
    assert store.get("k2") == b"two"


def test_missing_files():
    fake = FakeDropbox()
    store = make_store(fake)
    with pytest.raises(StorageError, match="not found"):
        store.get("nope")
    store.delete("nope")  # deleting something already gone is fine


def test_documents_work_end_to_end_on_dropbox(client_for, monkeypatch, db):
    """Upload, permission checks, chatbot indexing and delete, with Dropbox as the store."""
    from app.routes import documents as doc_routes
    from app.services import documents as doc_service

    fake = FakeDropbox()
    store = make_store(fake)
    monkeypatch.setattr(doc_service, "get_storage", lambda: store)
    monkeypatch.setattr(doc_routes, "get_storage", lambda: store)

    board = client_for(Role.BOARD)
    owner = client_for(Role.HOMEOWNER)
    resp = board.post_form(
        "/documents",
        {"title": "Minutes", "description": "", "roles": ["board"]},
        files={
            "file": ("m.md", io.BytesIO(b"# Minutes\n\nLot 7 goes to counsel."), "text/markdown")
        },
    )
    assert resp.status_code == 303
    did = int(resp.headers["location"].rsplit("/", 1)[1])
    assert len(fake.files) == 1 and b"counsel" not in next(iter(fake.files.values()))
    assert board.get(f"/documents/{did}/download").content.endswith(b"Lot 7 goes to counsel.")
    assert owner.get(f"/documents/{did}/download").status_code == 404
    board.post_form(f"/documents/{did}/delete")
    assert fake.files == {}


class FakeSecretsManager:
    def __init__(self, secret: dict | str):
        self.secret = secret if isinstance(secret, str) else json.dumps(secret)
        self.calls: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's argument name
        self.calls.append(SecretId)
        return {"SecretString": self.secret}


def _settings_with(client, **init) -> Settings:
    class S(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, **_):
            return (init_settings, env_settings, SecretsManagerSource(settings_cls, client))

    return S(**init)


def test_settings_load_from_secrets_manager(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    fake = FakeSecretsManager(
        {
            "SECRET_KEY": "s" * 40,
            "DROPBOX_REFRESH_TOKEN": "refresh-placeholder",
            "ANTHROPIC_API_KEY": "api-placeholder",
            "SOMETHING_ELSE": "ignored",
        }
    )
    s = _settings_with(fake)
    assert fake.calls == ["hoa-portal/production"]
    assert s.secret_key == "s" * 40
    assert s.dropbox_refresh_token == "refresh-placeholder"
    assert s.anthropic_api_key == "api-placeholder"
    # Secrets never show up in logs or tracebacks.
    assert "refresh-placeholder" not in repr(s) and "api-placeholder" not in repr(s)


def test_environment_overrides_secret_and_no_secret_id_means_no_call(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    monkeypatch.setenv("HOA_NAME", "From env")
    s = _settings_with(FakeSecretsManager({"HOA_NAME": "From secret"}))
    assert s.hoa_name == "From env"

    monkeypatch.delenv(SECRETS_ID_ENV)
    fake = FakeSecretsManager({})
    _settings_with(fake)
    assert fake.calls == []


def test_secret_must_be_a_json_object(monkeypatch):
    monkeypatch.setenv(SECRETS_ID_ENV, "hoa-portal/production")
    with pytest.raises(ValueError, match="JSON"):
        _settings_with(FakeSecretsManager("not json"))


def test_production_requires_dropbox_credentials():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="DROPBOX_REFRESH_TOKEN"):
        Settings(
            app_env="production",
            storage_backend="dropbox",
            storage_encryption_key="placeholder",
        )
