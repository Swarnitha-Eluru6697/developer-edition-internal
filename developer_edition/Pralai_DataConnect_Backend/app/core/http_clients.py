"""
A shared, lifecycle-managed httpx.AsyncClient per outbound HTTP endpoint.

Every runtime HTTP integration (Kafka Connect REST, Kafka REST Proxy) used
to open a brand new `httpx.AsyncClient` — and therefore a brand new
connection (and TLS handshake, where applicable) — on every single call.
That defeats keep-alive, makes connection-pool limits meaningless, and
leaves nothing to tune centrally.

`get_client(base_url)` instead returns one client per distinct base_url,
created on first use and reused for every subsequent call to that endpoint,
with a shared connection-limit/keep-alive policy and connect-level retries
(safe for any HTTP method, since they only ever fire before a request
reaches the server — never after a response has already been received).
`close_all()` releases every pooled connection; call it once, from the
application's shutdown hook.
"""

import asyncio

import httpx

from app.core.config import settings

_clients: dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def _build_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=15.0,  # per-request timeout= overrides this where a call needs longer (e.g. deploy)
        limits=httpx.Limits(
            max_connections=settings.http_pool_max_connections,
            max_keepalive_connections=settings.http_pool_max_keepalive_connections,
            keepalive_expiry=settings.http_pool_keepalive_expiry_seconds,
        ),
        transport=httpx.AsyncHTTPTransport(retries=settings.http_pool_connect_retries),
    )


async def get_client(base_url: str) -> httpx.AsyncClient:
    """The shared client for `base_url` — created lazily, reused thereafter.
    Never close the object this returns yourself; it's owned by this module."""
    client = _clients.get(base_url)
    if client is not None and not client.is_closed:
        return client
    async with _lock:
        client = _clients.get(base_url)
        if client is None or client.is_closed:
            client = _build_client(base_url)
            _clients[base_url] = client
    return client


async def close_all() -> None:
    """Release every pooled connection. Call once from the app shutdown hook."""
    clients = list(_clients.values())
    _clients.clear()
    # All of them, even if one fails to close: a connection left open because its neighbour raised is a leak.
    results = await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        import structlog

        structlog.get_logger(__name__).warning("http_clients.close_failed", count=len(failures), first=type(failures[0]).__name__)
