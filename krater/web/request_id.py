"""`RequestIdMiddleware`: a per-request id, for tying together everything one request logs.

Generated server-side (never taken from an incoming header) so a client can't inject an arbitrary string
into every log line this request produces. Stashed on `request.state.request_id`, set on the
`krater.web.logging_config.request_id_var` contextvar for the duration of the request (so log records
emitted anywhere while handling it -- routers, services, the exception handler -- pick it up without it
being threaded through every call), and echoed back as `X-Request-ID` so it can be handed to support.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from krater.web.logging_config import new_request_id, request_id_var

RESPONSE_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = new_request_id()
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[RESPONSE_HEADER] = request_id
        return response


__all__ = ["RESPONSE_HEADER", "RequestIdMiddleware"]
