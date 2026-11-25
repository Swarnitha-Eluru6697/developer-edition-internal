"""DQ-BIND-003: binding creation resolves + authorizes job and dataset."""
import uuid

import httpx
import pytest
import respx
from jose import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.models import JobBinding, Ruleset
from tests.conftest import make_principal

S = get_settings()
ORG = "test-org"


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setattr(S, "ucs_contract_v2_enabled", True)


async def _ruleset(db):
    async with db() as s:
        rs = Ruleset(org_id=ORG, name=f"rs-{uuid.uuid4().hex[:6]}", version=1)
        s.add(rs)
        await s.commit()
        return rs.id


def _ucs_ok(mock, fqn="pg.public.orders", canonical="pg.public.orders", decision="allow",
            object_type="table", org=ORG):
    mock.post("http://ucs.test/api/v1/datasets/resolve").respond(
        200, json={"fqn": canonical, "object_type": object_type, "org_id": org})
    return mock.post("http://ucs.test/api/v1/authz/check").respond(
        200, json={"decision": decision, "reason": "nope" if decision != "allow" else "ok"})


def _body(rs, job_id, **kw):
    return {"ruleset_id": str(rs), "job_type": "dbt_project", "job_id": str(job_id),
            "target_table": "orders", "ucs_fqn": "pg.public.orders", **kw}


async def test_dbt_binding_resolves_and_persists_canonical_ids(client, db, v2):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=True) as m:
        proj = m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "jaffle", "engine": "trino"})
        m.post("http://ucs.test/api/v1/datasets/resolve").respond(
            200, json={"fqn": "PG.public.Orders", "object_type": "table", "org_id": ORG})
        authz = m.post("http://ucs.test/api/v1/authz/check").respond(
            200, json={"decision": "allow"})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job, ucs_fqn="pg.public.orders"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["ucs_fqn"] == "PG.public.Orders" and b["dataset_fqn"] == "PG.public.Orders"
    assert b["job_name"] == "jaffle" and b["job_kind"] == "dbt_project" and b["resolved_at"]
    assert proj.calls[0].request.headers["authorization"] == "Bearer user-token"
    req = authz.calls[0].request
    assert b'"action":"dq.bind"' in req.content.replace(b" ", b"")
    claims = jwt.decode(req.headers["authorization"].split()[1], S.ucs_service_jwt_secret,
                        algorithms=["HS256"], audience="ucs")
    assert claims["scope"] == "ucs.authz.check" and claims["org_id"] == ORG
    assert claims["on_behalf_of"] == "u1@example.com"


async def test_cross_org_job_rejected(client, db, v2):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": "other-org", "name": "x"})
        _ucs_ok(m)
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 403
    async with db() as s:
        assert (await s.execute(select(JobBinding))).first() is None


async def test_missing_job_rejected(client, db, v2):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(404, json={})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 404


async def test_mistyped_flink_quality_job_rejected(client, db, v2):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://flink.test/api/v1/jobs/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "q", "job_kind": "quality"})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job, job_type="flink_job"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "JOB_KIND_MISMATCH"


async def test_dataset_authorization_denied(client, db, v2):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "x"})
        _ucs_ok(m, decision="deny")
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 403


@pytest.mark.parametrize("ds,code", [
    ({"fqn": "pg.public", "object_type": "schema", "org_id": ORG}, 422),
    ({"fqn": "pg.public.orders", "object_type": "table", "org_id": "other"}, 403),
])
async def test_dataset_wrong_type_or_org_rejected(client, db, v2, ds, code):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "x"})
        m.post("http://ucs.test/api/v1/datasets/resolve").respond(200, json=ds)
        m.post("http://ucs.test/api/v1/authz/check").respond(200, json={"decision": "allow"})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == code, r.text


async def test_ucs_contract_unavailable_fails_closed(client, db):
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "x"})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "UCS_CONTRACT_UNAVAILABLE"


async def test_dev_bypass_records_unverified_dataset(client, db, monkeypatch):
    monkeypatch.setattr(S, "ucs_bind_dev_bypass", True)
    rs, job = await _ruleset(db), uuid.uuid4()
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "name": "x"})
        async with client as c:
            r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 201
    assert r.json()["resolution"]["dataset"]["verified"] is False


async def test_binding_requires_dq_bind_and_bearer(client, db, v2, as_principal):
    rs, job = await _ruleset(db), uuid.uuid4()
    as_principal(make_principal(roles=["analyst"]))
    async with client as c:
        assert (await c.post("/api/v1/bindings", json=_body(rs, job))).status_code == 403
    as_principal(make_principal())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=__import__("app.main").main.app),
                                 base_url="http://t") as c:
        r = await c.post("/api/v1/bindings", json=_body(rs, job))
    assert r.status_code == 401


@pytest.mark.parametrize("override", [{"ucs_fqn": "pg.public.orders'; drop"},
                                      {"target_table": "a\nb"},
                                      {"flink_fail_stop": True}])
async def test_invalid_binding_inputs_rejected(client, db, v2, override):
    rs = await _ruleset(db)
    async with client as c:
        r = await c.post("/api/v1/bindings", json=_body(rs, uuid.uuid4(), **override))
    assert r.status_code == 400


async def test_cross_org_ruleset_not_visible(client, db, v2):
    async with db() as s:
        rs = Ruleset(org_id="other-org", name="theirs", version=1)
        s.add(rs)
        await s.commit()
    async with client as c:
        r = await c.post("/api/v1/bindings", json=_body(rs.id, uuid.uuid4()))
    assert r.status_code == 404


async def test_resolve_legacy_binding(client, db, v2):
    from tests.dq_fixtures import seed
    ids = await seed(db, resolved=False)
    with respx.mock(assert_all_called=False) as m:
        m.get(f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}").respond(
            200, json={"id": str(ids["job_id"]), "org_id": ORG, "name": "p", "engine": "trino"})
        _ucs_ok(m)
        async with client as c:
            r = await c.post(f"/api/v1/bindings/{ids['binding_id']}/resolve")
    assert r.status_code == 200, r.text
    assert r.json()["resolved_at"] and r.json()["version"] == 2
