"""
Real tests for extending egress governance to every configurable
outbound destination (section 0.86, GAP_REPORT).

The real, reported bug: the HTTP node and Doc Extractor were already
routed through egress.py's own SSRF/redirect governance, but several
other real, configurable outbound destinations still bypassed it
entirely:

1. The custom moderation endpoint (routers/execution.py) - a direct,
   ungoverned httpx.post to a real, per-agent-configurable URL.
2. Non-pgvector KB reachability checks (routers/knowledge_bases.py) -
   a direct, ungoverned httpx.get carrying the KB's own real,
   DECRYPTED API key as an Authorization header.
3. ModelProvider.api_base_url, passed straight into
   anthropic.Anthropic(base_url=...) - the SDK builds its own internal
   httpx.Client, with no destination validation at all.
4. pgvector KB database connections - a real, configurable host/port
   for a direct psycopg2.connect, with the real, decrypted database
   password sent to whatever host is configured.

The fix: (1) and (2) now route through the same real safe_http_request
every other governed outbound call uses. (3) uses a new, reusable
GovernedTransport injected into the SDK's own http_client= parameter.
(4) uses a new, deliberately NARROWER validate_database_host - RFC1918/
loopback are genuinely, commonly legitimate targets for a real KB's
own separate database, so only the real, never-legitimate
cloud-metadata case is actually blocked there.
"""
import sys
import os
import threading
import http.server
import socketserver
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.egress import (
    GovernedTransport, governed_httpx_client, SSRFBlocked,
    validate_database_host, _is_unsafe_database_host_ip,
)

client = TestClient(app)


def cleanup_agent(agent_id):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


def cleanup_kb(kb_id):
    client.delete(f"/api/integrations/knowledge-bases/{kb_id}")


class TestRealGovernedTransport:
    def test_blocks_the_cloud_metadata_endpoint(self):
        c = httpx.Client(transport=GovernedTransport())
        with pytest.raises(SSRFBlocked):
            c.get("http://169.254.169.254/latest/meta-data/")

    def test_blocks_an_rfc1918_address(self):
        c = httpx.Client(transport=GovernedTransport())
        with pytest.raises(SSRFBlocked):
            c.get("http://10.0.0.5/")

    def test_allows_a_legitimate_request_through_with_headers_intact(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"auth={self.headers.get('Authorization')}".encode())
            def log_message(self, *a): pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch("app.egress._is_unsafe_ip", return_value=False):
                c = governed_httpx_client()
                resp = c.get(f"http://127.0.0.1:{port}/", headers={"Authorization": "Bearer test-key"})
                assert resp.status_code == 200
                assert "test-key" in resp.text
        finally:
            server.shutdown()

    def test_validates_every_redirect_hop_not_just_the_first(self):
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
            def log_message(self, *a): pass

        server = socketserver.TCPServer(("127.0.0.1", 0), RedirectHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            import app.egress as egress_module
            real_validate = egress_module._resolve_and_validate

            def allow_only_test_server(hostname, p, **kwargs):
                if hostname == "127.0.0.1":
                    return "127.0.0.1"
                return real_validate(hostname, p, **kwargs)

            with patch("app.egress._resolve_and_validate", side_effect=allow_only_test_server):
                c = httpx.Client(transport=GovernedTransport(), follow_redirects=True)
                with pytest.raises(SSRFBlocked, match="169.254.169.254"):
                    c.get(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()


class TestRealModerationEndpointGoverned:
    def test_a_custom_moderation_endpoint_pointed_at_the_metadata_service_is_blocked(self):
        r = client.post("/api/agents", json={
            "name": "Egress Moderation Test", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "resp"}},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
            },
        })
        assert r.status_code == 200, r.text
        agent_id = r.json()["id"]
        try:
            fr = client.patch(f"/api/agents/{agent_id}/features", json={
                "features": {"moderation": {"enabled": True, "scope": "input", "provider": "custom_endpoint",
                                             "custom_endpoint_url": "http://169.254.169.254/latest/meta-data/"}}
            })
            assert fr.status_code == 200

            rr = client.post(f"/api/agents/{agent_id}/run", json={"input": "hi"})
            assert rr.status_code == 200
            body = rr.json()
            assert body["status"] == "blocked"
            assert "egress policy" in body["output"]
            assert body["fail_class"] == "moderation"
        finally:
            cleanup_agent(agent_id)


