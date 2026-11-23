"""Outbox delivery to UCS (DQ-013 audit, QualityCheckRegistration):
at-least-once, Idempotency-Key, scoped JWT, never dropped."""
import pytest
import respx
from jose import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.models import OutboxEvent
from app.services import audit
from app.services.outbox import drain_once
from app.services.ucs_sync_service import UcsSyncService
from tests.dq_fixtures import ORG, seed

S = get_settings()
AUDIT_URL = "http://ucs.test/api/v1/audit/events"
REG_URL = "http://ucs.test/api/v1/quality/checks/registrations"


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(S, "ucs_contract_v2_enabled", True)


async def _event(db):
    async with db() as s:
        await audit.record(s, org_id=ORG, actor="u1", resource_type="rule", resource_id="r1",
                           operation="update", resource_version=3)
        await s.commit()


async def _rows(db):
    async with db() as s:
        return list((await s.execute(select(OutboxEvent))).scalars())


async def test_delivers_with_idempotency_key_and_scoped_jwt(db, v2):
    await _event(db)
    with respx.mock() as m:
        route = m.post(AUDIT_URL).respond(202)
        stats = await drain_once(db)
    assert stats["delivered"] == 1
    req = route.calls[0].request
    row = (await _rows(db))[0]
    assert row.status == "delivered" and req.headers["idempotency-key"] == row.idempotency_key
    c = jwt.decode(req.headers["authorization"].split()[1], S.ucs_service_jwt_secret,
                   algorithms=["HS256"], audience="ucs")
    assert c["scope"] == "ucs.audit.events.write" and c["org_id"] == ORG


async def test_transient_failure_retries_never_drops(db, v2):
    await _event(db)
    with respx.mock() as m:
        m.post(AUDIT_URL).respond(503)
        assert (await drain_once(db))["retry"] == 1
    row = (await _rows(db))[0]
    assert row.status == "pending" and row.attempts == 1 and "503" in row.last_error
    # due again -> delivered, same key
    async with db() as s:
        r = await s.get(OutboxEvent, row.id)
        from datetime import datetime, timezone
        r.next_attempt_at = datetime.now(timezone.utc)
        await s.commit()
    with respx.mock() as m:
        route = m.post(AUDIT_URL).respond(200)
        await drain_once(db)
    assert route.calls[0].request.headers["idempotency-key"] == row.idempotency_key
    assert (await _rows(db))[0].status == "delivered"


async def test_conflict_counts_as_delivered_and_4xx_is_permanent(db, v2):
    await _event(db)
    await _event(db)
    with respx.mock() as m:
        m.post(AUDIT_URL).mock(side_effect=[respx.MockResponse(409), respx.MockResponse(400)])
        stats = await drain_once(db)
    assert stats == {"delivered": 1, "failed": 1, "retry": 0}


async def test_contract_disabled_keeps_rows_pending(db):
    await _event(db)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(AUDIT_URL)
        stats = await drain_once(db)
    assert stats["retry"] == 1 and not route.called
    row = (await _rows(db))[0]
    assert row.status == "pending" and "UCS_CONTRACT_V2_ENABLED" in row.last_error


async def test_sync_queues_versioned_registrations(db, v2):
    ids = await seed(db, rules=[("not_null", "id", {"threshold": 0.02}, {}),
                                ("range", "amount", {"min": 0}, {})])
    async with db() as s:
        report = await UcsSyncService(s).sync_org(ORG)
        again = await UcsSyncService(s).sync_org(ORG)
        await s.commit()
    assert report["registrations_queued"] == 2 and again["registrations_queued"] == 2
    rows = await _rows(db)
    assert len(rows) == 2                        # idempotent: same versions -> same keys
    p = rows[0].payload
    assert set(p) >= {"org_id", "dataset_fqn", "check_id", "rule_id", "rule_version",
                      "ruleset_id", "ruleset_version", "binding_id", "binding_version",
                      "enforcement_mode", "threshold"}
    with respx.mock() as m:
        route = m.post(REG_URL).respond(201)
        await drain_once(db)
    c = jwt.decode(route.calls[0].request.headers["authorization"].split()[1],
                   S.ucs_service_jwt_secret, algorithms=["HS256"], audience="ucs")
    assert c["scope"] == "ucs.quality.checks.write"
    assert ids["binding_id"]


async def test_sync_api(client, db):
    await seed(db)
    async with client as c:
        r = await c.post("/api/v1/sync/ucs")
    assert r.status_code == 200 and r.json()["registrations_queued"] == 1
