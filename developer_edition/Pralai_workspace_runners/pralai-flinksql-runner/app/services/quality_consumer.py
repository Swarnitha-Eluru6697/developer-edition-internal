"""Quality result consumer (shared DQ quality contract v2).

Reads the topics defined by the shared contract
(app/contracts/quality_contract.json, byte-identical in inaiera-data-quality,
injected via QUALITY_RESULTS_TOPIC / QUALITY_GATE_TOPIC):

  window_result  one aggregated row per (binding, check, processing-time
                 window) written by the DQ-rendered FlinkSQL quality job —
                 evaluated_rows, violation_count, threshold, breach_status,
                 bounded sample_values, run_ref (= deployment id)
  gate_event     blocking bindings only: a breaching window, action
                 'quarantine' or (explicit per-binding opt-in) 'fail_stop'

At-least-once, never lossy (DQ-RESULT-009)
──────────────────────────────────────────
poll -> validate against the contract -> durable write to
quality_result_outbox (same transaction: heartbeat upsert + 'started'
event) -> ONLY THEN commit the Kafka offset (enable_auto_commit=False).
A record that violates the contract (unknown major version, missing field,
foreign org, wrong topic) is stored as a `dead_letter` row with status
'rejected' before the offset moves — never silently dropped.

Idempotency
───────────
Outbox keys are SEMANTIC, derived from the contract's aggregation key
(org_id, ucs_fqn, check_id, window_start, window_end, run_ref) + event kind,
so a Flink replay of the same window (at-least-once sink) or a re-poll of
the same message collapses onto one row; the key is sent to UCS as the
Idempotency-Key on every attempt.

Delivery never holds the database (FL-004)
──────────────────────────────────────────
A drain CLAIMS a batch in one short transaction (FOR UPDATE SKIP LOCKED, then
pending -> processing with lease_owner / lease_expires_at, commit), delivers
each row to UCS (and runs any fail_stop) with NO transaction or row lock open,
and finalizes each row in a second short transaction that only applies if the
worker still owns the lease. A slow UCS therefore costs time, never pooled
connections or locks; a worker that dies mid-delivery leaves a 'processing'
row whose lease lapses and any replica claims it again (the same idempotency
key makes the repeat safe).

A failing event cannot spin (FL-005)
────────────────────────────────────
A failed delivery is retried after base x 2^(attempts-1) seconds (capped),
persisted in next_attempt_at so a restart does not reset it; the claim only
takes rows that are due, so a poison event or a UCS outage does not consume a
slot at the head of every flush. After QUALITY_OUTBOX_MAX_ATTEMPTS failed
deliveries the row is PARKED: kept with its last error, never retried until an
operator requeues it (QUALITY_OUTBOX.md).

QualityRun aggregation + lifecycle (DQ-RESULT-010/011)
─────────────────────────────────────────────────────
One QualityRun per (org, dataset, check, window, run_ref) with
evaluated_rows, violation_count, violation_rate, threshold, status and at
most contract.max_samples samples (each truncated). Status:
  started         first window seen for (org, dataset, check, run_ref)
  completed_pass  violation_rate <= threshold
  completed_fail  rate > threshold and breach_status 'fail' (ERROR/CRITICAL)
  completed_warn  rate > threshold and breach_status 'warn'
  unknown         evaluated_rows == 0 (nothing evaluated, no verdict)
  stale           no window for max(QUALITY_CHECK_STALE_AFTER_SECONDS,
                  stale_after_windows x window) — heartbeat lost
Delivery: UCS contract-v2 POST /api/v1/quality/runs (+ /gate-events) when
UCS_QUALITY_RUNS_ENABLED; otherwise (LEGACY mode, reported as DEGRADED by
/readyz) completed runs are mapped onto the legacy POST /api/v1/quality/results
and lifecycle/gate events are kept as 'skipped' — retained, and re-queued for
delivery as soon as v2 is enabled (FL-007).

Tombstones (FL-006): a null-valued record is classified explicitly (outbox row
kind dead_letter, status 'skipped', classification 'tombstone') before the
offset is committed, never silently passed over.

Gate fail-stop: a gate_event with action 'fail_stop' (only rendered when the
binding opted in) stops the parent job via JobService.stop (idempotency
key derived from the event) unless QUALITY_GATE_FAIL_STOP_ENABLED=false.

Lifecycle of the task itself (RUN-FLINK-010): runs only when
QUALITY_CONSUMER_ENABLED=true; misconfiguration (no UCS URL/token, or a
configured contract version this build doesn't implement) = 'misconfigured'
(readiness 503); Kafka failures retried with exponential backoff forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
from kafka import KafkaConsumer
from sqlalchemy import and_, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import quality_contract as qc
from app.core.config import get_settings
from app.core.service_auth import mint_service_token
from app.db.session import AsyncSessionLocal
from app.models import QualityCheckHeartbeat, QualityResultOutbox

log = structlog.get_logger()

CONSUMER_STATE: dict[str, Any] = {
    "status": "not_started",     # not_started|disabled|misconfigured|connecting|running|backoff|stopped
    "last_error": None,
    "last_poll_at": None,
    "kafka_connected": False,
    "ucs_reachable": None,
    "consecutive_ucs_failures": 0,
}

LEGACY_SKIP_REASON = ("lifecycle/gate events need UCS contract v2 (UCS_QUALITY_RUNS_ENABLED); not deliverable to /quality/results — "
                      "retained, and re-queued when v2 delivery is enabled")
KIND_RUN = "quality_run"
KIND_GATE = "gate_event"
KIND_DEAD = "dead_letter"
RUNS_PATH = "/api/v1/quality/runs"
GATE_EVENTS_PATH = "/api/v1/quality/gate-events"
LEGACY_RESULTS_PATH = "/api/v1/quality/results"
SCOPE_RUNS = "ucs.quality.runs.write"
SCOPE_LEGACY = "ucs.quality.checks.write"


def delivery_mode(settings) -> dict:
    """FL-007: which UCS contract quality events are delivered with. 'legacy' (completed runs only, via /quality/results) is DEGRADED:
    lifecycle (started/stale/unknown) and gate events are kept in the outbox but not delivered to UCS."""
    if settings.ucs_quality_runs_enabled:
        return {"mode": "v2", "degraded": False}
    return {"mode": "legacy", "degraded": True,
            "detail": "UCS receives completed runs only; lifecycle and gate events are retained, not delivered "
                      "(set UCS_QUALITY_RUNS_ENABLED=true once UCS serves the QualityRun contract)"}


async def legacy_backlog_count() -> int | None:
    """How many lifecycle/gate events are waiting because of legacy mode (None if the database cannot be asked)."""
    try:
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(func.count()).select_from(QualityResultOutbox)
                .where(QualityResultOutbox.status == "skipped", QualityResultOutbox.kind.in_((KIND_RUN, KIND_GATE)),
                       QualityResultOutbox.last_error == LEGACY_SKIP_REASON))).scalar_one()
    except Exception:  # noqa: BLE001
        return None


async def requeue_legacy_skipped() -> int:
    """When v2 delivery is on, give the events legacy mode set aside their delivery (idempotency keys make a repeat safe)."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            update(QualityResultOutbox)
            .where(QualityResultOutbox.status == "skipped", QualityResultOutbox.kind.in_((KIND_RUN, KIND_GATE)),
                   QualityResultOutbox.last_error == LEGACY_SKIP_REASON)
            .values(status="pending", attempts=0, last_error=None, next_attempt_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False))
        await db.commit()
    return int(getattr(res, "rowcount", 0) or 0)


