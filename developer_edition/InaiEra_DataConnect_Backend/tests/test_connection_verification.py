"""
BUG-08: a live connectivity probe used to be wrapped in a bare
`except Exception: pass` at connection-creation time, so a probe that
raised (a timeout, a DNS failure, ...) left the connection saved with no
diagnostic at all — not even a failed status. ConnectionProfileService.
probe_and_record is the fix: it always persists verification_status/
last_tested_at/last_test_message, whether the probe returns cleanly or
raises, and is shared by both creation's own auto-probe and the explicit
"Test connection" action.
"""

import asyncio

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services.connection_service import connection_service
from app.services.secrets import sanitize_probe_message

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


async def _create(org_id: str, name: str = "c") -> dict:
    return await connection_service.create_connection(
        org_id,
        {"name": name, "source_type": "postgres",
         "connection_config": {"hostname": "db.internal", "database": "d", "username": "u", "password": "p"}},
    )


@pytest.mark.asyncio
async def test_probe_and_record_persists_a_diagnostic_when_the_probe_raises(fake_postgres, monkeypatch) -> None:
    """The core of BUG-08: a raised exception (not just a returned failure)
    must still leave a durable, honest record — never silently swallowed."""
    created = await _create("org_bug08_1")

    async def _raises(*args, **kwargs):
        raise TimeoutError("Connection timed out")

    monkeypatch.setattr(connection_service, "live_test_connection", _raises)

    result = await connection_service.probe_and_record("org_bug08_1", created["id"])

    assert result["status"] == "error"
    assert "timed out" in result["message"].lower()

    stored = fake_postgres.connection_verifications[created["id"]]  # GAP-02: its own record, not the profile row
    assert stored.status == "error"
    assert stored.error_code == "TIMEOUT"
    assert stored.tested_at is not None
    assert "timed out" in stored.message.lower()
    seen = await connection_service.get_or_404("org_bug08_1", created["id"])
    assert (seen["status"], seen["verification_status"], seen["error_code"]) == ("error", "error", "TIMEOUT")


