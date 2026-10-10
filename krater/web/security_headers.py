"""`SecurityHeadersMiddleware`: browser-security response headers on every response.

Applied once, near the top of the middleware stack (see `krater.web.app`), so it covers ordinary page
renders, redirects, JSON responses (the screenshot widget's `fetch` calls), and error pages (403/404/500)
alike -- whatever comes back from `call_next` gets the same headers stamped on before it goes further out.

The CSP is the interesting one: `default-src 'self'` with no `'unsafe-inline'` anywhere, which is why
every template's JS lives in `krater/web/static/js/*.js` (loaded via `<script src>`) instead of an inline
`<script>` or an `on*=` attribute -- either would be silently blocked. The one deliberate exception is the
S3-compatible object store's public origin (`KRATER_S3_PUBLIC_ENDPOINT_URL`), added to `img-src` (gallery
thumbnails, presigned `GET` URLs), `connect-src` (the screenshot widget's `fetch` straight to storage) and
`form-action` (belt-and-suspenders, in case that upload is ever a plain `<form>` post instead of `fetch`).
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from krater.config import Settings

#: Locked down to nothing: this app embeds nothing and needs none of these.
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"


def _origin(url: str) -> str | None:
    """`scheme://host[:port]` for `url`, or `None` if it isn't a usable absolute URL (e.g. unset, or a
    bare `fake` value from `KRATER_S3_MODE=fake`'s in-memory store, which never serves a browser-facing
    URL)."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def build_csp(settings: Settings) -> str:
    """The `Content-Security-Policy` header value for `settings`."""
    s3_origin = _origin(settings.s3_public_endpoint_url)
    img_src = "'self'" + (f" {s3_origin}" if s3_origin else "")
    connect_src = "'self'" + (f" {s3_origin}" if s3_origin else "")
    form_action = "'self'" + (f" {s3_origin}" if s3_origin else "")

    directives = [
        "default-src 'self'",
        f"img-src {img_src}",
        f"connect-src {connect_src}",
        f"form-action {form_action}",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
    return "; ".join(directives)


def apply_security_headers(response: Response, settings: Settings) -> None:
    """Stamp CSP and friends onto `response` in place.

    Factored out of the middleware below so `krater.web.app`'s catch-all `Exception` handler can call it
    too: that handler ends up registered on Starlette's `ServerErrorMiddleware`, which sits *outside*
    every middleware this app adds (including `SecurityHeadersMiddleware`) -- an exception propagating up
    through `call_next` never gives the middleware a response to add headers to, so a 500 needs this
    applied directly instead.
    """
    response.headers["Content-Security-Policy"] = build_csp(settings)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY
    # CSP's `frame-ancestors` already does this for modern browsers; kept for older ones.
    response.headers["X-Frame-Options"] = "DENY"
    if settings.env == "production":
        # 2 years, subdomains included -- standard "submit to the HSTS preload list" numbers.
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamps CSP and friends onto every response. See the module docstring."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        apply_security_headers(response, self._settings)
        return response


__all__ = ["SecurityHeadersMiddleware", "apply_security_headers", "build_csp"]
