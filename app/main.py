"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import session_factory
from app.routes import (
    account,
    admin,
    auth,
    blasts,
    chat,
    documents,
    duties,
    inbox,
    reservations,
    weekly_review,
)
from app.security.auth import (
    CSRF_COOKIE,
    CSRFError,
    Forbidden,
    LoginRequired,
    csrf_protect,
    set_csrf_cookie,
)
from app.web import FLASH_COOKIE, redirect, render

log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(
        title=f"{settings.hoa_name} portal",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    protected = [Depends(csrf_protect)]
    modules = (
        auth,
        chat,
        documents,
        reservations,
        duties,
        inbox,
        weekly_review,
        admin,
        blasts,
        account,
    )
    for module in modules:
        app.include_router(module.router, dependencies=protected)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        db = session_factory()()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return PlainTextResponse("ok")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if settings.session_cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        if "text/html" in response.headers.get("content-type", ""):
            response.headers.setdefault("Cache-Control", "no-store")
        # Persist a CSRF token created while rendering, unless login rotated it.
        token = getattr(request.state, "csrf_token", None)
        already_set = any(
            h.startswith(f"{CSRF_COOKIE}=") for h in response.headers.getlist("set-cookie")
        )
        if token and request.cookies.get(CSRF_COOKIE) != token and not already_set:
            set_csrf_cookie(response, token)
        if getattr(request.state, "clear_flash", False):
            response.delete_cookie(FLASH_COOKIE)
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return redirect(f"/login?next={nxt}" if request.method == "GET" else "/login")

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return render(
            request,
            "error.html",
            403,
            title="Not allowed",
            message="Your account doesn't have access to that page.",
        )

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, exc: CSRFError):
        return render(
            request,
            "error.html",
            403,
            title="Please try again",
            message="Your form expired. Go back, refresh the page and try again.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        titles = {404: "Not found", 403: "Not allowed", 400: "Something's not right"}
        message = {
            404: "That page doesn't exist, or you don't have access to it.",
            403: "Your account doesn't have access to that.",
        }.get(exc.status_code, str(exc.detail) if exc.detail else "Please try again.")
        return render(
            request,
            "error.html",
            exc.status_code,
            title=titles.get(exc.status_code, "Error"),
            message=message,
        )

    _ = FastAPIHTTPException  # FastAPI's HTTPException subclasses Starlette's
    return app


app = create_app()