def _set_state(**kw) -> None:
    CONSUMER_STATE.update(kw)


def reset_state() -> None:
    global _LEGACY_REQUEUED
    _LEGACY_REQUEUED = False
    CONSUMER_STATE.update(status="not_started", last_error=None, last_poll_at=None,
                          kafka_connected=False, ucs_reachable=None, consecutive_ucs_failures=0)


def _config_problem(settings) -> str | None:
    if not settings.ucs_url or not settings.ucs_internal_token:
        return "QUALITY_CONSUMER_ENABLED requires UCS_URL and UCS_INTERNAL_TOKEN"
    if not qc.is_compatible_version(settings.quality_contract_version):
        return (f"QUALITY_CONTRACT_VERSION={settings.quality_contract_version!r} is not "
                f"implemented by this build (contract {qc.CONTRACT_VERSION})")
    return None


def consumer_health(settings=None) -> tuple[bool, dict]:
    settings = settings or get_settings()
    if not settings.quality_consumer_enabled:
        return True, {"status": "disabled"}
    problem = _config_problem(settings)
    if problem:
        return False, {"status": "misconfigured", "error": problem}
    st = dict(CONSUMER_STATE)
    detail = {"status": st["status"], "kafka_connected": st["kafka_connected"],
              "ucs_reachable": st["ucs_reachable"], "last_error": st["last_error"],
              "contract_version": qc.CONTRACT_VERSION}
    if st["status"] != "running" or not st["kafka_connected"]:
        return False, detail
    last = st["last_poll_at"]
    if last is None or time.monotonic() - last > settings.quality_consumer_stale_after_seconds:
        detail["error"] = "consumer has not completed a poll recently"
        return False, detail
    if st["ucs_reachable"] is False:
        return False, detail
    return True, detail


