from __future__ import annotations

import httpx

from app.core.errors import DQServiceError

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class UpstreamError(Exception):
    """A dependency answered unexpectedly or could not be reached."""

    def __init__(self, service: str, message: str, *, status: int | None = None,
                 body: object = None):
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message
        self.status = status
        self.body = body

    def to_http(self) -> DQServiceError:
        return DQServiceError(502, "UPSTREAM_UNAVAILABLE", f"{self.service}: {self.message}",
                              {"upstream_status": self.status})


class UpstreamNotFound(UpstreamError):
    pass


class UpstreamForbidden(UpstreamError):
    pass


def safe_json(r: httpx.Response) -> object:
    try:
        return r.json() if r.content else None
    except ValueError:
        return None


def raise_for(service: str, r: httpx.Response, what: str) -> None:
    if 200 <= r.status_code < 300:
        return
    body = safe_json(r)
    if r.status_code == 404:
        raise UpstreamNotFound(service, f"{what} not found", status=404, body=body)
    if r.status_code in (401, 403):
        raise UpstreamForbidden(service, f"{what}: access denied (HTTP {r.status_code})",
                                status=r.status_code, body=body)
    raise UpstreamError(service, f"{what}: HTTP {r.status_code}: {r.text[:300]}",
                        status=r.status_code, body=body)
