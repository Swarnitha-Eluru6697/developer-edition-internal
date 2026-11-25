"""How Workspace talks to the services behind it, and what it says when one of them is gone (X-007).

Before: every proxy used one 60 s timeout for connecting AND reading and let httpx's exceptions escape, so a runner that was down
or frozen held the request for up to a minute and then surfaced as a bare 500 with nothing about what failed. Now connecting must
succeed within `upstream_connect_timeout_seconds` (a dead service is noticed in seconds), reading is bounded by
`upstream_timeout_seconds`, and the transport failures become the documented envelope with the status a client can act on:

  502 UPSTREAM_UNAVAILABLE   the service could not be reached (connection refused/reset, DNS, TLS)
  504 UPSTREAM_TIMEOUT       it accepted the request and did not answer in time

An answer from the service (any status code, including its own 5xx) is passed through untouched.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import structlog

from app.core.config import get_settings
from app.core.errors import WorkspaceError

log = structlog.get_logger()


def upstream_timeout(slow: bool = False) -> httpx.Timeout:
    """Connect fast, read within a bound. `slow` is for calls that legitimately take longer (SQL validation against a cluster)."""
    s = get_settings()
    read = s.upstream_timeout_seconds * (2 if slow else 1)
    return httpx.Timeout(read, connect=s.upstream_connect_timeout_seconds)


@contextmanager
def upstream_guard(service: str) -> Iterator[None]:
    """Turn transport failures talking to `service` into 502 / 504 in the standard envelope."""
    try:
        yield
    except httpx.TimeoutException as e:
        timed_out_connecting = isinstance(e, httpx.ConnectTimeout)
        log.error("upstream_timeout", service=service, phase="connect" if timed_out_connecting else "read", error=type(e).__name__)
        if timed_out_connecting:
            raise WorkspaceError(502, "UPSTREAM_UNAVAILABLE", f"{service} could not be reached") from e
        raise WorkspaceError(504, "UPSTREAM_TIMEOUT", f"{service} did not answer in time") from e
    except httpx.TransportError as e:
        log.error("upstream_unavailable", service=service, error=type(e).__name__)
        raise WorkspaceError(502, "UPSTREAM_UNAVAILABLE", f"{service} could not be reached") from e