def consumed_topics(settings) -> list[str]:
    return [settings.quality_results_topic, settings.quality_gate_topic]


def _make_consumer(settings) -> KafkaConsumer:
    if not settings.kafka_bootstrap_servers:
        raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS is empty")
    consumer_config: dict[str, Any] = {
        "bootstrap_servers": settings.kafka_bootstrap_servers,
        "group_id": settings.quality_consumer_group_id,
        "auto_offset_reset": "earliest",
        # DQ-RESULT-009: offsets committed explicitly, only after the outbox write.
        "enable_auto_commit": False,
        # Raw bytes: decoding happens per record so a malformed one is
        # dead-lettered instead of crashing the poll.
        "consumer_timeout_ms": int(settings.quality_consumer_flush_interval_seconds * 1000),
    }
    protocol = settings.kafka_security_protocol
    if protocol != "PLAINTEXT":
        consumer_config["security_protocol"] = protocol
        if protocol in ("SASL_SSL", "SASL_PLAINTEXT"):
            if not settings.kafka_sasl_username or not settings.kafka_sasl_password:
                raise RuntimeError(
                    f"KAFKA_SECURITY_PROTOCOL={protocol} but KAFKA_SASL_USERNAME/KAFKA_SASL_PASSWORD "
                    "are not set")
            consumer_config.update({
                "sasl_mechanism": settings.kafka_sasl_mechanism,
                "sasl_plain_username": settings.kafka_sasl_username,
                "sasl_plain_password": settings.kafka_sasl_password,
            })
        if protocol in ("SSL", "SASL_SSL") and settings.kafka_ssl_cafile:
            consumer_config["ssl_cafile"] = settings.kafka_ssl_cafile
    topics = consumed_topics(settings)
    consumer = KafkaConsumer(*topics, **consumer_config)
    log.info("quality_consumer.connected", topics=topics, group=settings.quality_consumer_group_id)
    return consumer


# ── record -> outbox entries ─────────────────────────────────────────────

@dataclass
class Entry:
    key: str
    kind: str
    payload: dict
    status: str = "pending"
    error: str | None = None
    heartbeat: dict | None = field(default=None)


def _key(prefix: str, *parts: Any) -> str:
    raw = json.dumps([str(p) for p in parts], separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(raw.encode()).hexdigest()}"


def _aggregation_key(r: dict) -> list:
    return [r.get(k) for k in qc.AGGREGATION_KEY]


def _samples(values: Any) -> list[str]:
    out: list[str] = []
    for v in values or []:
        if v is None:
            continue
        s = str(v)[:qc.MAX_SAMPLE_CHARS]
        if s not in out:
            out.append(s)
        if len(out) >= qc.MAX_SAMPLES:
            break
    return out


def run_status(evaluated: int, violations: int, threshold: float, breach_status: str) -> str:
    if evaluated <= 0:
        return "unknown"
    if violations / evaluated <= threshold:
        return "completed_pass"
    return "completed_fail" if breach_status == "fail" else "completed_warn"


def quality_run_payload(r: dict, status: str, key: str) -> dict:
    evaluated, violations = int(r["evaluated_rows"]), int(r["violation_count"])
    return {
        "contract": "inaiera.dq.quality-run", "contract_version": qc.CONTRACT_VERSION,
        "org_id": r["org_id"], "dataset_fqn": r["ucs_fqn"], "check_id": r["check_id"],
        "rule_id": r["rule_id"], "rule_name": r["rule_name"], "rule_version": r["rule_version"],
        "dimension": r["dimension"], "severity": r["severity"],
        "binding_id": r["binding_id"], "artifact_version_id": r["artifact_version_id"],
        "enforcement_mode": r["enforcement_mode"], "column_name": r.get("column_name"),
        "run_ref": r["run_ref"], "parent_job_id": r.get("parent_job_id"),
        "window_start": r["window_start"], "window_end": r["window_end"],
        "window_seconds": r["window_seconds"],
        "status": status, "evaluated_rows": evaluated, "violation_count": violations,
        "violation_rate": (violations / evaluated) if evaluated else None,
        "threshold": float(r["threshold"]), "breach_status": r["breach_status"],
        "samples": _samples(r.get("sample_values")),
        "engine": "flink", "observed_at": r["emitted_at"],
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": key,
    }


