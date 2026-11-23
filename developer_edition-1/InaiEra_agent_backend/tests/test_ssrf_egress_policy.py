"""
Real tests for the SSRF / unrestricted outbound networking fix
(section 0.56, GAP_REPORT).

The real bug, exactly as reported: handle_http (harness.py) and the Doc
Extractor's _fetch_and_extract_one made outbound requests with zero
validation of the target - no check for loopback, RFC1918, link-local
(the real cloud metadata range), or internal DNS names, and the Doc
Extractor followed redirects with follow_redirects=True with no
validation of where they actually led either.

The fix is a real, shared Egress Policy Engine (egress.py):
DNS-resolve the real target, validate every resolved IP against a real
denylist, and pin the connection to the validated IP (closing the
DNS-rebinding gap a check-then-connect approach would leave open) -
with every real redirect hop validated the same way, and a real
streaming response-size cap enforced during download.

A real, critical regression was found and fixed while building this:
the Doc Extractor is designed to fetch this platform's own uploaded
files via a self-referential http://127.0.0.1:.../api/uploads/{id}/download
URL, which a blanket loopback block would have broken outright. The fix
reads real local uploads directly from the database instead
(_try_read_local_upload), avoiding the network call - and therefore any
need for a loopback carve-out in the security policy - entirely.
"""
import sys
import os
import threading
import http.server
import socketserver
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.egress import safe_http_request, SSRFBlocked
from app.harness import handle_http, handle_docextractor, _try_read_local_upload


def make_ctx(**overrides) -> dict:
    ctx = {"input": "test", "vars": {}, "agent_id": "test", "agent_name": "test", "invoking_user": "user:test", "output": None}
    ctx.update(overrides)
    return ctx


