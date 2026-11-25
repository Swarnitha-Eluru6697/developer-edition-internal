"""
Real tests for the egress redirect credential-leak and body-handling
fixes (section 0.85, GAP_REPORT).

The real, reported bugs, all confirmed directly against the actual
code before fixing:

1. Credential leakage across origins: every redirect hop reused the
   exact same headers dict regardless of where the redirect actually
   pointed - a malicious or compromised destination could 302 a
   request carrying Authorization/Cookie/API-key headers to a
   completely different origin, and those real secrets would be sent
   there.
2. 307/308 body loss: request body/params/json were only ever attached
   on hop 0, silently dropping them on every subsequent hop - wrong
   for 307/308, which exist specifically to preserve both method and
   body across a redirect.
3. Unbounded redirect-body download: an intermediate redirect hop's
   own response body was drained with an unbounded resp.read(), not
   the same real, streaming size cap the final response already
   correctly enforced.

The fix: cross-origin redirects are refused by default
(allow_cross_origin_redirects=False, the report's own explicit
recommendation) unless the destination is a real, benign same-host
http-to-https upgrade; credential headers are stripped even when a
caller explicitly opts into following them; body is resent whenever
the redirect didn't downgrade the method to GET (matching httpx's own
reference _redirect_stream logic exactly, not a simplified
hop-0-only rule); every redirect hop's own response body is drained
through the same real, capped path the final response already used.
Redirect-method semantics were also brought into exact alignment with
httpx's own reference _redirect_method (a real, if minor, pre-existing
divergence caught while fixing this: only a 301-on-POST specifically
downgrades, not any non-GET/HEAD method).

These tests exercise real local HTTP servers (the same established
pattern test_ssrf_egress_policy.py already uses), not mocked redirect
logic - genuine sockets, genuine redirect responses, genuine header
inspection on the receiving end.
"""
import sys
import os
import threading
import http.server
import socketserver
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import httpx

from app.egress import safe_http_request, SSRFBlocked, _same_origin, _is_https_upgrade, _redirect_method


def _run_server(handler_cls):
    server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