def tombstone_entry(topic: str, partition: int, offset: int) -> Entry:
    """FL-006: a null-valued record (a Kafka tombstone) carries nothing to evaluate, and the contract defines no meaning for it on these
    topics. It is NOT silently skipped: it becomes an explicit, auditable 'ignored' row (kind dead_letter, status 'skipped',
    classification 'tombstone') so that the offset it occupies is only ever committed after it has been durably classified, exactly
    like every other record."""
    return Entry(key=f"tombstone:{topic}-{partition}-{offset}", kind=KIND_DEAD,
                 payload={"topic": topic, "partition": partition, "offset": offset,
                          "classification": "tombstone", "record": None},
                 status="skipped", error="tombstone (null value): ignored by contract")


def process_record(topic: str, partition: int, offset: int, raw: Any, settings) -> Entry:
    """One Kafka message -> one outbox entry (QualityRun, gate event or dead letter)."""
    record: Any = raw
    errors: list[str] = []
    if isinstance(raw, (bytes, bytearray)):
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            record, errors = {"raw": raw.decode("utf-8", "replace")[:2000]}, [f"not JSON: {e}"]
    if not errors:
        errors = qc.validate_record(record)
    if not errors:
        if record.get("org_id") != settings.org_id:
            errors.append(f"org_id {record.get('org_id')!r} does not belong to this data plane")
        expected = {settings.quality_results_topic: "window_result",
                    settings.quality_gate_topic: "gate_event"}.get(topic)
        if expected and record.get("record_type") != expected:
            errors.append(f"record_type {record.get('record_type')!r} on topic {topic!r}")
    if errors:
        return Entry(key=f"dead-letter:{topic}-{partition}-{offset}", kind=KIND_DEAD,
                     payload={"topic": topic, "partition": partition, "offset": offset,
                              "record": record, "errors": errors},
                     status="rejected", error="; ".join(errors)[:1000])

    if record["record_type"] == "gate_event":
        key = _key("gate", *_aggregation_key(record), record["action"])
        payload = quality_run_payload(record, run_status(
            int(record["evaluated_rows"]), int(record["violation_count"]),
            float(record["threshold"]), record["breach_status"]), key)
        payload.update(contract="inaiera.dq.gate-event", gate=True, action=record["action"])
        return Entry(key=key, kind=KIND_GATE, payload=payload)

    status = run_status(int(record["evaluated_rows"]), int(record["violation_count"]),
                        float(record["threshold"]), record["breach_status"])
    key = _key("qrun", *_aggregation_key(record), "completed")
    hb = {"org_id": record["org_id"], "ucs_fqn": record["ucs_fqn"],
          "check_id": record["check_id"], "run_ref": record["run_ref"],
          "binding_id": record["binding_id"], "rule_name": record["rule_name"],
          "window_seconds": int(record["window_seconds"]),
          "last_window_end": record["window_end"], "record": record}
    return Entry(key=key, kind=KIND_RUN, payload=quality_run_payload(record, status, key),
                 heartbeat=hb)


def _lifecycle_payload(base: dict, status: str, key: str) -> dict:
    return {
        "contract": "inaiera.dq.quality-run", "contract_version": qc.CONTRACT_VERSION,
        "org_id": base["org_id"], "dataset_fqn": base["ucs_fqn"], "check_id": base["check_id"],
        "rule_name": base.get("rule_name"), "binding_id": base.get("binding_id"),
        "run_ref": base["run_ref"], "status": status, "window_seconds": base["window_seconds"],
        "last_window_end": base.get("last_window_end"), "engine": "flink",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(), "idempotency_key": key,
    }


