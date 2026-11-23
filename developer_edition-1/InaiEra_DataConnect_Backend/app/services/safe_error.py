"""
BUG-11: a single place that turns whatever exception a background runner
caught into something safe to persist and return to a caller.

Both background runners (app/services/operation_runner.py,
app/services/quality_job_runner.py — "both runners" this ticket refers to)
used to do roughly `error_message = str(exc)[:2000]` and log the same raw
string. A database/HTTP/Kafka driver's own exception text can carry a
username, hostname, URL, SQL, a file path, or a request payload — none of
that belongs in a column a caller (eventually a browser tab) reads back.

`to_safe_error` classifies the exception into a small, stable, non-leaking
(code, message, retryable) triple — the message is always one of a fixed
set of generic sentences, never anything derived from the exception's own
text — and logs the REAL exception (redacted by app/core/redaction.py — credentials, user names, SQL literals, e-mail
addresses and file paths masked, since a driver's message is outside this app's control, but otherwise left detailed)
tagged with a fresh correlation_id, so an operator can cross-reference the generic
message a caller sees back to the real cause without ever exposing it to
them. The persisted/returned SafeError and the diagnostic log entry are
two different audiences reading two different amounts of detail about the
same failure — that's the split the ticket asks for, not just a smaller
character cap.
"""

from __future__ import annotations

import asyncio
import errno
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import structlog
from kafka.errors import KafkaError

from app.core.errors import (
    DataConnectError,
    KafkaConnectConfigError,
    KafkaConnectError,
    TopicProvisioningError,
)
from app.core.network_policy import DestinationBlockedError
from app.core.redaction import redact_diagnostic, redact_secrets

logger = structlog.get_logger(__name__)

@dataclass(frozen=True)
class SafeError:
    code: str
    message: str
    retryable: bool
    correlation_id: str

    def describe(self) -> str:
        """One line for a single text column or message (a pipeline's runtime error, a discovery run's error): the
        fixed message, the stable code, and the reference that finds the detailed log entry."""
        return f"{self.message} [{self.code}, ref {self.correlation_id}]"


# Exception class names / message fragments that mean "the source rejected the credentials", across drivers that share no
# base class (asyncpg InvalidPasswordError, aiomysql OperationalError 1045, pymongo OperationFailure 18, oracledb
# DatabaseError ORA-01017, pytds, ftplib error_perm "530 ...").
_AUTH_CLASS_RE = re.compile(r"(?i)(invalidpassword|invalidauthorization|authentication|authfailed|accessdenied|loginfailed)")
_AUTH_MESSAGE_RE = re.compile(
    r"(?i)(password authentication failed|access denied for user|authentication failed|login failed|"
    r"invalid (?:user|credentials|password)|ora-01017|\b530\b.*(?:login|not logged in)|not authorized|unauthorized)"
)
_NETWORK_ERRNOS = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED}


def _is_auth_failure(exc: Exception) -> bool:
    return bool(_AUTH_CLASS_RE.search(type(exc).__name__) or _AUTH_MESSAGE_RE.search(str(exc)))


def _connect_config_settings(exc: Exception) -> list[str]:
    """The names of the connector SETTINGS Kafka Connect rejected, pulled out of its response text. Names only — the
    text around them quotes the values (a JDBC URL, a host), which is exactly what must not be stored."""
    names = re.findall(r"for configuration ([A-Za-z0-9_.\-]+)", str(exc))
    names += re.findall(r"Missing required configuration \"([A-Za-z0-9_.\-]+)\"", str(exc))
    # The validator's own wording (e.g. a Debezium setting with a value it does not accept): "The 'snapshot.mode' value is invalid: ..."
    names += re.findall(r"The '([A-Za-z0-9_.\-]+)' value is invalid", str(exc))
    return sorted(dict.fromkeys(names))[:10]


