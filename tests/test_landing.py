"""The sign-in page shows a swappable photo when one is in app/static."""

from __future__ import annotations

import pytest

from app import web


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    web.landing_photo.cache_clear()
    yield tmp_path
    web.landing_photo.cache_clear()


def test_no_photo_file_means_no_photo(client, static_dir):
    html = client.get("/login").text
    assert "landing-photo" not in html
    assert "Sign in" in html


def test_photo_and_credit_show_above_the_form(client, static_dir):
    (static_dir / "landing.png").write_bytes(b"\x89PNG")
    (static_dir / "landing-credit.txt").write_text("Photo: Someone on Unsplash\nignored line\n")
    html = client.get("/login").text
    assert '<img src="/static/landing.png" alt="">' in html
    assert "Photo: Someone on Unsplash" in html
    assert "ignored line" not in html
    assert html.index("landing-photo") < html.index('action="/login"')


def test_photo_without_credit(client, static_dir):
    (static_dir / "landing.jpg").write_bytes(b"\xff\xd8")
    html = client.get("/login").text
    assert "/static/landing.jpg" in html
    assert "<figcaption" not in html