async def _write_outbox(entries: list[Entry]) -> None:
    """Durably persist every polled record (and lifecycle 'started' events)
    BEFORE the caller commits the Kafka offset. Idempotent on the key."""
    if not entries:
        return
    async with AsyncSessionLocal() as db:
        extra: list[Entry] = []
        now = datetime.now(timezone.utc)
        for e in entries:
            hb = e.heartbeat
            if not hb:
                continue
            res = await db.execute(
                pg_insert(QualityCheckHeartbeat)
                .values(org_id=hb["org_id"], ucs_fqn=hb["ucs_fqn"], check_id=hb["check_id"],
                        run_ref=hb["run_ref"], binding_id=hb["binding_id"],
                        rule_name=hb["rule_name"], window_seconds=hb["window_seconds"],
                        first_seen_at=now, last_seen_at=now,
                        last_window_end=hb["last_window_end"], state="active")
                .on_conflict_do_update(
                    index_elements=["org_id", "ucs_fqn", "check_id", "run_ref"],
                    set_={"last_seen_at": now, "last_window_end": hb["last_window_end"],
                          "window_seconds": hb["window_seconds"], "state": "active"})
                .returning(literal_column("(xmax = 0)").label("inserted")))
            if res.scalar():
                key = _key("qrun-started", hb["org_id"], hb["ucs_fqn"], hb["check_id"], hb["run_ref"])
                p = _lifecycle_payload(hb, "started", key)
                rec = hb["record"]
                p.update(rule_id=rec["rule_id"], rule_version=rec["rule_version"],
                         artifact_version_id=rec["artifact_version_id"],
                         started_at=rec["window_start"], threshold=float(rec["threshold"]))
                extra.append(Entry(key=key, kind=KIND_RUN, payload=p))
        rows = [{"idempotency_key": e.key, "kind": e.kind, "payload": e.payload,
                 "status": e.status, "last_error": e.error} for e in [*extra, *entries]]
        await db.execute(pg_insert(QualityResultOutbox).values(rows)
                         .on_conflict_do_nothing(index_elements=["idempotency_key"]))
        await db.commit()


async def _sweep_stale(settings) -> int:
    """Heartbeat lost -> one 'stale' QualityRun per (check, run_ref, last window)."""
    now = datetime.now(timezone.utc)
    floor = timedelta(seconds=settings.quality_check_stale_after_seconds)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(QualityCheckHeartbeat)
            .where(QualityCheckHeartbeat.state == "active",
                   QualityCheckHeartbeat.last_seen_at < now - floor)
            .with_for_update(skip_locked=True))).scalars().all()
        entries = []
        for hb in rows:
            threshold = max(floor, timedelta(seconds=hb.window_seconds * qc.STALE_AFTER_WINDOWS))
            if hb.last_seen_at >= now - threshold:
                continue
            hb.state = "stale"
            base = {"org_id": hb.org_id, "ucs_fqn": hb.ucs_fqn, "check_id": hb.check_id,
                    "run_ref": hb.run_ref, "binding_id": hb.binding_id, "rule_name": hb.rule_name,
                    "window_seconds": hb.window_seconds, "last_window_end": hb.last_window_end}
            key = _key("qrun-stale", hb.org_id, hb.ucs_fqn, hb.check_id, hb.run_ref,
                       hb.last_window_end)
            p = _lifecycle_payload(base, "stale", key)
            p["last_seen_at"] = hb.last_seen_at.isoformat()
            entries.append({"idempotency_key": key, "kind": KIND_RUN, "payload": p,
                            "status": "pending"})
        if entries:
            await db.execute(pg_insert(QualityResultOutbox).values(entries)
                             .on_conflict_do_nothing(index_elements=["idempotency_key"]))
        await db.commit()
    return len(entries)


# ── delivery ────────────────────────────────────────────────────────────

def legacy_result(p: dict) -> dict | None:
    """Map a completed QualityRun onto UCS's legacy POST /quality/results."""
    status = {"completed_pass": "pass", "completed_fail": "fail",
              "completed_warn": "warn"}.get(p.get("status"))
    if status is None:
        return None
    rate = p.get("violation_rate")
    return {
        "fqn": p["dataset_fqn"], "name": p["rule_name"], "dimension": p["dimension"],
        "status": status, "observed": rate, "threshold": p.get("threshold"),
        "rows_scanned": p.get("evaluated_rows"),
        "pass_rate": round((1 - rate) * 100, 4) if rate is not None else None,
        "engine": "flink", "run_ref": p.get("run_ref", ""),
        "detail": (f"{p.get('violation_count')}/{p.get('evaluated_rows')} rows violated "
                   f"({rate:.1%}) in window {p.get('window_start')}..{p.get('window_end')}"
                   if rate is not None else "no rows evaluated"),
    }