class TestRealEgressPolicyBlocking:
    @pytest.mark.parametrize("url,desc", [
        ("http://127.0.0.1/", "loopback"),
        ("http://localhost/", "localhost"),
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata range"),
        ("http://10.0.0.5/", "RFC1918 10.x"),
        ("http://172.16.0.1/", "RFC1918 172.16.x"),
        ("http://192.168.1.1/", "RFC1918 192.168.x"),
        ("http://[::1]/", "IPv6 loopback"),
        ("http://internal-svc.internal/", "internal DNS suffix"),
        ("http://myservice.svc.cluster.local/", "real Kubernetes internal service DNS"),
    ])
    def test_every_reported_attack_vector_is_genuinely_blocked(self, url, desc):
        with pytest.raises(SSRFBlocked):
            safe_http_request("GET", url)

    def test_a_non_http_scheme_is_genuinely_rejected(self):
        with pytest.raises(SSRFBlocked):
            safe_http_request("GET", "ftp://example.com/")

    def test_a_hostname_with_one_safe_and_one_unsafe_resolved_address_is_still_blocked(self, monkeypatch):
        import app.egress as egress_module

        def fake_getaddrinfo(host, port, **kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", port)),
                (2, 1, 6, "", ("169.254.169.254", port)),
            ]
        monkeypatch.setattr(egress_module.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(SSRFBlocked):
            safe_http_request("GET", "http://mixed-dns-example.test/")


class TestRealRedirectValidation:
    def test_a_redirect_to_the_metadata_endpoint_is_genuinely_blocked(self):
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
            def log_message(self, *a): pass

        server = socketserver.TCPServer(("127.0.0.1", 0), RedirectHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            import app.egress as egress_module
            real_validate = egress_module._resolve_and_validate

            def allow_only_test_server(hostname, p):
                if hostname == "127.0.0.1":
                    return "127.0.0.1"
                return real_validate(hostname, p)

            with patch("app.egress._resolve_and_validate", side_effect=allow_only_test_server):
                with pytest.raises(SSRFBlocked, match="169.254.169.254"):
                    safe_http_request("GET", f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()

    def test_too_many_redirects_is_genuinely_rejected(self):
        class LoopHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", self.path + "x")
                self.end_headers()
            def log_message(self, *a): pass

        server = socketserver.TCPServer(("127.0.0.1", 0), LoopHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                with pytest.raises(SSRFBlocked, match="too many redirects"):
                    safe_http_request("GET", f"http://127.0.0.1:{port}/", max_redirects=3)
        finally:
            server.shutdown()


class TestRealStreamingSizeCap:
    def test_a_large_response_is_genuinely_stopped_during_download_not_after(self):
        class BigHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                try:
                    self.wfile.write(b"X" * (5 * 1024 * 1024))
                except Exception:
                    pass
            def log_message(self, *a): pass

        server = socketserver.TCPServer(("127.0.0.1", 0), BigHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                with pytest.raises(SSRFBlocked, match="exceeded"):
                    safe_http_request("GET", f"http://127.0.0.1:{port}/", max_response_bytes=1024)
        finally:
            server.shutdown()


class TestRealHttpNodeIntegration:
    def test_the_http_node_genuinely_blocks_an_internal_target(self):
        ctx = make_ctx()
        result = handle_http({"method": "GET", "url": "http://127.0.0.1:5432/"}, ctx)
        assert "blocked by egress policy" in result["vars"]["http_output"]["error"]

    def test_the_http_node_genuinely_blocks_the_metadata_endpoint(self):
        ctx = make_ctx()
        result = handle_http({"method": "GET", "url": "http://169.254.169.254/latest/meta-data/"}, ctx)
        assert "blocked by egress policy" in result["vars"]["http_output"]["error"]

    def test_verify_ssl_false_is_no_longer_a_real_config_option(self):
        """A real, direct guard that the removed bypass genuinely
        can't be reintroduced through cfg - checks for the actual
        code pattern that would read it back out of the node's
        config, not just any mention of the string (the function's
        own docstring legitimately explains the removal by name)."""
        import inspect
        src = inspect.getsource(handle_http)
        assert 'cfg.get("verify_ssl"' not in src
        assert "cfg.get('verify_ssl'" not in src
        assert 'cfg["verify_ssl"]' not in src


class TestRealDocExtractorLocalUploadRegression:
    def _seed_upload(self, session, file_id: str, content: bytes, filename: str = "test.txt", content_type: str = "text/plain"):
        from app.models import UploadedFile
        from app.storage import get_storage_backend
        object_key = f"test-owner/{file_id}/{filename}"
        get_storage_backend().put(object_key, content, content_type)
        row = UploadedFile(id=file_id, filename=filename, content_type=content_type, size=len(content), object_key=object_key, owner="test-owner")
        session.add(row)
        session.commit()

    @pytest.fixture()
    def session(self):
        from sqlmodel import Session
        from app.db import engine
        with Session(engine) as s:
            yield s

    def test_a_real_local_upload_url_is_read_directly_not_fetched_over_the_network(self, session):
        import uuid
        file_id = f"up-test-{uuid.uuid4().hex[:10]}"
        self._seed_upload(session, file_id, b"real uploaded content")
        url = f"http://127.0.0.1:8000/api/uploads/{file_id}/download"
        result = _try_read_local_upload(url)
        assert result is not None
        content, content_type, filename = result
        assert content == b"real uploaded content"

    def test_the_local_upload_path_works_regardless_of_configured_host(self, session):
        import uuid
        file_id = f"up-test-{uuid.uuid4().hex[:10]}"
        self._seed_upload(session, file_id, b"content via a different real host")
        url = f"https://my-real-deployment.example.com/api/uploads/{file_id}/download"
        result = _try_read_local_upload(url)
        assert result is not None
        assert result[0] == b"content via a different real host"

    def test_a_url_that_only_looks_like_an_upload_url_but_isnt_real_falls_through_safely(self, session):
        result = _try_read_local_upload("http://127.0.0.1:8000/api/uploads/up-does-not-exist/download")
        assert result is None

    def test_handle_docextractor_genuinely_still_works_end_to_end_for_a_real_local_upload(self, session):
        import uuid
        file_id = f"up-test-{uuid.uuid4().hex[:10]}"
        self._seed_upload(session, file_id, b"end to end real content")
        url = f"http://127.0.0.1:8000/api/uploads/{file_id}/download"
        ctx = make_ctx()
        result = handle_docextractor({"input_url": url}, ctx)
        assert result["vars"].get("docextractor_error") is None
        assert result["vars"]["docextractor_text"] == "end to end real content"

    def test_handle_docextractor_genuinely_blocks_a_real_internal_target_that_is_not_a_local_upload(self):
        ctx = make_ctx()
        result = handle_docextractor({"input_url": "http://169.254.169.254/latest/meta-data/"}, ctx)
        assert "blocked by egress policy" in result["vars"]["docextractor_error"]