@pytest.mark.asyncio
async def test_probe_and_record_persists_ok_on_a_successful_probe(fake_postgres, monkeypatch) -> None:
    created = await _create("org_bug08_2")

    async def _ok(*args, **kwargs):
        return {"status": "ok", "message": "Authenticated to PostgreSQL", "details": {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _ok)

    result = await connection_service.probe_and_record("org_bug08_2", created["id"])

    assert result["status"] == "ok"
    seen = await connection_service.get_or_404("org_bug08_2", created["id"])
    assert seen["status"] == "active"
    assert seen["verification_status"] == "ok"
    assert seen["last_tested_at"] is not None
    assert seen["last_test_message"] == "Authenticated to PostgreSQL"
    assert seen["error_code"] is None


@pytest.mark.asyncio
async def test_probe_and_record_persists_error_on_a_clean_failed_probe(fake_postgres, monkeypatch) -> None:
    """Distinct from the raises-case above: connection_adapters.py can also
    just return a "failed" ConnectionCheck without raising at all — same
    durable outcome either way."""
    created = await _create("org_bug08_3")

    async def _failed(*args, **kwargs):
        return {"status": "failed", "message": "Cannot reach db.internal:5432 — timeout", "details": {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _failed)

    result = await connection_service.probe_and_record("org_bug08_3", created["id"])

    assert result["status"] == "failed"
    seen = await connection_service.get_or_404("org_bug08_3", created["id"])
    assert seen["status"] == "error"
    assert seen["verification_status"] == "error"
    assert "timeout" in seen["last_test_message"].lower()
    assert seen["error_code"] == "PROBE_FAILED"  # a clean failure still gets a stable code


def test_create_connection_route_saves_and_returns_a_diagnostic_when_the_probe_raises(
    fake_postgres, monkeypatch
) -> None:
    """End-to-end through the real route (BUG-08's own example): creation
    itself must still succeed (201, connection saved) even though the probe
    blew up, with the diagnostic visible in the response body — not the old
    "silently ignored" behavior."""

    async def _raises(*args, **kwargs):
        raise TimeoutError("Connection timed out")

    async def _noop_record_audit(**kwargs):
        return None

    monkeypatch.setattr(connection_service, "live_test_connection", _raises)
    monkeypatch.setattr("app.api.routes.connections.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/connections",
        headers={"X-Tenant-Id": "org_bug08_4"},
        json={"name": "c", "source_type": "postgres",
              "connection_config": {"hostname": "db.internal", "database": "d", "username": "u", "password": "p"}},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["verification_status"] == "error"
    assert body["last_tested_at"] is not None
    assert "timed out" in body["last_test_message"].lower()


def test_sanitize_probe_message_scrubs_embedded_uri_credentials() -> None:
    message = sanitize_probe_message("connect failed: mongodb://admin:s3cr3t@mongo.internal:27017/db unreachable")
    assert "s3cr3t" not in message
    assert "admin" not in message
    assert "mongo.internal" in message


def test_sanitize_probe_message_caps_length() -> None:
    message = sanitize_probe_message("x" * 1000)
    assert len(message) <= 501


def test_sanitize_probe_message_handles_none_and_empty() -> None:
    assert sanitize_probe_message(None) == ""
    assert sanitize_probe_message("") == ""


# ── GAP-02 ────────────────────────────────────────────────────────────────
# Operational state (status/verification_status/last_tested_at/
# last_test_message) must never be touched by the general connection-edit
# path — only probe_and_record writes it. status used to be one of
# ConnectionProfileCreate's own fields, reused as the PUT body, so a routine
# edit silently reset it to "draft" (Pydantic fills in a field's default
# even when the request JSON never mentioned it) and would have clobbered
# an in-progress verification result too, since both went through the same
# update_connection() call.


@pytest.mark.asyncio
async def test_updating_a_connection_does_not_reset_its_operational_state(fake_postgres, monkeypatch) -> None:
    created = await _create("org_gap02_1")

    async def _ok(*args, **kwargs):
        return {"status": "ok", "message": "Authenticated", "details": {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _ok)
    await connection_service.probe_and_record("org_gap02_1", created["id"])

    stored_before = fake_postgres.connection_profiles[created["id"]]
    seen_before = await connection_service.get_or_404("org_gap02_1", created["id"])
    assert seen_before["status"] == "active"
    assert seen_before["verification_status"] == "ok"

    # A routine edit — renaming only, echoing the masked password back
    # unchanged (a real edit form's own contract) — `status` isn't even a
    # field on the request schema any more (see ConnectionProfileBase), so
    # this must leave the operational state untouched either way.
    await connection_service.update_connection(
        "org_gap02_1", created["id"],
        {
            "name": "renamed", "source_type": "postgres",
            "connection_config": {**stored_before.connection_config, "password": "********"},
        },
    )

    stored_after = fake_postgres.connection_profiles[created["id"]]
    seen_after = await connection_service.get_or_404("org_gap02_1", created["id"])
    assert stored_after.name == "renamed"
    assert seen_after["status"] == "active"
    assert seen_after["verification_status"] == "ok"
    assert seen_after["last_tested_at"] == seen_before["last_tested_at"]
    assert seen_after["last_test_message"] == seen_before["last_test_message"]
    assert seen_after["verification_stale"] is False  # a rename does not invalidate the result


def test_connection_profile_create_schema_has_no_status_field() -> None:
    """The request schema literally can't carry it — not just "the route
    happens not to use it" — so a future call site can't reintroduce the
    coupling by accident."""
    from app.schemas.connection import ConnectionProfileCreate

    assert "status" not in ConnectionProfileCreate.model_fields


def test_a_client_supplied_status_in_the_request_body_is_ignored(fake_postgres, monkeypatch) -> None:
    """End-to-end: even if a client sends `status` in the JSON body (an old
    frontend build, a stray API client), it has no effect — Pydantic drops
    an unrecognized field rather than erroring, by this app's convention."""

    async def _noop_record_audit(**kwargs):
        return None

    async def _ok(*args, **kwargs):
        return {"status": "ok", "message": "Authenticated", "details": {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _ok)
    monkeypatch.setattr("app.api.routes.connections.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/connections",
        headers={"X-Tenant-Id": "org_gap02_2"},
        json={
            "name": "c", "source_type": "postgres", "status": "error",  # attempted override
            "connection_config": {"hostname": "db.internal", "database": "d", "username": "u", "password": "p"},
        },
    )

    assert resp.status_code == 201, resp.text
    # Driven only by the (successful) probe, never the client's "error".
    assert resp.json()["status"] == "active"


@pytest.mark.asyncio
async def test_a_raising_probe_neither_logs_nor_persists_embedded_credentials(fake_postgres, monkeypatch) -> None:
    """BUG-08 follow-up: the diagnostic was sanitized in the database but the log line carried the raw
    exception text. Both must be scrubbed."""
    from structlog.testing import capture_logs

    created = await _create("org_probe_log")
    secret = "TEST_SECRET_9f32d1e7c8b4a5069d2f1e3c4b5a6978"

    async def _boom(*args, **kwargs):
        raise ConnectionError(f"could not connect to postgres://dataconnect:{secret}@db.internal:5432/erp")

    monkeypatch.setattr(connection_service, "live_test_connection", _boom)

    with capture_logs() as logs:
        result = await connection_service.probe_and_record("org_probe_log", created["id"])

    assert result["status"] == "error"
    failure_logs = [e for e in logs if e["event"] == "connection.probe_failed"]
    assert len(failure_logs) == 1
    assert secret not in str(logs)  # not in ANY log event emitted during the probe
    assert "db.internal" in failure_logs[0]["error"]  # the useful part of the diagnostic is kept
    assert failure_logs[0]["error_type"] == "ConnectionError"
    stored = fake_postgres.connection_verifications[created["id"]]
    assert secret not in (stored.message or "")
    assert stored.status == "error"


# ── GAP-02: verification is its own record, with latency + error code, and an edit invalidates it ───────────────

async def _verified(org_id: str, monkeypatch, *, latency=None) -> dict:
    created = await _create(org_id)

    async def _ok(*args, **kwargs):
        return {"status": "ok", "message": "Authenticated", "details": {"latency_ms": latency} if latency else {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _ok)
    await connection_service.probe_and_record(org_id, created["id"])
    return created


@pytest.mark.asyncio
async def test_the_probe_outcome_is_stored_apart_from_the_connection_profile(fake_postgres, monkeypatch) -> None:
    created = await _verified("org_gap02_split", monkeypatch)

    profile = fake_postgres.connection_profiles[created["id"]]
    assert not {"status", "verification_status", "last_tested_at", "last_test_message"} & set(profile.__dict__)
    assert fake_postgres.connection_verifications[created["id"]].status == "ok"


@pytest.mark.asyncio
async def test_latency_is_recorded_and_reported(fake_postgres, monkeypatch) -> None:
    created = await _verified("org_gap02_latency", monkeypatch, latency=12.5)

    seen = await connection_service.get_or_404("org_gap02_latency", created["id"])
    assert seen["latency_ms"] == 12.5


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "code"), [
    (TimeoutError("t"), "TIMEOUT"),
    (ConnectionRefusedError("r"), "CONNECTION_REFUSED"),
    (__import__("socket").gaierror("no such host"), "DNS_FAILURE"),
    (PermissionError("p"), "PERMISSION_DENIED"),
    (RuntimeError("something else"), "PROBE_ERROR"),
])
async def test_a_raising_probe_records_a_stable_error_code(fake_postgres, monkeypatch, exc, code) -> None:
    created = await _create("org_gap02_codes")

    async def _raises(*args, **kwargs):
        raise exc

    monkeypatch.setattr(connection_service, "live_test_connection", _raises)
    await connection_service.probe_and_record("org_gap02_codes", created["id"])

    assert fake_postgres.connection_verifications[created["id"]].error_code == code


@pytest.mark.asyncio
async def test_a_connection_that_was_never_tested_is_unverified_with_no_record(fake_postgres) -> None:
    created = await _create("org_gap02_new")

    seen = await connection_service.get_or_404("org_gap02_new", created["id"])

    assert (seen["status"], seen["verification_status"]) == ("draft", "unverified")
    assert seen["last_tested_at"] is None
    assert fake_postgres.connection_verifications == {}


_NEW_HOST = {"hostname": "other-host.internal", "database": "d", "username": "u", "password": "********"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"connection_config": _NEW_HOST},                                   # a different host
    {"network_profile": "private-link-2"},                             # a different network route
    {"source_type": "mysql", "connection_config": {**_NEW_HOST, "hostname": "db.internal"}},  # a different kind of source
])
async def test_changing_how_a_connection_connects_makes_its_last_result_stale(fake_postgres, monkeypatch, change) -> None:
    created = await _verified("org_gap02_stale", monkeypatch)
    before = await connection_service.get_or_404("org_gap02_stale", created["id"])
    assert before["verification_status"] == "ok"

    payload = {"name": "c", "source_type": "postgres", "connection_config": before["connection_config"], **change}
    await connection_service.update_connection("org_gap02_stale", created["id"], payload)

    after = await connection_service.get_or_404("org_gap02_stale", created["id"])
    assert after["verification_stale"] is True
    assert (after["status"], after["verification_status"]) == ("draft", "unverified")
    assert "changed after it was last tested" in after["last_test_message"]
    assert after["last_tested_at"] is not None  # when it WAS tested is still shown


@pytest.mark.asyncio
async def test_a_new_password_makes_the_last_result_stale(fake_postgres, monkeypatch) -> None:
    created = await _verified("org_gap02_pw", monkeypatch)
    before = await connection_service.get_or_404("org_gap02_pw", created["id"])

    await connection_service.update_connection(
        "org_gap02_pw", created["id"],
        {"name": "c", "source_type": "postgres", "connection_config": {**before["connection_config"], "password": "a-new-password"}},
    )

    assert (await connection_service.get_or_404("org_gap02_pw", created["id"]))["verification_stale"] is True


@pytest.mark.asyncio
async def test_retesting_after_an_edit_makes_it_current_again(fake_postgres, monkeypatch) -> None:
    created = await _verified("org_gap02_retest", monkeypatch)
    before = await connection_service.get_or_404("org_gap02_retest", created["id"])
    await connection_service.update_connection(
        "org_gap02_retest", created["id"],
        {"name": "c", "source_type": "postgres", "connection_config": {**before["connection_config"], "hostname": "moved.internal"}},
    )

    await connection_service.probe_and_record("org_gap02_retest", created["id"])

    after = await connection_service.get_or_404("org_gap02_retest", created["id"])
    assert after["verification_stale"] is False
    assert after["verification_status"] == "ok"


@pytest.mark.asyncio
async def test_an_edit_that_lands_mid_probe_leaves_the_result_stale_instead_of_blessing_the_new_settings(fake_postgres, monkeypatch) -> None:
    created = await _create("org_gap02_race")
    before = await connection_service.get_or_404("org_gap02_race", created["id"])

    async def _probe_during_which_the_user_edits(*args, **kwargs):
        await connection_service.update_connection(
            "org_gap02_race", created["id"],
            {"name": "c", "source_type": "postgres", "connection_config": {**before["connection_config"], "hostname": "edited.internal"}},
        )
        return {"status": "ok", "message": "Authenticated", "details": {}}

    monkeypatch.setattr(connection_service, "live_test_connection", _probe_during_which_the_user_edits)
    await connection_service.probe_and_record("org_gap02_race", created["id"])

    seen = await connection_service.get_or_404("org_gap02_race", created["id"])
    assert seen["verification_stale"] is True  # the probe tested the OLD settings


def test_a_client_cannot_write_verification_through_the_edit_endpoint(fake_postgres, monkeypatch) -> None:
    async def _noop_record_audit(**kwargs):
        return None

    monkeypatch.setattr("app.api.routes.connections.record_audit", _noop_record_audit)
    created = asyncio.run(_create("org_gap02_http"))

    resp = client.put(
        f"/api/v1/dataconnect/connections/{created['id']}", headers={"X-Tenant-Id": "org_gap02_http"},
        json={
            "name": "c", "source_type": "postgres",
            "connection_config": {"hostname": "db.internal", "database": "d", "username": "u", "password": "********"},
            "verification_status": "ok", "status": "active", "last_test_message": "forged", "error_code": "x",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["verification_status"], body["status"]) == ("unverified", "draft")
    assert fake_postgres.connection_verifications == {}


@pytest.mark.asyncio
async def test_probe_latency_is_measured_when_the_adapter_does_not_report_one(fake_postgres, monkeypatch) -> None:
    from app.services.connection_adapters import ConnectionCheck

    created = await _create("org_gap02_timed")

    async def _slow_probe(connection):
        await asyncio.sleep(0.05)
        return ConnectionCheck("ok", "Authenticated", {})  # no latency_ms of its own

    monkeypatch.setattr("app.services.connection_service.live_probe", _slow_probe)

    await connection_service.probe_and_record("org_gap02_timed", created["id"])

    seen = await connection_service.get_or_404("org_gap02_timed", created["id"])
    assert seen["latency_ms"] is not None
    assert seen["latency_ms"] >= 40


@pytest.mark.asyncio
async def test_an_adapter_reported_latency_is_kept_rather_than_overwritten(fake_postgres, monkeypatch) -> None:
    from app.services.connection_adapters import ConnectionCheck

    created = await _create("org_gap02_reported")

    async def _probe(connection):
        return ConnectionCheck("ok", "Authenticated", {"latency_ms": 7.0})

    monkeypatch.setattr("app.services.connection_service.live_probe", _probe)

    await connection_service.probe_and_record("org_gap02_reported", created["id"])

    assert (await connection_service.get_or_404("org_gap02_reported", created["id"]))["latency_ms"] == 7.0