async def _post_to_ucs(client: httpx.AsyncClient, settings, payload: dict, retries: int = 3, *,
                       idempotency_key: str | None = None, path: str = LEGACY_RESULTS_PATH,
                       scope: str = SCOPE_LEGACY) -> int | None:
    """POST with retries. Returns the final HTTP status, or None if unreachable."""
    token = mint_service_token(
        signing_key=settings.ucs_internal_token or "", issuer="flinksql-runner",
        audience="ucs", scope=scope, org_id=settings.org_id)
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    url = f"{settings.ucs_url.rstrip('/')}{path}"
    status: int | None = None
    for attempt in range(retries):
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=10.0)
            status = resp.status_code
            if 200 <= status < 300 or status == 409:
                _set_state(ucs_reachable=True, consecutive_ucs_failures=0)
                return status
            log.warning("quality_consumer.ucs_error", status=status, body=resp.text[:200])
            if 400 <= status < 500 and status not in (408, 429):
                _set_state(ucs_reachable=True)
                return status
        except httpx.HTTPError as e:
            log.warning("quality_consumer.http_error", error=str(e), attempt=attempt)
        if attempt < retries - 1:
            await asyncio.sleep(settings.quality_consumer_retry_backoff_seconds)
    _set_state(ucs_reachable=False,
               consecutive_ucs_failures=CONSUMER_STATE.get("consecutive_ucs_failures", 0) + 1)
    return status


async def _fail_stop(payload: dict, key: str) -> dict:
    """Stop the parent job for an opted-in fail_stop gate event."""
    from app.schemas import StopRequest
    from app.services.job_service import JobService
    try:
        job_id = uuid.UUID(str(payload.get("parent_job_id")))
    except ValueError:
        return {"status": "failed", "error": "gate event has no valid parent_job_id"}
    async with AsyncSessionLocal() as db:
        try:
            dep = await JobService(db).stop(
                payload["org_id"], job_id,
                StopRequest(drain=False, idempotency_key=f"dq-gate-{key.split(':')[-1][:40]}"))
            await db.commit()
            return {"status": "stopped", "deployment_id": str(getattr(dep, "id", ""))}
        except Exception as e:  # noqa: BLE001 — recorded on the event, never raised
            await db.rollback()
            return {"status": "failed", "error": str(e)[:300]}


async def _deliver(client: httpx.AsyncClient, settings, row: _Claimed) -> tuple[str, str | None]:
    """-> (new_status, error). new_status in delivered|pending|rejected|skipped."""
    p = row.payload
    if settings.ucs_quality_runs_enabled:
        path = GATE_EVENTS_PATH if row.kind == KIND_GATE else RUNS_PATH
        status = await _post_to_ucs(client, settings, p, idempotency_key=row.idempotency_key,
                                    path=path, scope=SCOPE_RUNS)
    else:
        legacy = legacy_result(p) if row.kind == KIND_RUN else None
        if legacy is None:
            return "skipped", LEGACY_SKIP_REASON
        status = await _post_to_ucs(client, settings, legacy,
                                    idempotency_key=row.idempotency_key)
    if status is not None and (200 <= status < 300 or status == 409):
        return "delivered", None
    if status is not None and 400 <= status < 500 and status not in (408, 429):
        return "rejected", f"UCS rejected the event (HTTP {status})"
    return "pending", "UCS delivery failed after retries"


def retry_delay_seconds(attempts: int, settings) -> float:
    """Backoff before the next try after the `attempts`-th failed delivery (FL-005)."""
    return min(settings.quality_outbox_retry_base_seconds * 2 ** max(0, attempts - 1),
               settings.quality_outbox_retry_max_seconds)


@dataclass
class _Claimed:
    """A claimed outbox row, detached from any session: delivery needs only these."""
    id: uuid.UUID
    idempotency_key: str
    kind: str
    payload: dict
    attempts: int


