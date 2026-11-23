"""
A safe HTTP GET for URLs a tenant declares (API connection health checks,
Elasticsearch's `_cat/indices`) — as opposed to app/core/http_clients.py,
which pools connections to *fixed, operator-configured* infrastructure
(Kafka Connect, the REST Proxy) and has no reason to apply any of this.

Every request here:
  - validates the destination host against app.core.network_policy before
    connecting — including, critically, before following any redirect: an
    initial URL passing validation is not enough on its own, so every hop
    of a manual, bounded redirect loop is re-validated the same way;
  - streams the response body and aborts once it exceeds a fixed byte cap,
    so a slow or oversized response can't exhaust memory;
  - applies a fixed connect/read timeout split, regardless of what a caller
    asks for.

Deliberately NOT pinned to the validated IP for the actual request: doing
that naively (rewriting the URL to the resolved address) breaks TLS
certificate verification for every HTTPS target, since the certificate is
issued for the hostname, not the IP — unacceptable given most real API/ES
endpoints are HTTPS. That means, as documented in network_policy.py, a
malicious DNS server could still in principle answer differently between
this check and httpx's own resolution moments later (DNS rebinding); this
still closes the overwhelmingly common case (a URL that resolves — now, or
after a redirect — to a blocked range) without breaking HTTPS.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings
from app.core.network_policy import DestinationBlockedError, validate_destination


@dataclass(frozen=True)
class SafeHttpResponse:
    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    truncated: bool

    def json(self) -> Any:
        import json

        return json.loads(self.content)


class TooManyRedirectsError(RuntimeError):
    pass


async def safe_get(
    url: str,
    *,
    org_id: str | None = None,
    auth: tuple[str, str] | None = None,
    headers: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SafeHttpResponse:
    """`transport` is a test hook only (e.g. httpx.MockTransport) — leave it
    unset in production, where real networking is what's wanted."""
    max_bytes = settings.network_probe_max_response_bytes
    max_redirects = settings.network_probe_max_redirects
    timeout = httpx.Timeout(
        connect=settings.network_probe_connect_timeout_seconds,
        read=settings.network_probe_read_timeout_seconds,
        write=settings.network_probe_read_timeout_seconds,
        pool=settings.network_probe_connect_timeout_seconds,
    )

    current_url = url
    for hop in range(max_redirects + 1):
        parsed = urlparse(current_url)
        if parsed.scheme not in {"http", "https"}:
            raise DestinationBlockedError(f"URL scheme '{parsed.scheme}' is not permitted")
        if not parsed.hostname:
            raise DestinationBlockedError(f"URL '{current_url}' has no host")

        # Re-validated on every hop — a redirect to a blocked destination
        # must be caught exactly like an initial URL pointed at one.
        await validate_destination(parsed.hostname, org_id=org_id, purpose="http_probe")

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
            async with client.stream("GET", current_url, headers=headers, auth=auth) as response:
                if response.is_redirect:
                    if hop >= max_redirects:
                        raise TooManyRedirectsError(f"Exceeded {max_redirects} redirects fetching '{url}'")
                    location = response.headers.get("location")
                    if not location:
                        return await _drain(response)
                    current_url = urljoin(current_url, location)
                    continue

                return await _drain(response, max_bytes=max_bytes)

    raise TooManyRedirectsError(f"Exceeded {max_redirects} redirects fetching '{url}'")


async def _drain(response: httpx.Response, *, max_bytes: int | None = None) -> SafeHttpResponse:
    body = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if max_bytes is not None and len(body) > max_bytes:
            truncated = True
            break
    return SafeHttpResponse(
        status_code=response.status_code,
        url=str(response.url),
        headers=dict(response.headers),
        content=bytes(body),
        truncated=truncated,
    )
