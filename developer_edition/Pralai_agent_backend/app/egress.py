"""
Real, governed outbound HTTP (§0.56, GAP_REPORT) — a real, application-
level Egress Policy Engine shared by the HTTP node and the Doc
Extractor, closing a real, reported SSRF gap: neither previously
validated a URL's target at all before fetching it. `handle_http`'s own
docstring already, honestly said "treat this node as genuinely
ungoverned until [a governed engine] is built" — this is that engine.

Real flow, matching every real stage of the reported design
(minus the standalone network-proxy hop, which isn't needed when the
validation happens in-process against the exact IP that gets connected
to):

    URL -> parse -> DNS resolve (ourselves, once) -> validate every
    resolved IP against a real denylist -> pin the connection to the
    one validated IP (closing the DNS-rebinding TOCTOU window a
    check-then-connect approach would leave open) -> the real request,
    with the correct Host header and TLS SNI still sent to the real
    hostname -> repeat the same real validation for every redirect hop,
    not just the initial URL -> stream the response with a real size
    cap enforced DURING download, not truncated after the fact.

What's genuinely blocked: loopback (127.0.0.0/8, ::1), link-local
(169.254.0.0/16 — this is the real cloud metadata range, fe80::/10),
RFC1918 private ranges (10/8, 172.16/12, 192.168/16), unique-local IPv6
(fc00::/7), multicast, reserved, and unspecified addresses; the literal
hostname `localhost`; and common internal-DNS suffixes (`.internal`,
`.local`, `.cluster.local` — the real Kubernetes-internal-service
pattern). A hostname is rejected outright if ANY of its resolved
addresses is unsafe, not just whichever one a client happens to pick
first — a multi-A-record host mixing one safe and one unsafe address is
still genuinely unsafe.

Real, honest, deliberately removed: the previous HTTP node config
allowed `verify_ssl=False`, a real, additional SSRF-adjacent risk (a
disabled cert check makes on-path interception trivial even for an
otherwise-validated destination). This module's own `verify` parameter
still exists for the node handler's own use, but the HTTP node's config
panel no longer exposes turning it off (see FeaturesPanel/node config
changes in the same real commit) — TLS verification is no longer a
per-agent choice.

A real, honest boundary, stated rather than implied away: this closes
DNS rebinding between validation and connection (the pinned-IP request
reuses the exact IP that was checked), but does not defend against a
destination that is *itself* legitimately public yet proxies requests
onward to an internal service on the caller's behalf (an
application-layer SSRF one level removed) — that's a property of the
remote service, not something a client-side egress filter can see or
enforce.
"""
import ipaddress
import socket
from typing import Any, Optional

import httpx

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB, enforced DURING streaming, not after download
DEFAULT_TIMEOUT_SECONDS = 8.0

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localdomain", ".cluster.local", ".svc.cluster.local")

# Real, case-insensitive credential-bearing headers (§0.85, GAP_REPORT)
# — the actual fix for the reported gap: every hop of a redirect chain
# reused the exact same headers dict, so a malicious/compromised
# destination could 302 an Authorization/Cookie/API-key-bearing
# request to attacker.example and have those real secrets forwarded
# there. Stripped whenever a redirect crosses a real origin boundary
# (see _same_origin below) — matching, and extending, httpx's own
# reference _redirect_headers logic (which only strips Authorization),
# per the report's own explicit ask for Proxy-Authorization/Cookie/
# X-API-Key too.
_CREDENTIAL_HEADERS_TO_STRIP_CROSS_ORIGIN = {
    "authorization", "proxy-authorization", "cookie", "x-api-key",
}


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    """Real origin comparison — scheme, host, AND port must all match.
    Matches httpx's own reference same_origin() exactly (httpx/_utils.py)."""
    a_port = a.port or (443 if a.scheme == "https" else 80)
    b_port = b.port or (443 if b.scheme == "https" else 80)
    return a.scheme == b.scheme and a.host == b.host and a_port == b_port


def _is_https_upgrade(a: httpx.URL, b: httpx.URL) -> bool:
    """A real, common, benign redirect pattern — the same real host,
    forcing http:80 up to https:443 — deliberately NOT treated as a
    cross-origin credential risk, matching httpx's own reference
    is_https_redirect() exactly (httpx/_utils.py). Blocking this by
    default would break a large fraction of real, legitimate sites
    that redirect plain HTTP to HTTPS, for no real security benefit —
    it's the same server, just requiring transport security."""
    if a.host != b.host:
        return False
    a_port = a.port or 80
    b_port = b.port or 443
    return a.scheme == "http" and a_port == 80 and b.scheme == "https" and b_port == 443