async def _claim_batch(settings, owner: str) -> list[_Claimed]:
    """FL-004: one SHORT transaction. Take the due rows (pending whose backoff
    passed, or processing whose worker's lease lapsed), mark them processing
    with this worker's lease, commit — then the caller delivers with nothing
    open."""
    now = datetime.now(timezone.utc)
    lease = timedelta(seconds=settings.quality_outbox_lease_seconds)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(QualityResultOutbox)
            .where(or_(and_(QualityResultOutbox.status == "pending",
                            QualityResultOutbox.next_attempt_at <= now),
                       and_(QualityResultOutbox.status == "processing",
                            QualityResultOutbox.lease_expires_at < now)))
            .order_by(QualityResultOutbox.created_at)
            .limit(settings.quality_consumer_batch_size)
            .with_for_update(skip_locked=True))).scalars().all()
        for row in rows:
            row.status = "processing"
            row.lease_owner = owner
            row.lease_expires_at = now + lease
        claimed = [_Claimed(r.id, r.idempotency_key, r.kind, r.payload, r.attempts) for r in rows]
        await db.commit()
    return claimed


async def _extend_lease(ids: list[uuid.UUID], settings, owner: str) -> None:
    """Keep the rest of the batch ours while earlier rows are being delivered."""
    if not ids:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(QualityResultOutbox)
            .where(QualityResultOutbox.id.in_(ids), QualityResultOutbox.status == "processing",
                   QualityResultOutbox.lease_owner == owner)
            .values(lease_expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=settings.quality_outbox_lease_seconds)))
        await db.commit()


async def _release(ids: list[uuid.UUID], owner: str) -> None:
    """Hand claimed-but-untouched rows back, without counting an attempt."""
    if not ids:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(QualityResultOutbox)
            .where(QualityResultOutbox.id.in_(ids), QualityResultOutbox.status == "processing",
                   QualityResultOutbox.lease_owner == owner)
            .values(status="pending", lease_owner=None, lease_expires_at=None))
        await db.commit()


async def _finalize(row: _Claimed, owner: str, new_status: str, error: str | None, payload: dict,
                    settings) -> bool:
    """FL-004/FL-005: the second short transaction. Applies only while this
    worker still owns the row (a lapsed lease may have been re-claimed, and
    that worker's outcome must stand). A failed delivery counts an attempt,
    schedules the retry with backoff — or parks the row at the maximum."""
    values: dict = {"status": new_status, "last_error": error, "payload": payload,
                    "lease_owner": None, "lease_expires_at": None}
    now = datetime.now(timezone.utc)
    if new_status == "delivered":
        values["delivered_at"] = now
    elif new_status == "pending":
        attempts = row.attempts + 1
        values["attempts"] = attempts
        if attempts >= settings.quality_outbox_max_attempts:
            values["status"] = "parked"
            values["last_error"] = f"parked after {attempts} attempts: {error}"
            log.error("quality_outbox.parked", key=row.idempotency_key, kind=row.kind,
                      attempts=attempts, error=error)
        else:
            values["next_attempt_at"] = now + timedelta(seconds=retry_delay_seconds(attempts, settings))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(QualityResultOutbox)
            .where(QualityResultOutbox.id == row.id, QualityResultOutbox.status == "processing",
                   QualityResultOutbox.lease_owner == owner)
            .values(**values).returning(QualityResultOutbox.id))
        owned = result.first() is not None
        await db.commit()
    if not owned:
        log.warning("quality_outbox.claim_lost", key=row.idempotency_key)
    return owned


_LEGACY_REQUEUED = False


async def _drain_outbox(http_client: httpx.AsyncClient, settings) -> tuple[int, int]:
    global _LEGACY_REQUEUED
    if settings.ucs_quality_runs_enabled and not _LEGACY_REQUEUED:
        n = await requeue_legacy_skipped()
        _LEGACY_REQUEUED = True
        if n:
            log.info("quality_outbox.legacy_events_requeued", count=n)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    claimed = await _claim_batch(settings, owner)
    pending_ids = [r.id for r in claimed]           # claimed and not yet finalized or released
    sent = errors = 0
    try:
        for i, row in enumerate(claimed):
            if i:
                await _extend_lease(pending_ids, settings, owner)
            payload = row.payload
            try:
                if row.kind == KIND_GATE and payload.get("action") == "fail_stop" \
                        and "fail_stop" not in payload:
                    result = (await _fail_stop(payload, row.idempotency_key)
                              if settings.quality_gate_fail_stop_enabled
                              else {"status": "disabled", "reason": "QUALITY_GATE_FAIL_STOP_ENABLED=false"})
                    payload = {**payload, "fail_stop": result}
                    log.warning("quality_gate.fail_stop", result=result, job=payload.get("parent_job_id"))
                new_status, error = await _deliver(http_client, settings, row)
            except Exception as e:  # noqa: BLE001 — a row that cannot even be built counts as a failed attempt
                log.warning("quality_outbox.delivery_error", key=row.idempotency_key, error=str(e))
                new_status, error = "pending", f"delivery error: {type(e).__name__}: {e}"[:500]
            await _finalize(row, owner, new_status, error, payload, settings)
            pending_ids.remove(row.id)
            if new_status == "delivered":
                sent += 1
            elif new_status == "pending":
                errors += 1
                if CONSUMER_STATE.get("ucs_reachable") is False:
                    break                            # UCS is down: stop hammering it; the rest go back untouched
    finally:
        with contextlib.suppress(Exception):
            await _release(pending_ids, owner)
    return sent, errors