class TestRealKBReachabilityGoverned:
    def test_a_non_pgvector_kb_url_pointed_at_metadata_is_blocked_and_key_never_sent(self):
        r = client.post("/api/integrations/knowledge-bases", json={
            "name": "Egress KB Reachability Test", "provider": "pinecone",
            "config": {"url": "http://169.254.169.254/latest/meta-data/", "api_key": "should-never-leave"},
        })
        assert r.status_code == 200, r.text
        kb_id = r.json()["id"]
        try:
            tr = client.post(f"/api/integrations/knowledge-bases/{kb_id}/test")
            assert tr.status_code == 200
            body = tr.json()
            assert body["test_result"]["ok"] is False
            assert "egress policy" in body["test_result"]["error"]
        finally:
            cleanup_kb(kb_id)


class TestRealDatabaseHostValidation:
    def test_legitimate_database_deployment_patterns_are_allowed(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "172.16.0.1", "192.168.1.1"):
            validate_database_host(host, 5432)  # must not raise

    def test_the_cloud_metadata_endpoint_is_blocked(self):
        with pytest.raises(SSRFBlocked):
            validate_database_host("169.254.169.254", 5432)

    def test_the_metadata_hostname_alias_is_blocked(self):
        with pytest.raises(SSRFBlocked):
            validate_database_host("metadata.google.internal", 5432)

    def test_is_unsafe_database_host_ip_is_narrower_than_the_http_denylist(self):
        import ipaddress
        assert _is_unsafe_database_host_ip(ipaddress.ip_address("10.0.0.5")) is False
        assert _is_unsafe_database_host_ip(ipaddress.ip_address("127.0.0.1")) is False
        assert _is_unsafe_database_host_ip(ipaddress.ip_address("192.168.1.1")) is False
        assert _is_unsafe_database_host_ip(ipaddress.ip_address("169.254.169.254")) is True

    def test_a_real_kb_pointed_at_the_metadata_endpoint_is_blocked_end_to_end(self):
        r = client.post("/api/integrations/knowledge-bases", json={
            "name": "Egress DB Host Test", "provider": "pgvector",
            "config": {"host": "169.254.169.254", "port": 5432, "dbname": "x", "user": "x", "password": "x"},
        })
        assert r.status_code == 200, r.text
        kb_id = r.json()["id"]
        try:
            tr = client.post(f"/api/integrations/knowledge-bases/{kb_id}/test")
            assert tr.status_code == 200
            body = tr.json()
            assert body["test_result"]["ok"] is False
            assert "blocked" in body["test_result"]["error"]
        finally:
            cleanup_kb(kb_id)

    def test_a_real_kb_pointed_at_a_local_address_is_not_blocked_by_validation(self):
        r = client.post("/api/integrations/knowledge-bases", json={
            "name": "Egress DB Host Local Test", "provider": "pgvector",
            "config": {"host": "127.0.0.1", "port": 5432, "dbname": "nonexistent_db_xyz", "user": "nonexistent_user_xyz", "password": "x"},
        })
        assert r.status_code == 200, r.text
        kb_id = r.json()["id"]
        try:
            tr = client.post(f"/api/integrations/knowledge-bases/{kb_id}/test")
            assert tr.status_code == 200
            body = tr.json()
            assert body["test_result"]["ok"] is False
            assert "blocked" not in body["test_result"]["error"]
        finally:
            cleanup_kb(kb_id)
