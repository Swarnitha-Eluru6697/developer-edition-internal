"""Rules / rulesets API on a real migrated DB: versions, privileged
custom_sql, audit events in the transactional outbox (DQ-013)."""
from sqlalchemy import select

from app.models import OutboxEvent
from tests.conftest import make_principal


async def _outbox(db):
    async with db() as s:
        return list((await s.execute(select(OutboxEvent).order_by(OutboxEvent.created_at))).scalars())


async def test_rule_crud_versions_and_audit(client, db):
    async with client as c:
        r = await c.post("/api/v1/rules", json={"name": "amt", "rule_type": "range",
                                                "dimension": "validity", "parameters": {"min": 0}})
        assert r.status_code == 201, r.text
        rule = r.json()
        assert rule["version"] == 1 and rule["privileged"] is False
        r = await c.patch(f"/api/v1/rules/{rule['id']}", json={"description": "d"})
        assert r.json()["version"] == 1           # cosmetic change: no version bump
        r = await c.patch(f"/api/v1/rules/{rule['id']}", json={"parameters": {"min": 5}})
        assert r.json()["version"] == 2
        r = await c.patch(f"/api/v1/rules/{rule['id']}", json={"parameters": {"min": "5"}})
        assert r.status_code == 400
        assert (await c.delete(f"/api/v1/rules/{rule['id']}")).status_code == 204
    events = await _outbox(db)
    ops = [(e.payload["event_type"], e.payload["resource_version"]) for e in events]
    assert ops == [("dq.rule.create", "1"), ("dq.rule.update", "1"), ("dq.rule.update", "2"),
                   ("dq.rule.delete", "2")]
    assert all(e.kind == "audit_event" and e.status == "pending" for e in events)
    assert events[0].payload["actor"] == "u1" and events[0].payload["org_id"] == "test-org"


async def test_failed_change_writes_no_audit_event(client, db):
    async with client as c:
        r = await c.post("/api/v1/rules", json={"name": "x", "rule_type": "range",
                                                "dimension": "validity",
                                                "parameters": {"min": "oops"}})
    assert r.status_code == 400
    assert await _outbox(db) == []


async def test_custom_sql_requires_permission_ack_and_lexer(client, db, as_principal):
    body = {"name": "pos", "rule_type": "custom_sql", "dimension": "custom",
            "parameters": {"condition": "amount > 0"}}
    as_principal(make_principal(roles=["developer"]))
    c = client
    r = await c.post("/api/v1/rules", json={**body, "acknowledged_privileged": True})
    assert r.status_code == 403
    as_principal(make_principal(roles=["admin"]))
    if True:
        assert (await c.post("/api/v1/rules", json=body)).status_code == 400
        bad = {**body, "parameters": {"condition": "1=1; DROP TABLE t"},
               "acknowledged_privileged": True}
        assert (await c.post("/api/v1/rules", json=bad)).status_code == 400
        r = await c.post("/api/v1/rules", json={**body, "acknowledged_privileged": True})
        assert r.status_code == 201, r.text
        rule = r.json()
        assert rule["privileged"] and rule["review_status"] == "approved"
        assert rule["reviewed_by"] == "u1"
        r = await c.post(f"/api/v1/rules/{rule['id']}/approve", json={})
        assert r.status_code == 400
        r = await c.post(f"/api/v1/rules/{rule['id']}/approve",
                         json={"acknowledged_privileged": True})
        assert r.json()["version"] == 2


async def test_ruleset_items_bump_version_and_validate(client, db):
    async with client as c:
        rule = (await c.post("/api/v1/rules", json={"name": "nn", "rule_type": "not_null",
                                                    "dimension": "nulls"})).json()
        rs = (await c.post("/api/v1/rulesets", json={"name": "rs"})).json()
        assert rs["version"] == 1
        r = await c.post(f"/api/v1/rulesets/{rs['id']}/items",
                         json={"rule_id": rule["id"], "column_name": "id"})
        assert r.status_code == 201
        item = r.json()
        r = await c.post(f"/api/v1/rulesets/{rs['id']}/items",
                         json={"rule_id": rule["id"], "column_name": "bad\ncol"})
        assert r.status_code == 400
        r = await c.patch(f"/api/v1/rulesets/{rs['id']}/items/{item['id']}",
                          json={"parameter_overrides": {"threshold": 0.5}})
        assert r.status_code == 200
        r = await c.patch(f"/api/v1/rulesets/{rs['id']}/items/{item['id']}",
                          json={"parameter_overrides": {"nonsense": 1}})
        assert r.status_code == 400
        got = (await c.get(f"/api/v1/rulesets/{rs['id']}")).json()
        assert got["version"] == 3
        cap = (await c.get(f"/api/v1/rulesets/{rs['id']}/capability",
                           params={"job_type": "flink_job", "enforcement_mode": "blocking"})).json()
        assert cap["degraded"] and cap["mode_semantics"]["guarantee"] == "degraded"
        summary = (await c.get("/api/v1/rulesets")).json()
        assert summary[0]["item_count"] == 1 and summary[0]["version"] == 3
