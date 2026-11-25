"""Kafka catalog client — topic discovery, schema resolution, message
preview and consumer-group lag, via the Confluent REST Proxy and Schema
Registry (RUN-FLINK-015/016).

  * REST Proxy URL is explicit configuration (KAFKA_REST_PROXY_URL, http or
    https) — never derived as http://{bootstrap-host}:8082 — and every call
    (catalog, preview, consumer lag) honours its credentials and TLS
    settings (custom CA bundle / verification). Schema Registry has its own
    credentials and CA bundle.
  * Fake/fixture data is served only in dev; elsewhere a failure returns
    nothing.
  * Preview reads from a sensible offset: each partition is assigned and
    seeked to max(beginning, end - N) so a fresh, single-use consumer
    actually returns the most recent messages instead of waiting at
    "latest". Row count, per-message value size, key size, the TOTAL
    response size (and the raw HTTP body, read as a capped stream) and wall
    time are all capped.
  * Topic access goes through check_topic_access()/filter_topics() —
    org-scoped topic patterns plus the UCS policy — for listing, schema
    lookup and preview alike; preview fails closed outside dev when UCS is
    not configured.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import math
import uuid
from typing import Any

import httpx
import structlog

from app.core.config import Settings, get_settings

log = structlog.get_logger()

_V2_JSON = "application/vnd.kafka.v2+json"


class PreviewTooLarge(Exception):
    pass


class KafkaAdminClient:
    def __init__(self, settings: Settings | None = None) -> None:
        s = settings or get_settings()
        self._environment = s.environment
        self._timeout = s.kafka_admin_timeout_seconds

        self._rest_url = s.kafka_rest_proxy_url.rstrip("/") if s.kafka_rest_proxy_url else None
        rest_user = s.kafka_rest_proxy_username or s.kafka_sasl_username
        rest_pass = s.kafka_rest_proxy_password or s.kafka_sasl_password
        self._rest_proxy_auth = httpx.BasicAuth(rest_user, rest_pass) if rest_user and rest_pass else None
        self._rest_verify: bool | str = s.kafka_rest_proxy_ca_file or s.kafka_rest_proxy_verify_ssl

        self._sr_url = s.schema_registry_url.rstrip("/")
        sr_user = s.schema_registry_username or s.kafka_sasl_username
        sr_pass = s.schema_registry_password or s.kafka_sasl_password
        self._schema_registry_auth = (
            httpx.BasicAuth(sr_user, sr_pass)
            if s.schema_registry_auth_enabled and sr_user and sr_pass else None
        )
        self._sr_verify: bool | str = s.schema_registry_ca_file or True

        self._preview_max_messages = s.kafka_preview_max_messages
        self._preview_max_bytes = s.kafka_preview_max_bytes_per_message
        self._preview_max_key_bytes = s.kafka_preview_max_key_bytes
        self._preview_max_total_bytes = s.kafka_preview_max_total_bytes
        self._preview_timeout_seconds = s.kafka_preview_timeout_seconds

    def _rest_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, verify=self._rest_verify)

    def _sr_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, verify=self._sr_verify)

    # ── Topics ────────────────────────────────────────────────────────────

    async def list_topics(self) -> list[dict[str, Any]]:
        """All non-internal topics the REST Proxy reports (UNFILTERED —
        callers apply filter_topics() for the requesting org/user)."""
        if self._rest_url:
            try:
                async with self._rest_client() as c:
                    cluster_id = await self._cluster_id(c)
                    if cluster_id:
                        topics_resp = await c.get(f"{self._rest_url}/v3/clusters/{cluster_id}/topics",
                                                  auth=self._rest_proxy_auth)
                        if topics_resp.status_code == 200:
                            topics = []
                            for t in topics_resp.json().get("data", []):
                                if t["topic_name"].startswith("_"):
                                    continue
                                topics.append({
                                    "name": t["topic_name"],
                                    "partitions": t.get("partitions_count", 1),
                                    "replication_factor": t.get("replication_factor", 1),
                                    "message_count_estimate": None,
                                    "schema_format": await self._get_schema_format(t["topic_name"]),
                                    "schema_id": None,
                                })
                            return topics
            except Exception as e:  # noqa: BLE001
                log.warning("kafka_rest_proxy_unavailable", error=str(e))
        else:
            log.info("kafka_rest_proxy_not_configured")
        if self._environment != "dev":
            return []
        return [
            {"name": "raw.orders", "partitions": 6, "replication_factor": 3,
             "message_count_estimate": None, "schema_format": "json", "schema_id": None},
            {"name": "raw.customers", "partitions": 3, "replication_factor": 3,
             "message_count_estimate": None, "schema_format": "avro", "schema_id": 42},
            {"name": "raw.products", "partitions": 3, "replication_factor": 3,
             "message_count_estimate": None, "schema_format": "json", "schema_id": None},
        ]

    async def _cluster_id(self, c) -> str | None:
        clusters = await c.get(f"{self._rest_url}/v3/clusters", auth=self._rest_proxy_auth)
        if clusters.status_code != 200:
            return None
        data = clusters.json().get("data") or []
        return data[0]["cluster_id"] if data else None

    async def get_topic_schema(self, topic: str) -> dict[str, Any] | None:
        for subject in (f"{topic}-value", f"{topic}-value-json", topic):
            try:
                async with self._sr_client() as c:
                    resp = await c.get(f"{self._sr_url}/subjects/{subject}/versions/latest",
                                       auth=self._schema_registry_auth)
                    if resp.status_code == 200:
                        data = resp.json()
                        schema_str = data.get("schema", "{}")
                        return {
                            "subject": subject,
                            "schema_id": data.get("id"),
                            "version": data.get("version"),
                            "schema": json.loads(schema_str) if isinstance(schema_str, str) else schema_str,
                            "schema_type": data.get("schemaType", "AVRO"),
                        }
            except Exception:  # noqa: BLE001
                pass
        return None

    async def _get_schema_format(self, topic: str) -> str | None:
        schema = await self.get_topic_schema(topic)
        if schema:
            st = schema.get("schema_type", "AVRO")
            return st.lower() if st else "avro"
        return "json"

    # ── Preview ───────────────────────────────────────────────────────────

    async def preview_messages(self, topic: str, max_messages: int = 10) -> list[dict]:
        return (await self.preview(topic, max_messages))["messages"]

    async def preview(self, topic: str, max_messages: int = 10) -> dict:
        """{"messages": [...], "truncated": bool, "bytes": int}."""
        max_messages = max(1, min(max_messages, self._preview_max_messages))
        try:
            if not self._rest_url:
                raise RuntimeError("KAFKA_REST_PROXY_URL not configured")
            return await asyncio.wait_for(self._preview(topic, max_messages),
                                          timeout=self._preview_timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("kafka_preview_timed_out", topic=topic, timeout=self._preview_timeout_seconds)
        except Exception as e:  # noqa: BLE001
            log.warning("kafka_preview_failed", topic=topic, error=str(e))
        if self._environment != "dev":
            return {"messages": [], "truncated": False, "bytes": 0}
        return {"messages": [
            {"partition": 0, "offset": 100, "key": "ord_001",
             "value": {"order_id": 1, "customer_id": 42, "amount": 99.99, "status": "placed"},
             "timestamp_ms": 1751500000000},
        ], "truncated": False, "bytes": 0}

    async def _preview(self, topic: str, max_messages: int) -> dict:
        rest = self._rest_url
        headers = {"Content-Type": _V2_JSON}
        async with self._rest_client() as c:
            # A fresh, single-use consumer group per call (no cross-user sharing).
            group_id = f"inaiera-preview-{uuid.uuid4().hex}"
            consumer_resp = await c.post(
                f"{rest}/consumers/{group_id}",
                json={"name": "preview-consumer", "format": "json",
                      "auto.offset.reset": "earliest", "auto.commit.enable": "false"},
                headers=headers, auth=self._rest_proxy_auth,
            )
            if consumer_resp.status_code not in (200, 201):
                raise RuntimeError(f"REST Proxy refused consumer creation ({consumer_resp.status_code})")
            body = consumer_resp.json() or {}
            instance = body.get("instance_id") or "preview-consumer"
            # Always address the instance through OUR configured URL (the
            # proxy's advertised base_uri may be plain-http / internal).
            consumer_url = f"{rest}/consumers/{group_id}/instances/{instance}"
            try:
                await self._position_at_tail(c, consumer_url, topic, max_messages, headers)
                raw = await self._fetch_records(c, consumer_url, max_messages)
            finally:
                try:
                    await c.delete(consumer_url, headers=headers, auth=self._rest_proxy_auth)
                except Exception:  # noqa: BLE001
                    pass
        return self._cap_records(raw, max_messages)

    async def _position_at_tail(self, c, consumer_url: str, topic: str, n: int, headers: dict) -> None:
        """Assign every partition and seek to max(beginning, end - per_partition_n);
        falls back to a plain subscription (auto.offset.reset=earliest)."""
        rest = self._rest_url
        try:
            parts_resp = await c.get(f"{rest}/topics/{topic}/partitions", auth=self._rest_proxy_auth)
            if parts_resp.status_code != 200:
                raise RuntimeError(f"partitions lookup {parts_resp.status_code}")
            partitions = [p["partition"] for p in parts_resp.json() or []]
            if not partitions:
                raise RuntimeError("no partitions")
            per_partition = max(1, math.ceil(n / len(partitions)))
            offsets = []
            for p in partitions:
                off = await c.get(f"{rest}/topics/{topic}/partitions/{p}/offsets",
                                  auth=self._rest_proxy_auth)
                if off.status_code != 200:
                    raise RuntimeError(f"offsets lookup {off.status_code}")
                o = off.json()
                start = max(int(o.get("beginning_offset", 0)), int(o.get("end_offset", 0)) - per_partition)
                offsets.append({"topic": topic, "partition": p, "offset": start})
            await c.post(f"{consumer_url}/assignments",
                         json={"partitions": [{"topic": topic, "partition": p} for p in partitions]},
                         headers=headers, auth=self._rest_proxy_auth)
            await c.post(f"{consumer_url}/positions", json={"offsets": offsets},
                         headers=headers, auth=self._rest_proxy_auth)
        except Exception as e:  # noqa: BLE001
            log.info("kafka_preview_seek_fallback_subscribe", topic=topic, reason=str(e))
            await c.post(f"{consumer_url}/subscription", json={"topics": [topic]},
                         headers=headers, auth=self._rest_proxy_auth)

    async def _fetch_records(self, c, consumer_url: str, max_messages: int) -> list[dict]:
        """Up to 3 polls (the first poll of a new consumer is often empty),
        each body read as a capped stream."""
        hard_cap = self._preview_max_total_bytes * 4 + 65_536
        records: list[dict] = []
        for _ in range(3):
            batch = await self._get_json_capped(
                c, f"{consumer_url}/records",
                params={"timeout": 1000, "max_bytes": self._preview_max_total_bytes},
                headers={"Accept": "application/vnd.kafka.json.v2+json"},
                cap=hard_cap,
            )
            records.extend(batch or [])
            if len(records) >= max_messages or (records and not batch):
                break
        return records

    async def _get_json_capped(self, c, url: str, *, cap: int, **kw) -> Any:
        async with c.stream("GET", url, auth=self._rest_proxy_auth, **kw) as resp:
            if resp.status_code != 200:
                return []
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > cap:
                    raise PreviewTooLarge(f"REST Proxy response exceeded {cap} bytes")
        return json.loads(bytes(buf) or b"[]")

    def _cap_records(self, raw: list[dict], max_messages: int) -> dict:
        out: list[dict] = []
        total = 0
        truncated = False
        for r in raw[:max_messages]:
            rec = self._capped_record(r)
            size = len(json.dumps(rec, default=str).encode("utf-8"))
            if total + size > self._preview_max_total_bytes:
                truncated = True
                break
            out.append(rec)
            total += size
        if len(raw) > max_messages:
            truncated = True
        return {"messages": out, "truncated": truncated, "bytes": total}

    def _capped_record(self, r: dict) -> dict:
        value = r.get("value", {})
        serialized = value if isinstance(value, str) else json.dumps(value, default=str)
        size = len(serialized.encode("utf-8"))
        if size > self._preview_max_bytes:
            value = {"_truncated": True, "_original_size_bytes": size}
        key = r.get("key")
        if key is not None:
            key = key if isinstance(key, str) else json.dumps(key, default=str)
            kb = key.encode("utf-8")
            if len(kb) > self._preview_max_key_bytes:
                key = kb[: self._preview_max_key_bytes].decode("utf-8", "ignore") + "…[truncated]"
        return {
            "partition": r.get("partition", 0),
            "offset": r.get("offset", 0),
            "key": key if key else None,
            "value": value,
            "timestamp_ms": r.get("timestamp") or 0,
        }

    # ── Consumer-group lag ───────────────────────────────────────────────

    async def get_consumer_group_lag(self, group_id: str) -> dict[str, Any]:
        """{"total_lag": int, "per_partition": {topic: {partition: lag}}},
        or {} when unavailable — callers record {} as UNKNOWN (NULL), never 0."""
        if not self._rest_url:
            return {}
        try:
            async with self._rest_client() as c:
                cluster_id = await self._cluster_id(c)
                if not cluster_id:
                    return {}
                resp = await c.get(
                    f"{self._rest_url}/v3/clusters/{cluster_id}/consumer-groups/{group_id}/lags",
                    auth=self._rest_proxy_auth,
                )
                if resp.status_code != 200:
                    return {}
                per_partition: dict[str, dict[str, int]] = {}
                total = 0
                for e in resp.json().get("data", []):
                    lag = int(e.get("lag") or 0)
                    per_partition.setdefault(e["topic_name"], {})[str(e["partition_id"])] = lag
                    total += lag
                if not per_partition:
                    return {}   # group exists but has no committed offsets yet — unknown
                return {"total_lag": total, "per_partition": per_partition}
        except Exception as e:  # noqa: BLE001
            log.warning("kafka_consumer_lag_unavailable", group_id=group_id, error=str(e))
            return {}


_kafka_client: KafkaAdminClient | None = None


def get_kafka_client() -> KafkaAdminClient:
    global _kafka_client
    if _kafka_client is None:
        _kafka_client = KafkaAdminClient()
    return _kafka_client


# ═════════════════════════════════════════════════════════════════════════
# Governance: org-scoped topic patterns + UCS policy (RUN-FLINK-016)
# ═════════════════════════════════════════════════════════════════════════

def topic_matches_org_patterns(topic: str, org_id: str, settings: Settings | None = None) -> bool | None:
    """True/False against KAFKA_TOPIC_ALLOWED_PATTERNS ("{org_id}"
    substituted); None when no patterns are configured."""
    s = settings or get_settings()
    patterns = [p.replace("{org_id}", org_id) for p in (s.kafka_topic_allowed_patterns or [])]
    if not patterns:
        return None
    return any(fnmatch.fnmatchcase(topic, p) for p in patterns)


def _ucs_configured(s: Settings) -> bool:
    return bool(s.ucs_url and s.ucs_internal_token)


def _ucs_token(s: Settings, org_id: str) -> str:
    from app.core.service_auth import mint_service_token
    return mint_service_token(signing_key=s.ucs_internal_token, issuer="flinksql-runner",
                              audience="ucs", scope="ucs.kafka.preview.check", org_id=org_id)


async def check_preview_policy(org_id: str, user_id: str, topic: str,
                               action: str = "preview") -> tuple[bool, str | None]:
    """Ask UCS whether `topic` may be accessed (`action`: preview|schema).

    Outside dev this FAILS CLOSED both when UCS is not configured at all
    and when the check errors/times out; in dev an unconfigured or
    unreachable UCS allows (local stacks)."""
    settings = get_settings()
    if not _ucs_configured(settings):
        if settings.environment == "dev":
            return True, None
        return False, "UCS preview policy is not configured for this data plane"
    try:
        async with httpx.AsyncClient(timeout=settings.kafka_admin_timeout_seconds) as c:
            resp = await c.post(
                f"{settings.ucs_url}/api/v1/kafka/preview-policy",
                json={"org_id": org_id, "user_id": user_id, "topic": topic, "action": action},
                headers={"Authorization": f"Bearer {_ucs_token(settings, org_id)}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return bool(data.get("allowed", False)), data.get("reason")
        log.warning("kafka_preview_policy_check_failed", topic=topic, status=resp.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("kafka_preview_policy_unreachable", topic=topic, error=str(e))
    return settings.environment == "dev", "UCS preview-policy check unavailable"


async def check_topic_access(org_id: str, user_id: str, topic: str,
                             action: str) -> tuple[bool, str | None]:
    """Org pattern scoping first, then the UCS policy. For `schema`
    outside dev, UCS OR configured org patterns must vouch for the topic;
    `preview` always requires UCS outside dev."""
    settings = get_settings()
    pattern_ok = topic_matches_org_patterns(topic, org_id, settings)
    if pattern_ok is False:
        return False, "topic is outside this org's allowed topic patterns"
    if action == "schema" and not _ucs_configured(settings):
        if settings.environment == "dev" or pattern_ok:
            return True, None
        return False, "no UCS policy or org topic patterns configured for this data plane"
    return await check_preview_policy(org_id, user_id, topic, action)


async def filter_topics(org_id: str, user_id: str, topics: list[dict]) -> tuple[list[dict], str | None]:
    """Topics this org/user may see. Org patterns, then UCS's batch policy
    (POST /api/v1/kafka/preview-policy/batch -> {"allowed": [names]}).
    Outside dev with neither configured -> nothing (fail closed)."""
    settings = get_settings()
    patterned = [t for t in topics if topic_matches_org_patterns(t["name"], org_id, settings) is not False]
    have_patterns = bool(settings.kafka_topic_allowed_patterns)
    if not _ucs_configured(settings):
        if settings.environment == "dev" or have_patterns:
            return patterned, None
        return [], "no UCS policy or org topic patterns configured for this data plane"
    names = [t["name"] for t in patterned]
    if not names:
        return [], None
    try:
        async with httpx.AsyncClient(timeout=settings.kafka_admin_timeout_seconds) as c:
            resp = await c.post(
                f"{settings.ucs_url}/api/v1/kafka/preview-policy/batch",
                json={"org_id": org_id, "user_id": user_id, "topics": names, "action": "list"},
                headers={"Authorization": f"Bearer {_ucs_token(settings, org_id)}"},
            )
        if resp.status_code == 200:
            allowed = set(resp.json().get("allowed") or [])
            return [t for t in patterned if t["name"] in allowed], None
        log.warning("kafka_topic_policy_batch_failed", status=resp.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("kafka_topic_policy_batch_unreachable", error=str(e))
    if settings.environment == "dev":
        return patterned, None
    return [], "UCS topic policy check unavailable"
