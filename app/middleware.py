"""Reject oversized requests using Content-Length, before the body is read.

Starlette spools the whole multipart body to disk before the endpoint runs, so the endpoint's
size check alone fires too late. Chunked requests (no Content-Length) still hit that check.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = dict(scope["headers"]).get(b"content-length")
            if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
                response = JSONResponse(
                    {"detail": f"Request body exceeds the maximum of {self.max_body_bytes} bytes."},
                    status_code=413,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