# ── loop ────────────────────────────────────────────────────────────────

async def quality_consumer_loop() -> None:
    settings = get_settings()
    if not settings.quality_consumer_enabled:
        _set_state(status="disabled")
        log.info("quality_consumer.disabled", reason="QUALITY_CONSUMER_ENABLED is false")
        return
    problem = _config_problem(settings)
    if problem:
        _set_state(status="misconfigured", last_error=problem)
        log.error("quality_consumer.misconfigured", reason=problem)
        return

    loop = asyncio.get_running_loop()
    backoff = max(0.01, settings.quality_consumer_retry_backoff_seconds)
    async with httpx.AsyncClient() as http_client:
        try:
            while True:
                consumer = None
                try:
                    _set_state(status="connecting")
                    if not settings.ucs_quality_runs_enabled:
                        log.warning("quality_consumer.legacy_ucs_mode_degraded", detail=delivery_mode(settings)["detail"])
                    consumer = await loop.run_in_executor(None, lambda: _make_consumer(settings))
                    if consumer is None:
                        raise RuntimeError("Kafka consumer could not be created")
                    _set_state(status="running", kafka_connected=True, last_error=None)
                    backoff = max(0.01, settings.quality_consumer_retry_backoff_seconds)
                    await _consume(consumer, http_client, settings)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    _set_state(status="backoff", kafka_connected=False, last_error=str(e)[:300])
                    log.warning("quality_consumer.retrying", error=str(e), backoff_seconds=backoff)
                finally:
                    if consumer is not None:
                        try:
                            await loop.run_in_executor(None, consumer.close)
                        except Exception:  # noqa: BLE001
                            pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.quality_consumer_max_backoff_seconds)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(_drain_outbox(http_client, settings), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            _set_state(status="stopped", kafka_connected=False)
            log.info("quality_consumer.stopped")
            raise


async def _consume(consumer, http_client: httpx.AsyncClient, settings) -> None:
    loop = asyncio.get_running_loop()
    last_flush = time.monotonic()
    while True:
        def _poll():
            return consumer.poll(
                timeout_ms=int(settings.quality_consumer_flush_interval_seconds * 1000),
                max_records=settings.quality_consumer_batch_size)

        raw_records = await loop.run_in_executor(None, _poll)
        _set_state(last_poll_at=time.monotonic(), kafka_connected=True, status="running")

        entries: list[Entry] = []
        for tp, messages in raw_records.items():
            for msg in messages:
                if msg.value is None:
                    entries.append(tombstone_entry(tp.topic, tp.partition, msg.offset))
                    continue
                entries.append(process_record(tp.topic, tp.partition, msg.offset, msg.value,
                                              settings))
        # Every polled record is now classified (processed, dead-lettered or tombstone), so `entries` is non-empty whenever
        # `raw_records` is: the offset only moves after ALL of them are durable (a tombstone-only batch included).
        if entries:
            await _write_outbox(entries)              # durable acceptance ...
            await loop.run_in_executor(None, consumer.commit)   # ... before the offset moves
            log.info("quality_consumer.durable", count=len(entries),
                     rejected=sum(1 for e in entries if e.kind == KIND_DEAD and e.status == "rejected"),
                     tombstones=sum(1 for e in entries if e.kind == KIND_DEAD and e.status == "skipped"))

        now = time.monotonic()
        if now - last_flush >= settings.quality_consumer_flush_interval_seconds:
            try:
                await _sweep_stale(settings)
                sent, errors = await _drain_outbox(http_client, settings)
                if sent or errors:
                    log.info("quality_consumer.flushed", sent=sent, errors=errors)
            except Exception as e:  # noqa: BLE001
                log.warning("quality_consumer.drain_failed", error=str(e))
            last_flush = now

        await asyncio.sleep(0.1)