def _classify(
    exc: Exception, timeout_seconds: float | None = None, target: str = "source",
) -> tuple[str, str, bool]:
    """(code, message, retryable) for a known exception family. `message`
    is always a fixed, generic sentence — it must never interpolate
    anything from `exc` itself, since that's exactly the leak this module
    exists to prevent. (Two deliberate exceptions, both NOT external text: the caller-supplied
    `timeout_seconds`, and the names of connector settings Kafka Connect rejected.)"""
    if isinstance(exc, KafkaConnectConfigError):
        # Connect's response text quotes the rejected VALUES (JDBC URLs, hosts) — keep only which settings it objected to.
        settings_named = _connect_config_settings(exc)
        suffix = f" Settings it objected to: {', '.join(settings_named)}." if settings_named else ""
        return "KAFKA_CONNECT_CONFIG_INVALID", f"Kafka Connect rejected the connector configuration.{suffix}", False
    if isinstance(exc, KafkaConnectError):
        return "KAFKA_CONNECT_UNAVAILABLE", "Kafka Connect is unavailable or returned an error.", True
    if isinstance(exc, TopicProvisioningError):
        return "TOPIC_CREATE_FAILED", "The Kafka topic could not be created or verified.", True
    if isinstance(exc, DataConnectError):
        # Already a deliberately UI-safe error (see app/core/errors.py's own
        # module docstring: "every ... error this codebase raises
        # deliberately") — its own code/message/retryable are reused
        # directly, uppercased to match this module's SCREAMING_SNAKE
        # convention, rather than re-classified into one of the generic
        # buckets below. Still passed through the secret redaction as a net.
        return exc.code.upper(), redact_secrets(exc.message) or exc.code, exc.retryable
    if target == "kafka_connect" and isinstance(
        exc, TimeoutError | asyncio.TimeoutError | httpx.TransportError | ConnectionError | socket.gaierror
    ):
        # The caller was talking to Kafka Connect, not a customer's source: "the source did not respond" would mislead.
        return "KAFKA_CONNECT_UNAVAILABLE", "Kafka Connect is unavailable or returned an error.", True
    if isinstance(exc, TimeoutError | asyncio.TimeoutError | httpx.TimeoutException) or (
        isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT
    ):
        within = f"within {timeout_seconds:g} seconds" if timeout_seconds else "within the configured timeout"
        return "SOURCE_CONNECTION_TIMEOUT", f"The source did not respond {within}.", True
    if isinstance(exc, DestinationBlockedError) and isinstance(exc.__cause__, socket.gaierror):
        # network_policy reports an unresolvable host as a block; it is a failure to find the source.
        return "SOURCE_HOST_NOT_FOUND", "The source host name could not be resolved.", False
    if isinstance(exc, DestinationBlockedError):
        return "DESTINATION_BLOCKED", "The destination host is not permitted by network policy.", False
    if _is_auth_failure(exc):
        return "SOURCE_AUTH_FAILED", "The source rejected the supplied credentials.", False
    if isinstance(exc, ssl.SSLError):
        return "SOURCE_TLS_ERROR", "A secure connection to the source could not be established.", False
    if isinstance(exc, socket.gaierror):
        return "SOURCE_HOST_NOT_FOUND", "The source host name could not be resolved.", False
    if isinstance(exc, httpx.ConnectError | ConnectionError) or (
        isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS
    ):
        return "SOURCE_CONNECTION_REFUSED", "Could not connect to the source system.", True
    if isinstance(exc, httpx.HTTPError):
        return "SOURCE_HTTP_ERROR", "The source system returned an HTTP error.", True
    if isinstance(exc, KafkaError):
        return "KAFKA_UNAVAILABLE", "Could not reach Kafka.", True
    if isinstance(exc, ModuleNotFoundError):
        return "DRIVER_NOT_INSTALLED", "A required driver is not installed on this worker.", False
    if isinstance(exc, FileNotFoundError | NotADirectoryError):
        return "SOURCE_NOT_FOUND", "The requested source path or object was not found.", False
    # Genuinely unclassified — a programming bug (KeyError, AttributeError, ...)
    # or an exception type this module doesn't know about yet. Never
    # retryable by default: retrying a bug in a loop is worse than surfacing
    # it once, and a newly-added exception type should earn a real
    # classification above rather than silently inheriting retry-forever
    # behavior. FE-BUG-04: OPERATION_FAILED, not a generic INTERNAL_ERROR —
    # this is specifically the fallback for a background Operation/
    # QualityExecutionJob's own run failing for an unclassified reason.
    return "OPERATION_FAILED", "An unexpected error occurred while running this operation.", False


def to_safe_error(
    exc: Exception, *, event: str, timeout_seconds: float | None = None, target: str = "source", **log_context: Any,
) -> SafeError:
    """Classifies `exc`, logs it (redacted, detail preserved) under `event` tagged with a fresh correlation_id, and
    returns the SafeError callers should persist/return instead of `str(exc)`. Call this exactly once, at the point a
    background runner catches the exception it's about to record — never re-derive a SafeError from an already-generic
    message. `timeout_seconds`, when the caller knows the limit that was hit, makes a timeout say so; `target="kafka_connect"`
    says the caller was talking to Kafka Connect, so a network failure is reported as Connect being unavailable, not
    as the customer's source."""
    correlation_id = uuid4().hex[:12]
    code, message, retryable = _classify(exc, timeout_seconds, target)
    diagnostic = redact_diagnostic(str(exc))
    logger.error(
        event, correlation_id=correlation_id, code=code, exception_type=type(exc).__name__,
        diagnostic=diagnostic, exc_info=exc, **log_context,
    )
    return SafeError(code=code, message=message, retryable=retryable, correlation_id=correlation_id)


def manual_safe_error(
    code: str, message: str, *, retryable: bool, event: str, **log_context: Any,
) -> SafeError:
    """Same correlation-id-and-log treatment as to_safe_error, for a
    terminal failure that isn't a caught exception (e.g. operation_runner's
    attempt-count exhaustion or missing work-factory registration) but
    should still carry the same structured shape as one that is."""
    correlation_id = uuid4().hex[:12]
    logger.error(event, correlation_id=correlation_id, code=code, **log_context)
    return SafeError(code=code, message=message, retryable=retryable, correlation_id=correlation_id)
