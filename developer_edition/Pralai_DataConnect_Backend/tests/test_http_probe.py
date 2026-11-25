import httpx
import pytest

from app.core.config import settings
from app.core.http_probe import TooManyRedirectsError, safe_get
from app.core.network_policy import DestinationBlockedError

# A literal public IP — no DNS resolution needed (works offline) and public,
# so it's allowed by policy without touching settings.
PUBLIC_IP = "8.8.8.8"


@pytest.mark.asyncio
async def test_safe_get_returns_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    resp = await safe_get(f"http://{PUBLIC_IP}/health", transport=httpx.MockTransport(handler))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.truncated is False


@pytest.mark.asyncio
async def test_safe_get_follows_redirect_and_revalidates_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": f"http://{PUBLIC_IP}/final"})
        return httpx.Response(200, text="ok")

    resp = await safe_get(f"http://{PUBLIC_IP}/start", transport=httpx.MockTransport(handler))

    assert resp.status_code == 200
    assert resp.content == b"ok"


@pytest.mark.asyncio
async def test_safe_get_blocks_redirect_to_link_local_target() -> None:
    """The core SSRF-via-redirect defense: passing initial validation is not
    enough — a redirect to the cloud metadata address must be caught too."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("must never reach the redirect target")

    with pytest.raises(DestinationBlockedError):
        await safe_get(f"http://{PUBLIC_IP}/start", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_safe_get_enforces_redirect_limit() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(302, headers={"location": f"http://{PUBLIC_IP}/hop-{calls['count']}"})

    with pytest.raises(TooManyRedirectsError):
        await safe_get(f"http://{PUBLIC_IP}/start", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_safe_get_truncates_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "network_probe_max_response_bytes", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1000)

    resp = await safe_get(f"http://{PUBLIC_IP}/big", transport=httpx.MockTransport(handler))

    # MockTransport hands the whole body back as a single chunk (there's no
    # real socket to fragment it the way a live server's reads would), so
    # `content` isn't meaningfully smaller here — what's under test is that
    # exceeding the cap is detected and flagged at all.
    assert resp.truncated is True


@pytest.mark.asyncio
async def test_safe_get_stops_consuming_once_over_the_cap(monkeypatch) -> None:
    """With a response delivered as multiple real chunks, the byte cap must
    stop consumption partway through rather than buffering everything."""
    monkeypatch.setattr(settings, "network_probe_max_response_bytes", 12)

    async def _chunks():
        for _ in range(200):
            yield b"x" * 5

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000"})

    # Bypass MockTransport for this one case and drive _drain directly
    # against a real async chunk generator, since MockTransport can't
    # produce genuinely incremental chunks.
    from app.core.http_probe import _drain

    class _FakeResponse:
        status_code = 200
        url = f"http://{PUBLIC_IP}/big"
        headers: dict[str, str] = {}

        def aiter_bytes(self):
            return _chunks()

    result = await _drain(_FakeResponse(), max_bytes=settings.network_probe_max_response_bytes)

    assert result.truncated is True
    assert len(result.content) < 1000
    assert len(result.content) % 5 == 0  # stopped on a whole-chunk boundary


@pytest.mark.asyncio
async def test_safe_get_rejects_non_http_scheme() -> None:
    with pytest.raises(DestinationBlockedError):
        await safe_get("file:///etc/passwd")


@pytest.mark.asyncio
async def test_safe_get_blocks_initial_target_in_blocked_range() -> None:
    with pytest.raises(DestinationBlockedError):
        await safe_get("http://127.0.0.1/", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
