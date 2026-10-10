"""The FastAPI application factory: `create_app()`.

Run with: `uv run uvicorn krater.web.app:create_app --factory --reload`
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from krater.config import get_settings
from krater.services.errors import NotAllowed, NotFound
from krater.web.logging_config import configure_logging, request_id_var
from krater.web.rate_limit import RateLimitMiddleware
from krater.web.request_id import RESPONSE_HEADER as REQUEST_ID_HEADER
from krater.web.request_id import RequestIdMiddleware
from krater.web.routers import (
    admin,
    auth,
    gallery,
    pages,
    pricing,
    projects,
    reviews,
    skypilot_policy,
    slack_interactions,
)
from krater.web.security_headers import SecurityHeadersMiddleware, apply_security_headers
from krater.web.templates import templates
from krater.worker.app import app as procrastinate_app

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE_NAME = "krater_session"
_HTML_ERROR_PAGES = {403: "errors/403.html", 404: "errors/404.html"}

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app
    # Opens the procrastinate connector's own connection pool, so routers can defer jobs (e.g. Slack
    # channel/message work) with a plain synchronous `Task.defer(...)` -- see krater/worker/app.py.
    procrastinate_app.open()
    try:
        yield
    finally:
        procrastinate_app.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(title="Krater", lifespan=_lifespan)

    # Middleware order matters here (see each middleware's own docstring):
    #  - `RequestIdMiddleware` outermost, so every log line from everything inside it -- including
    #    session handling and the security-headers pass -- carries the same request id.
    #  - `SessionMiddleware` next, so `request.session` is populated before `RateLimitMiddleware` (which
    #    keys general POSTs by signed-in user id when there is one) ever needs it.
    #  - `SecurityHeadersMiddleware` stamps every response, including a 429 from the rate limiter below
    #    it or a 403/404/500 from the exception handlers further in.
    #  - `RateLimitMiddleware` innermost, closest to the actual routes.
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE_NAME,
        max_age=settings.session_cookie_max_age_seconds,
        same_site="lax",
        https_only=settings.env == "production",
    )
    app.add_middleware(RequestIdMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(reviews.router)
    app.include_router(admin.router)
    app.include_router(gallery.router)
    app.include_router(pricing.router)
    app.include_router(skypilot_policy.router)
    app.include_router(slack_interactions.router)

    @app.exception_handler(NotAllowed)
    def _handle_not_allowed(request: Request, exc: NotAllowed):
        return templates.TemplateResponse(request, "errors/403.html", status_code=403)

    @app.exception_handler(NotFound)
    def _handle_not_found(request: Request, exc: NotFound):
        return templates.TemplateResponse(request, "errors/404.html", status_code=404)

    # Unknown URLs and the auth dependencies' 403s are plain `HTTPException`s, which FastAPI renders as
    # JSON. Browsers get the site's error pages instead; API callers (SkyPilot's policy hook, Slack,
    # the estimator's fetch) don't ask for HTML, so they keep the JSON body.
    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code in _HTML_ERROR_PAGES and "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(
                request, _HTML_ERROR_PAGES[exc.status_code], status_code=exc.status_code, headers=exc.headers
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    def _handle_unexpected_error(request: Request, exc: Exception):
        # This runs outside `RequestIdMiddleware` (see below), which has already reset the request id by now;
        # put it back from `request.state` (shared with the middleware through the ASGI scope) for this line.
        request_id = getattr(request.state, "request_id", None)
        token = request_id_var.set(request_id)
        try:
            logger.exception("unhandled exception handling %s %s", request.method, request.url.path)
        finally:
            request_id_var.reset(token)
        # Outside production, let it propagate: local dev/tests want the real traceback, not a page
        # asking them to look at logs that are, in this case, right there in the terminal.
        if get_settings().env != "production":
            raise exc
        response = templates.TemplateResponse(request, "errors/500.html", {"request_id": request_id}, status_code=500)
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        # This handler runs on Starlette's `ServerErrorMiddleware`, outside `SecurityHeadersMiddleware` --
        # see `apply_security_headers`'s docstring for why it has to be called directly here too.
        apply_security_headers(response, get_settings())
        return response

    return app
