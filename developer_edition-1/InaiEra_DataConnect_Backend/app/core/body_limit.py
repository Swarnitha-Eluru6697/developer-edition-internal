"""
Refuses an oversized request body BEFORE it is read into memory (GAP-13).

Record-count and size limits applied after FastAPI has parsed a body are too late: by then the whole payload, however
large, is already a Python object in the API process. This ASGI middleware enforces a byte limit on the routes that
take bulk payloads, in two ways: from the declared Content-Length (rejected without reading a byte), and, for a body
with no honest Content-Length (chunked transfer, or a lying header), by counting bytes as they stream in and stopping at
the limit.

It speaks the same error envelope as every other API error (app/schemas/errors.py), so a client handles it like any other.
"""

import json
import re
from collections.abc import Callable
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODY_METHODS = {"POST", "PUT", "PATCH"}


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: list[tuple[re.Pattern[str], Callable[[], int]]]) -> None:
        """`rules`: (path pattern, callable returning the byte limit). A callable, not a number, so a setting changed at
        runtime (and tests) is honored without rebuilding the app."""
        self.app = app
        self.rules = rules

    def _limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            return None
        for pattern, limit in self.rules:
            if pattern.search(scope["path"]):
                return limit()
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, send, limit)
            return

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not response_started:
                await self._reject(scope, send, limit)
            # else: the handler already began answering; nothing sane to add

    async def _reject(self, scope: Scope, send: Send, limit: int) -> None:
        request_id = next((v.decode() for k, v in scope["headers"] if k.lower() == b"x-request-id"), str(uuid4()))
        body = json.dumps({
            "code": "payload_too_large",
            "message": f"The request body is larger than the {limit // 1024 // 1024} MiB this endpoint accepts. "
                       "Submit a smaller batch.",
            "field_errors": None, "request_id": request_id, "retryable": False, "details": {"limit_bytes": limit},
        }).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ]})
        await send({"type": "http.response.body", "body": body})
