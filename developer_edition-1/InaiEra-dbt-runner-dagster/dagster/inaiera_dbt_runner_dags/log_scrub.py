"""One place that keeps repository credentials out of Dagster's logs (DG-011). (Same module as the dbt runner's `app/core/log_scrub.py`.)

The dbt Runner refuses a repository URL with user info (`https://user:token@host/repo.git`) at registration and redacts it in every
project it returns (DBT-009), and Dagster no longer runs git at all (DG-009) — but a project record from an older runner, an exception
message or a client library could still carry one into a log line. Rather than trust every call site, every log event of the code
server and the location manager passes through here: structlog events (all string values, nested) and standard-library records
(message, arguments and tracebacks).

`scrub_urls("clone of https://ci:ghp_x@github.com/a/b.git failed")` -> `clone of https://***@github.com/a/b.git failed`.
"""
from __future__ import annotations

import logging
import re
import traceback
from typing import Any

# scheme://userinfo@  — userinfo is everything up to the first '@' with no '/', whitespace or quote in it
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@'\"<>]+@")


def scrub_urls(text: str) -> str:
    """`text` with the user info of every URL in it replaced by `***`."""
    if "@" not in text or "://" not in text:
        return text
    return _USERINFO.sub(r"\g<scheme>***@", text)


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_urls(value)
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_walk(v) for v in value)
    return value


def scrub_event(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor: scrub every string in the event (put it BEFORE the renderer)."""
    return _walk(event_dict)


def add_to_structlog() -> None:
    """Insert `scrub_event` (with exception formatting before it, so tracebacks are scrubbed too) ahead of the renderer of the CURRENT
    structlog configuration. Idempotent. Call after `structlog.configure`, or on the default configuration."""
    import structlog

    processors = list(structlog.get_config()["processors"])
    if scrub_event in processors:
        return
    renderer = processors.pop()                       # the renderer is last by structlog's contract
    if not any(getattr(p, "__name__", "") == "format_exc_info" or type(p).__name__ == "ExceptionRenderer" for p in processors):
        processors.append(structlog.processors.format_exc_info)
    processors += [scrub_event, renderer]
    structlog.configure(processors=processors)


_installed = False


def install_stdlib() -> None:
    """Scrub every standard-library log record (any logger, any handler): message, arguments and tracebacks. Idempotent."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = scrub_urls(record.msg)
        if record.args:
            record.args = _walk(record.args) if isinstance(record.args, (tuple, dict)) else record.args
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            record.exc_text = scrub_urls("".join(traceback.format_exception(*record.exc_info)).rstrip("\n"))
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def install() -> None:
    """Both: structlog and the standard library."""
    add_to_structlog()
    install_stdlib()
