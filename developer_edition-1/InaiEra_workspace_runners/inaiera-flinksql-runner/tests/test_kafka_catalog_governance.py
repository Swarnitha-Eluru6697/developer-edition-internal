"""RUN-FLINK-016 — the Kafka catalog/preview client had four problems:

  1. It called the Confluent REST Proxy and Schema Registry with no
     credentials at all, regardless of what security (SASL_SSL, Schema
     Registry auth) this deployment is actually configured with.
  2. Every preview used the SAME shared consumer group
     ("inaiera-preview-group") — concurrent previews from different
     users/orgs could race each other's partition assignment and offsets.
  3. Nothing capped preview response size (bytes) or duration (time) —
     only the API's own `max_messages` query param capped row count, and
     only if a caller happened to go through that specific endpoint.
  4. On any failure, it silently returned fabricated "dev fallback" topics
     and messages — in ANY environment, including prod — that someone
     authoring a Flink SQL source could mistake for real cluster data.
  5. There was no policy check at all before returning preview data, and
     no audit trail of who previewed what.

These tests cover the KafkaAdminClient fixes directly (security headers,
consumer identity, caps, dev-only fallback) using the same
monkeypatched-httpx.AsyncClient technique already used in
test_flink_gateway_job_id.py, plus check_preview_policy() and the
preview_topic() route function's policy enforcement / audit logging.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.perms import VIEWER
from app.core.config import Settings
from app.kafka.client import KafkaAdminClient, check_preview_policy, check_topic_access, filter_topics


REST = "https://rest-proxy.internal:8443"


def _settings(**overrides) -> Settings:
    base = {
        "internal_service_token": "test-secret",
        "org_id": "org1",
        "kafka_bootstrap_servers": "kafka-host:9092",
        "kafka_rest_proxy_url": REST,
    }
    base.update(overrides)
    return Settings(**base)


def _client(settings: Settings) -> KafkaAdminClient:
    return KafkaAdminClient(settings)


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeStream:
    def __init__(self, resp: _FakeResponse, chunk: int = 64):
        self._resp = resp
        self.status_code = resp.status_code
        self._chunk = chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self):
        body = json.dumps(self._resp._payload).encode()
        for i in range(0, len(body), self._chunk):
            yield body[i:i + self._chunk]


class _FakeHttpClient:
    """Records every call (method, url, auth) and answers by URL suffix.
    Also records the AsyncClient constructor kwargs (TLS verify)."""

    def __init__(self, *, responses: dict[str, _FakeResponse] | None = None,
                fail: bool = False, delay_seconds: float = 0.0):
        self.responses = responses or {}
        self.fail = fail
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, str, object]] = []
        self.client_kwargs: list[dict] = []

    def __call__(self, **kw):
        self.client_kwargs.append(kw)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def _handle(self, method: str, url: str, auth=None):
        self.calls.append((method, url, auth))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("connection refused")
        for suffix, resp in self.responses.items():
            if url.endswith(suffix) or suffix in url:
                return resp
        return _FakeResponse(404, {})

    async def get(self, url, auth=None, **kw):
        return await self._handle("GET", url, auth)

    async def post(self, url, json=None, auth=None, **kw):
        return await self._handle("POST", url, auth)

    async def delete(self, url, auth=None, **kw):
        return await self._handle("DELETE", url, auth)

    def stream(self, method, url, auth=None, **kw):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                resp = await outer._handle(method, url, auth)
                self_inner.s = _FakeStream(resp)
                return self_inner.s

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


# ---------------------------------------------------------------------
# Security: configured SASL/Schema-Registry credentials are actually used
# ---------------------------------------------------------------------
async def test_list_topics_sends_configured_basic_auth_to_rest_proxy(monkeypatch):
    settings = _settings(kafka_sasl_username="svc-user", kafka_sasl_password="svc-pass")
    client = _client(settings)
    fake = _FakeHttpClient(responses={
        "/v3/clusters": _FakeResponse(200, {"data": [{"cluster_id": "c1"}]}),
        "/topics": _FakeResponse(200, {"data": []}),
    })
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    await client.list_topics()

    assert len(fake.calls) >= 1
    for _, _, auth in fake.calls:
        assert isinstance(auth, httpx.BasicAuth)


async def test_list_topics_sends_no_auth_when_nothing_configured(monkeypatch):
    settings = _settings()  # no SASL creds
    client = _client(settings)
    fake = _FakeHttpClient(responses={
        "/v3/clusters": _FakeResponse(200, {"data": [{"cluster_id": "c1"}]}),
        "/topics": _FakeResponse(200, {"data": []}),
    })
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    await client.list_topics()

    assert all(auth is None for _, _, auth in fake.calls)


async def test_schema_registry_auth_only_sent_when_explicitly_enabled(monkeypatch):
    settings = _settings(kafka_sasl_username="u", kafka_sasl_password="p",
                         schema_registry_auth_enabled=False)
    client = _client(settings)
    fake = _FakeHttpClient(responses={"/versions/latest": _FakeResponse(404, {})})
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    await client.get_topic_schema("orders")

    assert all(auth is None for _, _, auth in fake.calls)


# ---------------------------------------------------------------------
# Fake data must not survive outside dev
# ---------------------------------------------------------------------
async def test_list_topics_returns_empty_outside_dev_when_proxy_unreachable(monkeypatch):
    settings = _settings(environment="prod")
    client = _client(settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient",
                        _FakeHttpClient(fail=True))

    assert await client.list_topics() == []


async def test_list_topics_still_falls_back_to_dev_fixtures_in_dev(monkeypatch):
    settings = _settings(environment="dev")
    client = _client(settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient",
                        _FakeHttpClient(fail=True))

    topics = await client.list_topics()
    assert len(topics) == 3
    assert topics[0]["name"] == "raw.orders"


async def test_preview_returns_empty_outside_dev_when_proxy_unreachable(monkeypatch):
    settings = _settings(environment="staging")
    client = _client(settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient",
                        _FakeHttpClient(fail=True))

    assert await client.preview_messages("orders") == []


async def test_preview_still_falls_back_to_dev_fixture_in_dev(monkeypatch):
    settings = _settings(environment="dev")
    client = _client(settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient",
                        _FakeHttpClient(fail=True))

    messages = await client.preview_messages("orders")
    assert len(messages) == 1
    assert messages[0]["key"] == "ord_001"


# ---------------------------------------------------------------------
# Preview identity, caps
# ---------------------------------------------------------------------
class _PreviewFakeClient(_FakeHttpClient):
    """REST Proxy v2 consumer lifecycle: create -> (partitions/offsets ->
    assignments -> positions | subscription) -> records -> delete."""

    def __init__(self, records: list[dict], *, partitions=(0,), end_offset=1000,
                 offsets_available=True):
        super().__init__()
        self._records = records
        self._partitions = partitions
        self._end = end_offset
        self._offsets_available = offsets_available
        self.consumer_groups_used: list[str] = []
        self.positions: list[dict] = []
        self.subscribed = False
        self._served = False

    async def _handle(self, method: str, url: str, auth=None, body=None):
        self.calls.append((method, url, auth))
        if method == "POST" and "/consumers/" in url and "/instances/" not in url:
            self.consumer_groups_used.append(url.rsplit("/consumers/", 1)[1])
            return _FakeResponse(200, {"instance_id": "inst-1",
                                       "base_uri": "http://internal-proxy/consumers/x/instances/inst-1"})
        if method == "GET" and url.endswith("/partitions"):
            return _FakeResponse(200, [{"partition": p} for p in self._partitions])
        if method == "GET" and url.endswith("/offsets"):
            if not self._offsets_available:
                return _FakeResponse(500, {})
            return _FakeResponse(200, {"beginning_offset": 0, "end_offset": self._end})
        if method == "POST" and url.endswith("/assignments"):
            return _FakeResponse(204, {})
        if method == "POST" and url.endswith("/positions"):
            self.positions.append(body)
            return _FakeResponse(204, {})
        if method == "POST" and url.endswith("/subscription"):
            self.subscribed = True
            return _FakeResponse(204, {})
        if method == "GET" and "/records" in url:
            if self._served:
                return _FakeResponse(200, [])
            self._served = True
            return _FakeResponse(200, self._records)
        if method == "DELETE":
            return _FakeResponse(204, {})
        return _FakeResponse(404, {})

    async def post(self, url, json=None, auth=None, **kw):
        return await self._handle("POST", url, auth, body=json)


async def test_preview_uses_a_fresh_consumer_group_every_call(monkeypatch):
    settings = _settings(environment="dev")
    client = _client(settings)
    fake = _PreviewFakeClient(records=[])
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    await client.preview_messages("orders")
    await client.preview_messages("orders")

    assert len(fake.consumer_groups_used) == 2
    assert fake.consumer_groups_used[0] != fake.consumer_groups_used[1]
    assert all(g.startswith("inaiera-preview-") for g in fake.consumer_groups_used)
    assert "inaiera-preview-group" not in fake.consumer_groups_used


async def test_preview_deletes_the_consumer_after_use(monkeypatch):
    settings = _settings(environment="dev")
    client = _client(settings)
    fake = _PreviewFakeClient(records=[])
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    await client.preview_messages("orders")

    assert any(method == "DELETE" for method, _, _ in fake.calls)


async def test_preview_caps_row_count_to_configured_max_regardless_of_caller(monkeypatch):
    settings = _settings(environment="dev", kafka_preview_max_messages=2)
    client = _client(settings)
    records = [
        {"partition": 0, "offset": i, "key": None, "value": {}, "timestamp": 0}
        for i in range(10)
    ]
    fake = _PreviewFakeClient(records=records)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    # Caller asks for far more than the configured cap.
    messages = await client.preview_messages("orders", max_messages=50)
    assert len(messages) == 2


async def test_preview_truncates_an_oversized_message_value(monkeypatch):
    settings = _settings(environment="dev", kafka_preview_max_bytes_per_message=20)
    client = _client(settings)
    big_value = {"payload": "x" * 1000}
    fake = _PreviewFakeClient(records=[
        {"partition": 0, "offset": 1, "key": None, "value": big_value, "timestamp": 0},
    ])
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    messages = await client.preview_messages("orders")
    assert len(messages) == 1
    assert messages[0]["value"]["_truncated"] is True
    assert messages[0]["value"]["_original_size_bytes"] > 20


async def test_preview_leaves_a_small_message_value_untouched(monkeypatch):
    settings = _settings(environment="dev")
    client = _client(settings)
    small_value = {"order_id": 1}
    fake = _PreviewFakeClient(records=[
        {"partition": 0, "offset": 1, "key": "k1", "value": small_value, "timestamp": 123},
    ])
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    messages = await client.preview_messages("orders")
    assert messages[0]["value"] == small_value


async def test_preview_times_out_and_returns_empty_outside_dev(monkeypatch):
    settings = _settings(environment="prod", kafka_preview_timeout_seconds=0.05)
    client = _client(settings)
    fake = _FakeHttpClient(delay_seconds=1.0)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    messages = await client.preview_messages("orders")
    assert messages == []


# ---------------------------------------------------------------------
# RUN-FLINK-016: REST Proxy URL / TLS, sensible offsets, total caps
# ---------------------------------------------------------------------
async def test_rest_proxy_is_reached_at_the_configured_https_url_with_ca_bundle(monkeypatch):
    settings = _settings(kafka_rest_proxy_url="https://proxy.example:8443/",
                         kafka_rest_proxy_ca_file="/etc/inaiera/tls/ca.crt")
    fake = _FakeHttpClient(responses={
        "/v3/clusters": _FakeResponse(200, {"data": [{"cluster_id": "c1"}]}),
        "/topics": _FakeResponse(200, {"data": []}),
    })
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    await _client(settings).list_topics()
    assert fake.calls and all(url.startswith("https://proxy.example:8443/") for _, url, _ in fake.calls)
    assert all(":8082" not in url for _, url, _ in fake.calls)   # never the old derived host:8082
    assert fake.client_kwargs[0]["verify"] == "/etc/inaiera/tls/ca.crt"


async def test_rest_proxy_not_configured_means_nothing_outside_dev(monkeypatch):
    settings = _settings(kafka_rest_proxy_url=None, environment="prod")
    fake = _FakeHttpClient()
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    c = _client(settings)
    assert await c.list_topics() == []
    assert await c.preview_messages("orders") == []
    assert await c.get_consumer_group_lag("g") == {}
    assert fake.calls == []


async def test_schema_registry_uses_its_own_credentials_and_ca(monkeypatch):
    settings = _settings(schema_registry_auth_enabled=True, schema_registry_username="sr-user",
                         schema_registry_password="sr-pass", schema_registry_ca_file="/ca/sr.pem",
                         kafka_sasl_username="kafka-user", kafka_sasl_password="kafka-pass")
    fake = _FakeHttpClient(responses={"/versions/latest": _FakeResponse(404, {})})
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    await _client(settings).get_topic_schema("orders")
    auth = fake.calls[0][2]
    assert isinstance(auth, httpx.BasicAuth)
    assert auth._auth_header == httpx.BasicAuth("sr-user", "sr-pass")._auth_header
    assert fake.client_kwargs[0]["verify"] == "/ca/sr.pem"


async def test_preview_seeks_each_partition_to_end_minus_n(monkeypatch):
    settings = _settings(environment="prod")
    fake = _PreviewFakeClient(records=[], partitions=(0, 1), end_offset=1000)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    await _client(settings).preview_messages("orders", max_messages=10)
    assert fake.subscribed is False
    offsets = {o["partition"]: o["offset"] for o in fake.positions[0]["offsets"]}
    assert offsets == {0: 995, 1: 995}   # end - ceil(10 / 2 partitions)
    # Instance addressed through the configured URL, not the proxy's base_uri.
    assert all(url.startswith(REST) for _, url, _ in fake.calls)


async def test_preview_falls_back_to_earliest_subscription_when_offsets_unavailable(monkeypatch):
    fake = _PreviewFakeClient(records=[], offsets_available=False)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    await _client(_settings(environment="prod")).preview_messages("orders")
    assert fake.subscribed is True


async def test_preview_total_bytes_are_capped(monkeypatch):
    settings = _settings(environment="prod", kafka_preview_max_total_bytes=400,
                         kafka_preview_max_bytes_per_message=8192)
    records = [{"partition": 0, "offset": i, "key": None, "value": {"v": "x" * 100}, "timestamp": 0}
               for i in range(20)]
    fake = _PreviewFakeClient(records=records)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    result = await _client(settings).preview("orders", max_messages=50)
    assert 0 < len(result["messages"]) < 20
    assert result["truncated"] is True
    assert result["bytes"] <= 400


async def test_preview_key_is_capped(monkeypatch):
    settings = _settings(environment="prod", kafka_preview_max_key_bytes=8)
    fake = _PreviewFakeClient(records=[
        {"partition": 0, "offset": 1, "key": "k" * 500, "value": {}, "timestamp": 0}])
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    messages = await _client(settings).preview_messages("orders")
    assert messages[0]["key"].startswith("kkkkkkkk") and "truncated" in messages[0]["key"]
    assert len(messages[0]["key"]) < 30


async def test_preview_aborts_an_oversized_raw_response(monkeypatch):
    settings = _settings(environment="prod", kafka_preview_max_total_bytes=10)
    huge = [{"partition": 0, "offset": i, "key": None, "value": {"v": "y" * 5000}, "timestamp": 0}
            for i in range(50)]
    fake = _PreviewFakeClient(records=huge)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    assert await _client(settings).preview_messages("orders") == []   # stream cut off, nothing served


async def test_consumer_lag_uses_configured_proxy_and_nests_partitions(monkeypatch):
    fake = _FakeHttpClient(responses={
        "/lags": _FakeResponse(200, {"data": [
            {"topic_name": "t1", "partition_id": 0, "lag": 3},
            {"topic_name": "t1", "partition_id": 1, "lag": 4}]}),
        "/v3/clusters": _FakeResponse(200, {"data": [{"cluster_id": "c1"}]}),
    })
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    lag = await _client(_settings(kafka_sasl_username="u", kafka_sasl_password="p")) \
        .get_consumer_group_lag("orders-etl")
    assert lag == {"total_lag": 7, "per_partition": {"t1": {"0": 3, "1": 4}}}
    assert all(isinstance(a, httpx.BasicAuth) for _, _, a in fake.calls)


async def test_consumer_lag_unavailable_is_empty_not_zero(monkeypatch):
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", _FakeHttpClient(fail=True))
    assert await _client(_settings()).get_consumer_group_lag("g") == {}


# ---------------------------------------------------------------------
# check_preview_policy / topic access — UCS + org scoping
# ---------------------------------------------------------------------
async def test_policy_allows_in_dev_when_ucs_not_configured(monkeypatch):
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: _settings(ucs_url=None))
    allowed, reason = await check_preview_policy("org1", "user1", "orders")
    assert allowed is True and reason is None


async def test_preview_fails_closed_outside_dev_when_ucs_not_configured(monkeypatch):
    monkeypatch.setattr("app.kafka.client.get_settings",
                        lambda: _settings(ucs_url=None, environment="prod"))
    allowed, reason = await check_preview_policy("org1", "user1", "orders")
    assert allowed is False and "not configured" in reason


class _UcsFakeClient:
    def __init__(self, status_code: int, payload: dict):
        self._status_code = status_code
        self._payload = payload
        self.calls = []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return _FakeResponse(self._status_code, self._payload)


async def test_policy_denies_when_ucs_denies(monkeypatch):
    settings = _settings(ucs_url="http://ucs", ucs_internal_token="ucs-secret", environment="prod")
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    fake = _UcsFakeClient(200, {"allowed": False, "reason": "classified: PII"})
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)

    allowed, reason = await check_preview_policy("org1", "user1", "customers.pii")
    assert allowed is False
    assert reason == "classified: PII"
    assert fake.calls[0][1] == {"org_id": "org1", "user_id": "user1", "topic": "customers.pii",
                                "action": "preview"}
    assert fake.calls[0][2]["Authorization"].startswith("Bearer ")


async def test_policy_allows_when_ucs_allows(monkeypatch):
    settings = _settings(ucs_url="http://ucs", ucs_internal_token="ucs-secret")
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient",
                        _UcsFakeClient(200, {"allowed": True, "reason": None}))
    allowed, _reason = await check_preview_policy("org1", "user1", "orders")
    assert allowed is True


class _Boom:
    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *a, **kw):
        raise RuntimeError("unreachable")


async def test_policy_fails_closed_outside_dev_when_ucs_unreachable(monkeypatch):
    settings = _settings(ucs_url="http://ucs", ucs_internal_token="ucs-secret", environment="prod")
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", _Boom())
    allowed, reason = await check_preview_policy("org1", "user1", "orders")
    assert allowed is False and reason


async def test_policy_fails_open_in_dev_when_ucs_unreachable(monkeypatch):
    settings = _settings(ucs_url="http://ucs", ucs_internal_token="ucs-secret", environment="dev")
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", _Boom())
    allowed, _ = await check_preview_policy("org1", "user1", "orders")
    assert allowed is True


_TOPICS = [{"name": n, "partitions": 1, "replication_factor": 1, "message_count_estimate": None,
            "schema_format": "json", "schema_id": None}
           for n in ("org1.orders", "org1.customers", "org2.orders", "raw.shared")]


async def test_topic_listing_is_scoped_to_the_orgs_patterns(monkeypatch):
    settings = _settings(environment="prod", kafka_topic_allowed_patterns=["{org_id}.*"])
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    topics, reason = await filter_topics("org1", "u", _TOPICS)
    assert [t["name"] for t in topics] == ["org1.orders", "org1.customers"] and reason is None


async def test_topic_listing_applies_the_ucs_batch_policy(monkeypatch):
    settings = _settings(environment="prod", ucs_url="http://ucs", ucs_internal_token="s",
                         kafka_topic_allowed_patterns=["{org_id}.*"])
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    fake = _UcsFakeClient(200, {"allowed": ["org1.orders"]})
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    topics, _ = await filter_topics("org1", "u", _TOPICS)
    assert [t["name"] for t in topics] == ["org1.orders"]
    assert fake.calls[0][1]["topics"] == ["org1.orders", "org1.customers"]   # pre-filtered by org


async def test_topic_listing_fails_closed_outside_dev_with_no_policy_configured(monkeypatch):
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: _settings(environment="prod"))
    topics, reason = await filter_topics("org1", "u", _TOPICS)
    assert topics == [] and reason


async def test_schema_lookup_outside_org_patterns_is_denied(monkeypatch):
    settings = _settings(environment="prod", kafka_topic_allowed_patterns=["{org_id}.*"])
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    assert (await check_topic_access("org1", "u", "org2.orders", "schema"))[0] is False
    assert (await check_topic_access("org1", "u", "org1.orders", "schema"))[0] is True


async def test_preview_outside_org_patterns_is_denied_before_ucs_is_asked(monkeypatch):
    settings = _settings(environment="prod", ucs_url="http://ucs", ucs_internal_token="s",
                         kafka_topic_allowed_patterns=["{org_id}.*"])
    monkeypatch.setattr("app.kafka.client.get_settings", lambda: settings)
    fake = _UcsFakeClient(200, {"allowed": True})
    monkeypatch.setattr("app.kafka.client.httpx.AsyncClient", fake)
    allowed, _ = await check_topic_access("org1", "u", "org2.orders", "preview")
    assert allowed is False and fake.calls == []


# ---------------------------------------------------------------------
# The API route: denies + audits
# ---------------------------------------------------------------------
async def test_preview_topic_route_denies_and_does_not_call_kafka_when_policy_denies(monkeypatch):
    from fastapi import HTTPException

    from app.api.kafka_catalog import preview_topic
    from app.core.auth import Principal

    async def _deny(org_id, user_id, topic, action):
        return False, "classified"
    monkeypatch.setattr("app.api.kafka_catalog.check_topic_access", _deny)
    audits = []

    async def _audit(**kw):
        audits.append(kw)
        return None
    monkeypatch.setattr("app.api.kafka_catalog.record_access", _audit)

    def _get_kafka_client_should_not_be_called():
        raise AssertionError("kafka client must not be used when preview is denied")
    monkeypatch.setattr("app.api.kafka_catalog.get_kafka_client",
                        _get_kafka_client_should_not_be_called)

    user = Principal(user_id="u1", org_id="org1", permissions=frozenset(VIEWER))
    with pytest.raises(HTTPException) as exc_info:
        await preview_topic("customers.pii", user, max_messages=10)

    assert exc_info.value.status_code == 403
    assert audits == [{"org_id": "org1", "user_id": "u1", "action": "preview",
                       "topic": "customers.pii", "allowed": False, "reason": "classified"}]


async def test_preview_topic_route_allows_and_returns_data_when_policy_allows(monkeypatch):
    from app.api.kafka_catalog import preview_topic
    from app.core.auth import Principal

    async def _allow(org_id, user_id, topic, action):
        return True, None
    monkeypatch.setattr("app.api.kafka_catalog.check_topic_access", _allow)

    async def _audit(**kw):
        return None
    monkeypatch.setattr("app.api.kafka_catalog.record_access", _audit)

    class _FakeKafkaClient:
        async def preview(self, topic, max_messages):
            return {"messages": [{"partition": 0, "offset": 1, "key": None, "value": {},
                                  "timestamp_ms": 0}], "truncated": False, "bytes": 30}

        async def get_topic_schema(self, topic):
            return None

    monkeypatch.setattr("app.api.kafka_catalog.get_kafka_client", lambda: _FakeKafkaClient())

    user = Principal(user_id="u1", org_id="org1", permissions=frozenset(VIEWER))
    result = await preview_topic("orders", user, max_messages=10)

    assert result.topic == "orders"
    assert len(result.messages) == 1
    assert result.bytes_returned == 30


async def test_preview_is_durably_audited_in_postgres(pg_db, monkeypatch):
    from sqlalchemy import select

    from app.api.kafka_catalog import preview_topic
    from app.core.auth import Principal
    from app.models import KafkaPreviewAudit

    async def _allow(org_id, user_id, topic, action):
        return True, None
    monkeypatch.setattr("app.api.kafka_catalog.check_topic_access", _allow)

    class _FakeKafkaClient:
        async def preview(self, topic, max_messages):
            return {"messages": [{"partition": 0, "offset": 1, "key": None, "value": {"a": 1},
                                  "timestamp_ms": 0}] * 2, "truncated": False, "bytes": 88}

        async def get_topic_schema(self, topic):
            return None

    monkeypatch.setattr("app.api.kafka_catalog.get_kafka_client", lambda: _FakeKafkaClient())
    await preview_topic("orders", Principal(user_id="u1", org_id="org1", permissions=frozenset(VIEWER)),
                        max_messages=5)

    async def _deny(org_id, user_id, topic, action):
        return False, "classified: PII"
    monkeypatch.setattr("app.api.kafka_catalog.check_topic_access", _deny)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await preview_topic("pii", Principal(user_id="u2", org_id="org1", permissions=frozenset(VIEWER)),
                            max_messages=5)

    async with pg_db() as db:
        rows = (await db.execute(select(KafkaPreviewAudit).order_by(KafkaPreviewAudit.created_at))).scalars().all()
    assert [(r.user_id, r.topic, r.allowed, r.message_count, r.bytes_returned) for r in rows] == [
        ("u1", "orders", True, 2, 88), ("u2", "pii", False, None, None)]
    assert rows[1].reason == "classified: PII"


async def test_preview_is_refused_outside_dev_when_the_audit_cannot_be_written(monkeypatch):
    from fastapi import HTTPException

    from app.services import preview_audit

    monkeypatch.setattr("app.services.preview_audit.get_settings",
                        lambda: _settings(environment="prod"))

    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.services.preview_audit.AsyncSessionLocal", lambda: _BrokenSession())
    with pytest.raises(HTTPException) as exc:
        await preview_audit.record_access(org_id="o", user_id="u", action="preview",
                                          topic="t", allowed=True)
    assert exc.value.status_code == 503
