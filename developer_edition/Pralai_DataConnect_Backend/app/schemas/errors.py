"""
The standard error envelope every DataConnect API error response is shaped
as — see app/core/errors.py for the exception hierarchy that produces it and
app/main.py for the exception handlers that render it. One shape for every
error, regardless of whether it came from a raised DataConnectError
subclass, a Pydantic validation failure, a bare HTTPException, or an
unhandled 500 — a frontend never has to special-case "is this response's
error a string or an object" per endpoint.
"""

from typing import Any

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer


class FieldError(BaseModel):
    """One field-level validation failure — `field` is a dotted path (e.g. "connection_config.hostname")."""

    field: str
    message: str
    #: A stable identifier a UI can switch on, when the failure has one (e.g. NOT_IMPLEMENTED for an option that does not exist yet).
    code: str | None = None

    @model_serializer(mode="wrap")
    def _omit_an_empty_code(self, handler: SerializerFunctionWrapHandler) -> dict:
        """`code` only appears when there is one, so every existing field error serializes exactly as it did before it existed."""
        data = handler(self)
        if data.get("code") is None:
            data.pop("code", None)
        return data


class ErrorEnvelope(BaseModel):
    #: Stable, machine-readable identifier a UI can switch on — see
    #: app/core/errors.py's DataConnectError subclasses for the fixed set
    #: this codebase raises deliberately (plus a handful FastAPI/infra
    #: failures map onto, like "DATABASE_UNAVAILABLE" for a database outage).
    code: str
    #: Safe to render directly to an end user — never a stack trace, a raw
    #: driver exception message, or anything else that might carry secrets.
    message: str
    #: Populated for validation-shaped failures (a request body that failed
    #: Pydantic validation, a connector config missing required fields, an
    #: invalid quality rule) — one entry per offending field. `None`, not an
    #: empty list, when the error isn't about specific fields at all.
    field_errors: list[FieldError] | None = None
    #: Echoes the same X-Request-Id this response's header carries (see
    #: app/main.py's request_id_middleware) — pass it back when reporting an
    #: issue so it's traceable in the backend's own structured logs.
    request_id: str | None = None
    #: Whether retrying the exact same request might succeed without the
    #: caller changing anything — true for transient upstream/backend
    #: failures (Kafka Connect unreachable, the database down, a timeout),
    #: false for anything the request itself caused (bad input, a conflict,
    #: a permission the caller doesn't have).
    retryable: bool = False
    #: Additional structured, UI-safe context specific to `code` — e.g. which
    #: connector class a Kafka Connect failure was for, or the competing
    #: schema version id a conflict was against. Never raw exception
    #: internals or anything secret-shaped.
    details: dict[str, Any] | None = None
