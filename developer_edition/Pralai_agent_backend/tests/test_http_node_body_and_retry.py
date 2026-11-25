"""
Real tests for the HTTP node body-type and retry fix (section 0.65,
GAP_REPORT).

The real bug, exactly as reported: the config panel offered JSON,
form-data, raw, and binary body types plus retry_on_failure/
max_retries, but handle_http only ever built a request body for
body_type == "JSON", and made exactly one real request regardless of
retry configuration.

The fix: handle_http now builds a real request body for every offered
type (form-data as real application/x-www-form-urlencoded, raw as the
exact real string, binary as real base64-decoded bytes), routed
through a new real data= parameter on egress.safe_http_request
(alongside the existing json= parameter, both going through the same
real SSRF/redirect governance). A real retry loop makes up to
max_retries additional real attempts on a real 5xx response or a real
request exception - never on a 4xx (a real client error a retry can't
fix) or a real SSRFBlocked policy block (retrying a blocked
destination can never succeed).

These tests mock only egress.safe_http_request itself (the real
network boundary) - everything else (body construction, retry
counting, error classification) is exercised for real.
"""
import sys
import os
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch

from app.harness import handle_http
from app.egress import SSRFBlocked
import app.egress as egress_module


def fake_ok(method, url, headers=None, params=None, json=None, data=None, calls=None):
    if calls is not None:
        calls.append({"json": json, "data": data})
    return {"status_code": 200, "text": "ok", "headers": {}, "content": b"ok", "final_url": url}


class TestRealBodyTypeConstruction:
    def test_json_body_type_accepts_a_real_json_string_and_parses_it(self):
        calls = []
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "JSON", "body_json": '{"a": 1}'}, ctx)
        assert calls[0]["json"] == {"a": 1}
        assert calls[0]["data"] is None

    def test_json_body_type_still_accepts_an_already_parsed_dict(self):
        calls = []
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "JSON", "body_json": {"a": 1}}, ctx)
        assert calls[0]["json"] == {"a": 1}

    def test_invalid_json_in_body_produces_a_real_honest_error_not_a_crash(self):
        ctx = {"vars": {}}
        handle_http({"url": "https://example.com", "body_type": "JSON", "body_json": "{not valid json"}, ctx)
        assert "error" in ctx["vars"]["http_output"]

    def test_form_data_body_type_genuinely_sends_the_real_parsed_fields(self):
        calls = []
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "form-data", "body_form_text": "a=1\nb=2"}, ctx)
        assert calls[0]["data"] == {"a": "1", "b": "2"}
        assert calls[0]["json"] is None

    def test_raw_body_type_sends_the_exact_real_string(self):
        calls = []
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "raw", "body_raw_text": "hello raw body"}, ctx)
        assert calls[0]["data"] == "hello raw body"

    def test_binary_body_type_decodes_real_base64_to_real_bytes(self):
        calls = []
        b64 = base64.b64encode(b"binary content here").decode()
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "binary", "body_binary_base64": b64}, ctx)
        assert calls[0]["data"] == b"binary content here"

    def test_invalid_base64_produces_a_real_honest_error_not_a_crash(self):
        ctx = {"vars": {}}
        handle_http({"url": "https://example.com", "body_type": "binary", "body_binary_base64": "not!valid!base64!!!"}, ctx)
        assert "error" in ctx["vars"]["http_output"]

    def test_body_type_none_sends_neither_json_nor_data(self):
        calls = []
        with patch.object(egress_module, "safe_http_request", lambda *a, **kw: fake_ok(*a, calls=calls, **kw)):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "body_type": "none"}, ctx)
        assert calls[0]["json"] is None
        assert calls[0]["data"] is None


class TestRealRetryLoop:
    def test_a_real_5xx_is_retried_up_to_max_retries_times(self):
        call_count = {"n": 0}

        def failing(method, url, **kw):
            call_count["n"] += 1
            return {"status_code": 503, "text": "server error", "headers": {}, "content": b"", "final_url": url}

        with patch.object(egress_module, "safe_http_request", failing):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 2}, ctx)

        assert call_count["n"] == 3
        assert ctx["vars"]["http_output"]["status_code"] == 503
        assert ctx["vars"]["http_output"]["attempts"] == 3

    def test_a_real_4xx_is_never_retried(self):
        call_count = {"n": 0}

        def failing(method, url, **kw):
            call_count["n"] += 1
            return {"status_code": 404, "text": "not found", "headers": {}, "content": b"", "final_url": url}

        with patch.object(egress_module, "safe_http_request", failing):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 5}, ctx)

        assert call_count["n"] == 1

    def test_a_real_ssrf_block_is_never_retried(self):
        call_count = {"n": 0}

        def blocked(method, url, **kw):
            call_count["n"] += 1
            raise SSRFBlocked("blocked destination")

        with patch.object(egress_module, "safe_http_request", blocked):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 5}, ctx)

        assert call_count["n"] == 1
        assert "blocked by egress policy" in ctx["vars"]["http_output"]["error"]

    def test_retry_on_failure_false_means_exactly_one_attempt_even_on_5xx(self):
        call_count = {"n": 0}

        def failing(method, url, **kw):
            call_count["n"] += 1
            return {"status_code": 503, "text": "server error", "headers": {}, "content": b"", "final_url": url}

        with patch.object(egress_module, "safe_http_request", failing):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": False, "max_retries": 5}, ctx)

        assert call_count["n"] == 1

    def test_a_real_network_exception_is_also_retried(self):
        call_count = {"n": 0}

        def failing(method, url, **kw):
            call_count["n"] += 1
            raise ConnectionError("simulated real network failure")

        with patch.object(egress_module, "safe_http_request", failing):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 1}, ctx)

        assert call_count["n"] == 2
        assert "simulated real network failure" in ctx["vars"]["http_output"]["error"]

    def test_a_real_success_after_a_real_transient_failure_returns_the_success(self):
        call_count = {"n": 0}

        def flaky(method, url, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"status_code": 503, "text": "temporary", "headers": {}, "content": b"", "final_url": url}
            return {"status_code": 200, "text": "recovered", "headers": {}, "content": b"recovered", "final_url": url}

        with patch.object(egress_module, "safe_http_request", flaky):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 3}, ctx)

        assert ctx["vars"]["http_output"]["status_code"] == 200
        assert ctx["vars"]["http_output"]["attempts"] == 2

    def test_max_retries_is_clamped_to_a_real_sane_range(self):
        call_count = {"n": 0}

        def failing(method, url, **kw):
            call_count["n"] += 1
            return {"status_code": 503, "text": "x", "headers": {}, "content": b"", "final_url": url}

        with patch.object(egress_module, "safe_http_request", failing):
            ctx = {"vars": {}}
            handle_http({"url": "https://example.com", "retry_on_failure": True, "max_retries": 999}, ctx)

        assert call_count["n"] == 11