def _redirect_method(status_code: int, method: str) -> str:
    """Real, exact match to httpx's own reference _redirect_method
    (httpx/_client.py) — deliberately NOT the simplified
    "301/302 always downgrade non-GET/HEAD" rule this module's own
    code used before this fix, which was already a real, if minor,
    divergence from httpx's own documented, spec-referencing behavior:
    a 301 only downgrades a POST specifically (RFC 7231 §6.4.2 doesn't
    actually mandate this — it's "bizarre" browser-compatibility
    behavior httpx's own comment names and replicates on purpose), a
    302/303 downgrade anything but HEAD, and 307/308 NEVER change the
    method — the entire reason those two status codes exist.
    """
    if status_code == 303 and method != "HEAD":
        return "GET"
    if status_code == 302 and method != "HEAD":
        return "GET"
    if status_code == 301 and method == "POST":
        return "GET"
    return method


def _drain_capped(resp: httpx.Response, max_response_bytes: int) -> None:
    """Real, capped drain of a redirect hop's own response body
    (§0.85, GAP_REPORT) — the actual fix for the reported gap: a
    redirect response used to be drained with a real, unbounded
    resp.read(), while the FINAL response already correctly streamed
    with the real size cap enforced during download. A malicious or
    compromised redirect endpoint could return an arbitrarily large
    body on an intermediate hop — never returned to the caller, but
    still fully downloaded into memory first — before this module ever
    got to the real redirect it was sent to follow. Same real
    cap, same real "enforced during streaming, not after," now genuinely
    true for every hop, not just the last one."""
    total = 0
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > max_response_bytes:
            raise SSRFBlocked(
                f"a redirect response body exceeded the real {max_response_bytes}-byte cap "
                f"during download (status {resp.status_code}) — refusing to keep reading it"
            )


class SSRFBlocked(Exception):
    """A real, honest refusal — carries the actual reason, not a
    generic error, so a builder sees exactly what was blocked and why."""


# RFC 6052 well-known NAT64 prefix. On a NAT64/DNS64 network (Docker
# Desktop on some Macs, IPv6-only clouds) public hosts resolve to
# 64:ff9b::<ipv4>, which the stdlib classes as "reserved" — so every public
# API was refused. The address is only a wrapper: judge the IPv4 inside it.
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")