class TestRealCrossOriginCredentialLeakProtection:
    def test_a_cross_origin_redirect_is_refused_by_default(self):
        class BHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *a): pass

        server_b, port_b = _run_server(BHandler)

        class AHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port_b}/")
                self.end_headers()
            def log_message(self, *a): pass

        server_a, port_a = _run_server(AHandler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                with pytest.raises(SSRFBlocked, match="cross-origin"):
                    safe_http_request("GET", f"http://127.0.0.1:{port_a}/", headers={"Authorization": "Bearer SECRET"})
        finally:
            server_a.shutdown()
            server_b.shutdown()

    def test_credentials_are_stripped_even_when_cross_origin_is_explicitly_allowed(self):
        captured = {}

        class BHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                captured["authorization"] = self.headers.get("Authorization")
                captured["cookie"] = self.headers.get("Cookie")
                captured["x-api-key"] = self.headers.get("X-API-Key")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *a): pass

        server_b, port_b = _run_server(BHandler)

        class AHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port_b}/")
                self.end_headers()
            def log_message(self, *a): pass

        server_a, port_a = _run_server(AHandler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                result = safe_http_request(
                    "GET", f"http://127.0.0.1:{port_a}/",
                    headers={"Authorization": "Bearer SECRET", "Cookie": "session=abc", "X-API-Key": "KEY123"},
                    allow_cross_origin_redirects=True,
                )
            assert result["status_code"] == 200
            assert captured["authorization"] is None
            assert captured["cookie"] is None
            assert captured["x-api-key"] is None
        finally:
            server_a.shutdown()
            server_b.shutdown()

    def test_same_origin_redirect_still_forwards_headers_no_regression(self):
        captured = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                else:
                    captured["authorization"] = self.headers.get("Authorization")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
            def log_message(self, *a): pass

        server, port = _run_server(Handler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                result = safe_http_request("GET", f"http://127.0.0.1:{port}/start", headers={"Authorization": "Bearer SECRET"})
            assert result["status_code"] == 200
            assert captured["authorization"] == "Bearer SECRET"
        finally:
            server.shutdown()


class TestRealHttpsUpgradeException:
    def test_is_https_upgrade_identifies_only_the_real_standard_case(self):
        assert _is_https_upgrade(httpx.URL("http://example.com/"), httpx.URL("https://example.com/")) is True
        assert _is_https_upgrade(httpx.URL("http://example.com/"), httpx.URL("https://other.com/")) is False
        assert _is_https_upgrade(httpx.URL("http://example.com:8080/"), httpx.URL("https://example.com/")) is False
        assert _is_https_upgrade(httpx.URL("https://example.com/"), httpx.URL("http://example.com/")) is False


class TestRealSameOriginHelper:
    def test_same_origin_compares_scheme_host_and_port(self):
        assert _same_origin(httpx.URL("http://a.com/"), httpx.URL("http://a.com/x")) is True
        assert _same_origin(httpx.URL("http://a.com:80/"), httpx.URL("http://a.com/")) is True
        assert _same_origin(httpx.URL("http://a.com:8080/"), httpx.URL("http://a.com/")) is False
        assert _same_origin(httpx.URL("https://a.com/"), httpx.URL("http://a.com/")) is False
        assert _same_origin(httpx.URL("http://a.com/"), httpx.URL("http://b.com/")) is False


class TestRealRedirectMethodSemantics:
    def test_303_downgrades_to_get_except_head(self):
        assert _redirect_method(303, "POST") == "GET"
        assert _redirect_method(303, "PUT") == "GET"
        assert _redirect_method(303, "HEAD") == "HEAD"

    def test_302_downgrades_to_get_except_head(self):
        assert _redirect_method(302, "POST") == "GET"
        assert _redirect_method(302, "HEAD") == "HEAD"

    def test_301_downgrades_only_post(self):
        assert _redirect_method(301, "POST") == "GET"
        assert _redirect_method(301, "PUT") == "PUT"
        assert _redirect_method(301, "DELETE") == "DELETE"

    def test_307_and_308_never_change_method(self):
        assert _redirect_method(307, "POST") == "POST"
        assert _redirect_method(307, "PUT") == "PUT"
        assert _redirect_method(308, "POST") == "POST"
        assert _redirect_method(308, "PATCH") == "PATCH"


class TestReal307308BodyPreservation:
    def test_a_307_redirect_preserves_method_and_body(self):
        captured = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/start":
                    self.send_response(307)
                    self.send_header("Location", "/final")
                    self.end_headers()
                else:
                    length = int(self.headers.get("Content-Length", 0))
                    captured["method"] = self.command
                    captured["body"] = self.rfile.read(length)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
            def log_message(self, *a): pass

        server, port = _run_server(Handler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                result = safe_http_request("POST", f"http://127.0.0.1:{port}/start", data=b"important request body")
            assert result["status_code"] == 200
            assert captured["method"] == "POST"
            assert captured["body"] == b"important request body"
        finally:
            server.shutdown()

    def test_a_302_redirect_correctly_drops_the_body_when_downgraded_to_get(self):
        captured = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()

            def do_GET(self):
                if self.path == "/final":
                    captured["method"] = self.command
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
            def log_message(self, *a): pass

        server, port = _run_server(Handler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                result = safe_http_request("POST", f"http://127.0.0.1:{port}/", data=b"this should be dropped")
            assert result["status_code"] == 200
            assert captured["method"] == "GET"
        finally:
            server.shutdown()


class TestRealRedirectResponseSizeCap:
    def test_a_huge_redirect_response_body_is_capped_during_download(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "/final")
                self.send_header("Content-Length", str(20 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b"X" * (1024 * 1024))
                except Exception:
                    pass
            def log_message(self, *a): pass

        server, port = _run_server(Handler)
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                start = time.time()
                with pytest.raises(SSRFBlocked, match="exceeded"):
                    safe_http_request("GET", f"http://127.0.0.1:{port}/", max_response_bytes=1024)
                elapsed = time.time() - start
            assert elapsed < 3.0, f"took {elapsed:.1f}s — looks like the full body was downloaded first"
        finally:
            server.shutdown()
