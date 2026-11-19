"""Test fixtures.

Tests run on SQLite by default. Set TEST_DATABASE_URL to a Postgres database
with pgvector available to run the same suite against Postgres (CI does both).
"""

from __future__ import annotations

import os
import re
import tempfile

_tmp = tempfile.mkdtemp(prefix="hoa-tests-")
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db",
        "LLM_PROVIDER": "offline",
        "EMBEDDING_PROVIDER": "hashing",
        "EMAIL_PROVIDER": "console",
        "EMAIL_SEND_INLINE": "true",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_DIR": f"{_tmp}/files",
        "STORAGE_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "BASE_URL": "http://testserver",
        "SECRET_KEY": "test-secret-key-test-secret-key-test",
        "MANAGEMENT_NOTIFY_EMAILS": "office@management.example",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db as app_db  # noqa: E402
from app.db import Base  # noqa: E402
from app.email.providers import get_provider  # noqa: E402
from app.models import User  # noqa: E402
from app.permissions import Role  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def database():
    app_db.configure(os.environ["DATABASE_URL"])
    engine = app_db.get_engine()
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_provider().sent.clear()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = app_db.session_factory()()
    yield session
    session.close()


@pytest.fixture
def outbox():
    return get_provider().sent


@pytest.fixture
def make_user(db):
    def _make(role: Role, email: str | None = None, name: str = "", active: bool = True) -> User:
        email = email or f"{role.value}@example.com"
        user = User(
            email=email,
            full_name=name or role.label,
            role=role.value,
            password_hash=hash_password(PASSWORD),
            is_active=active,
        )
        db.add(user)
        db.commit()
        return user

    return _make


class Client(TestClient):
    """TestClient that knows how to sign in and attach the CSRF token."""

    def csrf(self) -> str:
        token = self.cookies.get("hoa_csrf")
        if not token:
            self.get("/login")
            token = self.cookies.get("hoa_csrf")
        return token

    def post_form(self, url: str, data: dict | None = None, files=None, **kw):
        data = {**(data or {}), "csrf_token": self.csrf()}
        return self.post(url, data=data, files=files, follow_redirects=False, **kw)

    def login(self, email: str, password: str = PASSWORD):
        resp = self.post_form("/login", {"email": email, "password": password})
        assert resp.status_code == 303, resp.text
        return resp


@pytest.fixture
def client():
    from app.main import create_app

    with Client(create_app()) as c:
        yield c


@pytest.fixture
def client_for(make_user):
    """client_for(Role.BOARD) -> a fresh client signed in as a new user with that role."""
    from app.main import create_app

    clients = []

    def _for(role: Role, email: str | None = None):
        user = make_user(role, email)
        c = Client(create_app())
        c.__enter__()
        c.login(user.email)
        c.user = user
        clients.append(c)
        return c

    yield _for
    for c in clients:
        c.__exit__(None, None, None)


def link_from(text_body: str) -> str:
    match = re.search(r"http://testserver(/\S+)", text_body)
    assert match, text_body
    return match.group(1)