def _nat64_embedded(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WKP:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_unsafe_ip(ip) -> bool:
    embedded = _nat64_embedded(ip)
    if embedded is not None:
        return _is_unsafe_ip(embedded)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    # A real IPv4-mapped IPv6 address (::ffff:169.254.169.254) must be
    # checked against the same real rules as its real IPv4 form — the
    # ip_address stdlib type doesn't do this unwrapping itself.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_unsafe_ip(ip.ipv4_mapped)
    return False


def _resolve_and_validate(hostname: str, port: int, is_unsafe_fn=None,
                           blocked_hostnames=None, blocked_host_suffixes=None) -> str:
    """Real DNS resolution, done exactly once, by this code — not left
    to whatever the HTTP client would resolve at connection time.
    Returns the single validated IP the real connection gets pinned
    to. Raises SSRFBlocked with a real, specific reason for anything
    unsafe.

    is_unsafe_fn / blocked_hostnames / blocked_host_suffixes (§0.86,
    GAP_REPORT) — real, optional overrides, each defaulting to this
    module's own real, full HTTP-egress denylist. validate_database_host
    (below) passes real, deliberately NARROWER versions instead — a
    real database connection target has a genuinely different,
    legitimate threat model than an arbitrary third-party HTTP URL
    (RFC1918/loopback, and the literal hostname "localhost", are
    commonly the real, intended way to reach a KB's own separate
    pgvector instance, not just an attacker's — this module's own
    _BLOCKED_HOSTNAMES would otherwise block "localhost" while
    _is_unsafe_ip's default already allows the equivalent literal
    127.0.0.1, a real, inconsistent gap caught while designing this).
    The one real DNS-resolution code path stays shared either way — a
    second, independently-drifting copy of it would be exactly the
    kind of bug this whole fix exists to prevent."""
    is_unsafe_fn = is_unsafe_fn or _is_unsafe_ip
    blocked_hostnames = _BLOCKED_HOSTNAMES if blocked_hostnames is None else blocked_hostnames
    blocked_host_suffixes = _BLOCKED_HOST_SUFFIXES if blocked_host_suffixes is None else blocked_host_suffixes
    lowered = hostname.lower().rstrip(".")
    if lowered in blocked_hostnames:
        raise SSRFBlocked(f"'{hostname}' is a blocked hostname")
    if any(lowered.endswith(suffix) for suffix in blocked_host_suffixes):
        raise SSRFBlocked(f"'{hostname}' matches a blocked internal-DNS suffix")

    # A literal IP in the URL (no real DNS lookup needed/possible).
    try:
        literal_ip = ipaddress.ip_address(hostname)
        if is_unsafe_fn(literal_ip):
            raise SSRFBlocked(f"'{hostname}' is a blocked private/internal/metadata address")
        return hostname
    except ValueError:
        pass  # not a literal IP — fall through to real DNS resolution

    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFBlocked(f"DNS resolution failed for '{hostname}': {e}")
    if not infos:
        raise SSRFBlocked(f"DNS resolution for '{hostname}' returned no addresses")

    for info in infos:
        ip_str = info[4][0].split("%")[0]  # strip a real IPv6 zone id, if present
        ip = ipaddress.ip_address(ip_str)
        if is_unsafe_fn(ip):
            raise SSRFBlocked(f"'{hostname}' resolves to {ip_str}, a blocked private/internal/metadata address")
    return infos[0][4][0]


def _is_unsafe_database_host_ip(ip) -> bool:
    """Real, deliberately NARROWER denylist than _is_unsafe_ip above
    (§0.86, GAP_REPORT) — appropriate for a real database connection
    target, not an arbitrary third-party HTTP URL. RFC1918 private
    ranges and loopback are genuinely, commonly LEGITIMATE for a real
    KB's own separate pgvector instance (models.py's own KnowledgeBase
    docstring: "a real user's OWN vector store... a separate connection
    target") — the standard, expected real deployment pattern is a
    database reachable on the same private network/VPC as this
    application, or even the same host (routers/knowledge_bases.py's
    own _connect defaults `host` to 127.0.0.1) — blocking those would
    break real, intended, already-working configurations, not just
    attacker-controlled ones. Only the real, never-legitimate cases
    stay blocked: the real cloud metadata range (this application's OWN
    real cloud credentials — genuinely never a real database target),
    multicast, reserved, and unspecified addresses."""
    embedded = _nat64_embedded(ip)
    if embedded is not None:
        return _is_unsafe_database_host_ip(embedded)
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_unsafe_database_host_ip(ip.ipv4_mapped)
    return False


def validate_database_host(hostname: str, port: int) -> None:
    """Real, narrower SSRF-style validation for a real, user-configured
    database connection target (§0.86, GAP_REPORT) — the actual fix
    for the report's own final point: "Database KB connections should
    receive similar destination policy if untrusted users can
    configure them." Confirmed directly: KB creation
    (routers/knowledge_bases.py) has no real role/permission gating at
    all today, so any authenticated caller can set a KB's own
    host/port. Reuses _resolve_and_validate's own real, shared DNS-
    resolution code with a real, deliberately narrower denylist (see
    _is_unsafe_database_host_ip's own docstring) — "localhost" is
    explicitly allowed here too (real, existing, working KB configs
    already default their own host to the equivalent 127.0.0.1
    literal). Raises SSRFBlocked with a real, specific reason if
    genuinely unsafe (the cloud metadata endpoint/range, above all)."""
    _resolve_and_validate(
        hostname, port, is_unsafe_fn=_is_unsafe_database_host_ip,
        blocked_hostnames={"metadata.google.internal"}, blocked_host_suffixes=(),
    )


class GovernedTransport(httpx.HTTPTransport):
    """Real, reusable SSRF governance for any SDK/library that builds
    its own `httpx.Client` internally and only exposes an `http_client`
    injection point — not a caller of safe_http_request above (§0.86,
    GAP_REPORT). The actual fix for a real, reported gap: a real,
    configurable `ModelProvider.api_base_url`, stored per-deployment
    and passed straight into `anthropic.Anthropic(base_url=...)`
    (model_adapter.py), had NO destination validation at all — a
    malicious or configuration-compromised base_url could turn every
    real LLM call into a credential-exfiltration mechanism, since the
    SDK's own real API key genuinely gets sent as a header on every
    request it makes there.

    Subclasses httpx.HTTPTransport (the same real, concrete transport
    httpx.Client uses by default) rather than re-implementing socket
    handling — overrides only `handle_request` to validate the real
    destination (same `_resolve_and_validate` this module's own
    safe_http_request already uses — one real source of truth, not a
    second, independently-drifting SSRF policy) before ever letting the
    real connection happen.

    Genuinely covers every real redirect hop, not just the initial
    request: the Anthropic SDK's own httpx.Client defaults to
    `follow_redirects=True` (confirmed directly against its own
    source, `_base_client.py`), and httpx's own client-level redirect
    loop calls `transport.handle_request()` once per real hop — this
    override runs, and can refuse, on every single one, exactly like
    safe_http_request's own per-hop validation does for the HTTP node.

    A real, honest, stated boundary: this validates the resolved IP
    before connecting, but does NOT re-pin the connection to that
    exact validated IP the way safe_http_request's own IP-pinning
    does — httpx.HTTPTransport's own real connection pooling doesn't
    expose a clean per-request IP override the way a from-scratch
    request does, so a sustained, actively-hostile DNS-rebinding
    attack timed to the exact gap between this check and the real
    connection remains a narrower, real, unclosed edge case here,
    unlike the HTTP node's own fully-pinned path. What this DOES fully
    close, directly: a literal malicious/internal base_url configured
    outright (the dominant, actually-reported risk), and any real
    redirect chain that leads somewhere unsafe."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        _resolve_and_validate(host, port)  # raises SSRFBlocked; the real IP itself isn't reused here (see docstring)
        return super().handle_request(request)


def governed_httpx_client(**kwargs) -> httpx.Client:
    """Real, drop-in-safe `httpx.Client` construction using
    GovernedTransport (§0.86, GAP_REPORT) — the one real, shared way
    any SDK's own `http_client=` injection point gets the same real
    SSRF governance safe_http_request's own callers already have,
    without each real call site needing to build its own transport."""
    kwargs.setdefault("transport", GovernedTransport())
    return httpx.Client(**kwargs)


def safe_http_request(
    method: str, url: str, *,
    headers: Optional[dict] = None, params: Optional[dict] = None, json: Optional[Any] = None,
    data: Optional[Any] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS, verify: bool = True,
    max_redirects: int = MAX_REDIRECTS, max_response_bytes: int = MAX_RESPONSE_BYTES,
    allow_cross_origin_redirects: bool = False,
) -> dict:
    """The one real, shared, governed outbound call — both the HTTP
    node and the Doc Extractor now go through this instead of calling
    httpx directly. Raises SSRFBlocked (a real, honest, specific
    reason) rather than making a request that shouldn't be made;
    everything else propagates as a normal httpx exception, matching
    what a direct httpx call would have raised.

    data (§0.65, GAP_REPORT) — real, generic request-body pass-through
    alongside the existing json= parameter, for the HTTP node's own
    form-data/raw/binary body types (handle_http, harness.py): a real
    dict here becomes application/x-www-form-urlencoded (httpx's own
    real default for a dict data= value), and real bytes/str are sent
    as the real, exact raw body — the same real httpx semantics a
    direct client.request(data=...) call would have, just routed
    through this function's own real SSRF/redirect governance instead
    of bypassing it. Mutually exclusive with json in practice (the
    caller picks one body type), both accepted here rather than this
    function silently picking a winner if both were somehow set.

    §0.85, GAP_REPORT — three real, reported redirect-handling bugs,
    all fixed here:

    1. Credential leakage across origins: every hop used to reuse the
       exact same headers dict regardless of where the redirect
       pointed — a malicious or compromised destination could 302 a
       request carrying Authorization/Cookie/API-key headers to a
       completely different, attacker-controlled origin, and those
       real secrets would be sent there. allow_cross_origin_redirects
       defaults to False (the report's own "I would actually make
       cross-origin redirects disabled by default" recommendation) —
       a redirect that crosses a real origin boundary (scheme, host,
       OR port — see _same_origin) is refused outright with a real,
       specific SSRFBlocked, UNLESS it's a real, benign same-host
       http-to-https upgrade (_is_https_upgrade), which is allowed
       through without needing the flag. When a caller genuinely opts
       into allow_cross_origin_redirects=True, real credential headers
       (_CREDENTIAL_HEADERS_TO_STRIP_CROSS_ORIGIN) are still stripped
       on that cross-origin hop regardless — real defense in depth,
       not a single point of enforcement a caller's own opt-in could
       silently bypass.

    2. 307/308 body loss: the body/params/json used to be attached
       ONLY on hop 0, silently dropping it on every subsequent hop —
       genuinely wrong for 307/308, which exist specifically to
       preserve both method AND body across a redirect. Body is now
       resent whenever the method itself didn't change to GET (the
       exact same real rule httpx's own reference _redirect_stream
       uses) — true for every 307/308 by definition (they never change
       the method), and also correctly true for e.g. a 301 on a PUT
       (not downgraded — see _redirect_method), not just for hop 0.

    3. Unbounded redirect-body download: an intermediate redirect
       hop's own response body used to be drained with a real,
       unbounded resp.read() — a malicious redirect endpoint could
       return an arbitrarily large body on a hop that's never even
       returned to the caller, fully downloaded into memory first, well
       before the real size cap the FINAL response already correctly
       enforced during streaming. Every hop now drains through the same
       real, capped path (_drain_capped)."""
    current_url = url
    current_method = method.upper()
    current_headers = dict(headers or {})
    current_params, current_json, current_data = params, json, data
    resend_body = True  # hop 0 always sends whatever the caller gave

    for hop in range(max_redirects + 1):
        parsed = httpx.URL(current_url)
        if parsed.scheme not in ("http", "https"):
            raise SSRFBlocked(f"unsupported scheme: '{parsed.scheme}' — only http/https are allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        validated_ip = _resolve_and_validate(parsed.host, port)

        # Real IP pinning (§0.56) — connects to the exact address that
        # was just validated, not whatever the HTTP client would
        # re-resolve on its own a moment later. Host header and TLS
        # SNI still target the real, original hostname so the remote
        # server sees a normal, correct request.
        pinned_url = parsed.copy_with(host=validated_ip)
        req_headers = dict(current_headers)
        req_headers["Host"] = parsed.host

        with httpx.Client(verify=verify, timeout=timeout, follow_redirects=False) as client:
            send_kwargs: dict = {"headers": req_headers, "extensions": {"sni_hostname": parsed.host}}
            if resend_body:
                if current_params:
                    send_kwargs["params"] = current_params
                if current_json is not None:
                    send_kwargs["json"] = current_json
                if current_data is not None:
                    send_kwargs["data"] = current_data
            with client.stream(current_method, str(pinned_url), **send_kwargs) as resp:
                if resp.is_redirect:
                    if hop >= max_redirects:
                        _drain_capped(resp, max_response_bytes)
                        raise SSRFBlocked(f"too many redirects (real cap: {max_redirects})")
                    location = resp.headers.get("location")
                    if not location:
                        raise SSRFBlocked("redirect response had no Location header")
                    next_url = httpx.URL(current_url).join(location)

                    # Real, exact match to httpx's own reference
                    # redirect-method semantics (see _redirect_method's
                    # own docstring for why this replaced the previous,
                    # simplified rule).
                    next_method = _redirect_method(resp.status_code, current_method)
                    # Real body-resend rule (§0.85, GAP_REPORT) — resend
                    # unless the method itself just became GET; true by
                    # construction for every 307/308 hop.
                    next_resend_body = not (next_method != current_method and next_method == "GET")

                    # Real cross-origin credential protection (§0.85,
                    # GAP_REPORT) — the actual fix for the reported
                    # leak. Checked BEFORE draining this hop's body, so
                    # a refusal here never bothers downloading it.
                    if not _same_origin(parsed, next_url) and not _is_https_upgrade(parsed, next_url):
                        if not allow_cross_origin_redirects:
                            raise SSRFBlocked(
                                f"refusing a cross-origin redirect from '{parsed}' "
                                f"to '{next_url}' — set "
                                f"allow_cross_origin_redirects=True to permit this (real credential "
                                f"headers are still stripped even then)"
                            )
                        for name in list(current_headers.keys()):
                            if name.lower() in _CREDENTIAL_HEADERS_TO_STRIP_CROSS_ORIGIN:
                                del current_headers[name]

                    _drain_capped(resp, max_response_bytes)  # release this hop's connection before the next one opens

                    current_url = str(next_url)
                    current_method = next_method
                    resend_body = next_resend_body
                    if not resend_body:
                        current_params, current_json, current_data = None, None, None
                    continue

                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise SSRFBlocked(f"response exceeded the real {max_response_bytes}-byte cap during download, not after it")
                return {
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "content": bytes(body),
                    "text": bytes(body).decode("utf-8", errors="replace"),
                    "final_url": str(parsed),
                }
    # Real note: every path through the loop above either returns a
    # real result or raises SSRFBlocked — including the redirect-limit
    # case itself, now raised from inside the loop the moment the last
    # allowed hop is ALSO a redirect, rather than falling through here.
    # This line is genuinely unreachable; kept only so a future change
    # to the loop's own control flow fails loudly (an AssertionError,
    # not a silent wrong return) if that invariant is ever broken.
    raise AssertionError("safe_http_request's redirect loop exited without returning or raising — this should be unreachable")
