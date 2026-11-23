"""
Real, ASGI-level request-body-size enforcement (§0.111, GAP_REPORT) —
the actual fix for a real, reported DoS gap: the upload endpoint's own
`data = await file.read()` (routers/uploads.py) read the ENTIRE real
request body into memory/a real spooled temp file BEFORE its own
MAX_UPLOAD_BYTES check ever ran — a much-larger-than-declared upload
could genuinely consume real memory/request resources well before
ever being rejected.

A fix living entirely inside the route handler cannot actually close
this: confirmed directly against Starlette's own real source
(`starlette.formparsers.MultiPartParser.parse`) — its own multipart
parser already writes every real incoming chunk to the UploadFile's
own underlying SpooledTemporaryFile AS IT ARRIVES, and this genuinely
happens BEFORE FastAPI's own dependency-injection machinery ever
hands control to the route handler at all. By the time a route
handler's own code runs, the real damage (the full body already
buffered) has already happened, regardless of how that handler then
chooses to read it.

The real, effective place to enforce this is one layer below Starlette's
own form parsing: a genuine ASGI middleware (not `BaseHTTPMiddleware`,
which itself sits above this same layer) wrapping the raw `receive`
callable, counting real bytes as they stream in off the wire, and
rejecting with a real, honest 413 the instant the running total would
exceed the real, configured limit — closing the gap at the one layer
that can actually see (and stop) the raw byte stream before anything
above it ever buffers the excess.
"""
from starlette.responses import JSONResponse


class _RequestBodyTooLarge(Exception):
    """Real, internal sentinel — never a real error surfaced to a
    caller. Raised from within this middleware's own wrapped
    `receive()` the instant the running byte count exceeds the real,
    configured limit, so the inner ASGI app's own `await receive()`
    call itself raises and unwinds immediately, rather than a
    same-shaped real HTTPException the inner app's own exception
    handlers might otherwise try to process (and, worse, might try to
    send a SECOND real response for, after this middleware's own
    genuine 413 has already gone out over the wire)."""


class MaxRequestBodySizeMiddleware:
    """Real, pure ASGI middleware (§0.111, GAP_REPORT) — deliberately
    NOT a `starlette.middleware.base.BaseHTTPMiddleware` subclass,
    which sits at the same real layer request-body reads already
    happen at and would not close the actual reported gap. This
    implements the raw, three-argument ASGI callable interface
    directly, so it can genuinely intercept and count every real
    `http.request` message's own body bytes as they arrive off the
    wire, before Starlette's own multipart/form parser (or anything
    else above this layer) ever gets to buffer them.

    `path_prefixes` scopes this real enforcement to specific real
    routes (e.g. the upload endpoint) rather than every real request
    this app serves — a real, deliberate choice: most real endpoints
    here take small, real JSON bodies with no genuine need for this
    real check, and a single, global cap would either be too generous
    to matter for the one, real endpoint that actually needs it, or
    too strict for a real, legitimate JSON payload elsewhere.

    On a real, genuine overage, this sends one real, honest
    `413 Request Entity Too Large` response directly over the wire and
    stops the inner app's own processing cleanly — the wrapped `send`
    below discards anything the inner app's own exception-handling
    middleware might otherwise still try to emit afterward (Starlette's
    own internal error-handling layers sit ABOVE this middleware in the
    real call stack and would otherwise attempt to send their own,
    second, real response once this middleware's own sentinel exception
    unwinds through them — a real, confirmed "multiple response.start"
    ASGI protocol violation without this)."""

    def __init__(self, app, max_bytes: int, path_prefixes: tuple[str, ...],
                 overrides: "dict[str, int] | None" = None):
        """`max_bytes` applies to every path under `path_prefixes`;
        `overrides` raises (or lowers) it for a more specific prefix.

        The override map exists because a single global cap is the wrong
        shape: this middleware was scoped to /api/uploads alone, which
        left every JSON endpoint — flows, prompts, structured inputs,
        batches, configuration — with no body limit at all, while a
        global 20MB cap would have been both far too generous for a JSON
        body and far too small for a document upload. The most SPECIFIC
        matching prefix wins, so /api gets a modest default and the
        upload paths keep the ceiling they need."""
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefixes = path_prefixes
        # Longest first, so the most specific prefix is found first.
        # Keys beginning "re:" are regular expressions matched against the
        # whole path (for routes with an id in the middle, such as
        # /api/agents/{id}/skills); everything else is a plain prefix.
        import re as _re
        ov = overrides or {}
        self.pattern_overrides = tuple((_re.compile(k[3:]), v) for k, v in ov.items() if k.startswith("re:"))
        self.overrides = tuple(sorted(((k, v) for k, v in ov.items() if not k.startswith("re:")),
                                      key=lambda kv: -len(kv[0])))

    def _limit_for(self, path: str) -> "int | None":
        for rx, limit in self.pattern_overrides:
            if rx.fullmatch(path):
                return limit
        for prefix, limit in self.overrides:
            if path.startswith(prefix):
                return limit
        if any(path.startswith(p) for p in self.path_prefixes):
            return self.max_bytes
        return None

    async def __call__(self, scope, receive, send):
        limit = self._limit_for(scope["path"]) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return

        total = 0
        state = {"rejected": False}

        async def guarded_send(message):
            if state["rejected"]:
                return
            await send(message)

        async def guarded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit and not state["rejected"]:
                    state["rejected"] = True
                    readable = (f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024
                                else f"{limit // 1024} KB")
                    # JSON with `detail`, like every other API error, so the UI
                    # can say why instead of a bare "Request failed: 413".
                    response = JSONResponse(
                        {"detail": f"This file is too large to upload. Files must be {readable} or smaller."},
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    raise _RequestBodyTooLarge()
            return message

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except _RequestBodyTooLarge:
            pass  # this middleware's own real 413 has already been sent above; nothing further to do
