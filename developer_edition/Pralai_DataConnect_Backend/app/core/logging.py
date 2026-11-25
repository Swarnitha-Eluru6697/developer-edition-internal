"""Structured JSON logging via structlog."""
import logging
import sys

import structlog

from app.core.config import settings
from app.core.redaction import redact_diagnostic, redact_secrets

# Event-dict keys that carry free-form error text. Anything logged under one of these is redacted (BUG-11), so a
# driver's exception message — which may embed a password, a token, SQL literals, a path — never reaches the log as-is,
# whichever call site produced it.
_ERROR_TEXT_KEYS = frozenset({"error", "diagnostic", "detail", "reason", "last_error", "exception", "errors", "message"})


def redact_error_text(logger, method_name, event_dict):  # structlog processor signature
    for key in _ERROR_TEXT_KEYS & event_dict.keys():
        value = event_dict[key]
        if isinstance(value, str):
            event_dict[key] = redact_diagnostic(value)
    return event_dict


NOISY_LIBRARY_LOGGERS = ("kafka",)
# Libraries that log request URLs through the standard logging module (not structlog, so redact_error_text never sees them): httpx logs
# "HTTP Request: POST https://hooks.slack.com/services/..." at INFO for every alert it posts. Their lines are passed through redact_secrets.
URL_LOGGING_LIBRARIES = ("httpx", "httpcore")


class _RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact_secrets(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


def configure_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )
    # DEBUG is for this app's own logs. The Kafka client library logs every network request and response at DEBUG (each fetch and
    # produce, with its bytes): with the local dev override's DATACONNECT_LOG_LEVEL=DEBUG that was ~92,000 lines a minute while idle
    # (2026-10-28), most of the quality worker's time. Its warnings and errors still come through.
    for library in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(library).setLevel(max(log_level, logging.WARNING))
    for library in URL_LOGGING_LIBRARIES:
        lib_logger = logging.getLogger(library)
        if not any(isinstance(f, _RedactSecretsFilter) for f in lib_logger.filters):
            lib_logger.addFilter(_RedactSecretsFilter())

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_error_text,
    ]

    renderer = (
        structlog.dev.ConsoleRenderer()
        if settings.env == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
