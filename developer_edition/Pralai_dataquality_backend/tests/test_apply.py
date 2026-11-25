"""Apply gates and runner contracts (DQ-CAP-007, DQ-014, DQ-AUTH-012, DQ-013)."""
import json
import time

import pytest
import respx
from jose import jwt
from sqlalchemy import select

from app.core import quality_contract as qc
from app.core.config import get_settings
from app.models import ArtifactApplication, OutboxEvent
from tests.dq_fixtures import ORG, seed
from tests.helpers import plan_payload

S = get_settings()


async def _apply(client, bid):
    return await client.post(f"/api/v1/bindings/{bid}/apply")


async def _rows(db, model):
    async with db() as s:
        return list((await s.execute(select(model))).scalars())


async def test_dbt_apply_sends_scoped_jwt_idempotency_and_artifact_shape(client, db):
    ids = await seed(db, mode="blocking")
    url = f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}/schema-tests"
    with respx.mock() as m:
        route = m.post(url).respond(204)
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    req = route.calls[0].request
    token = req.headers["x-internal-service-token"]
    claims = jwt.decode(token, S.dbtrunner_service_jwt_secret, algorithms=["HS256"],
                        audience="dbtrunner")
    assert claims["scope"] == "dbt.schema-tests.write" and claims["iss"] == "dataquality"
    assert claims["sub"] == "dataquality" and claims["org_id"] == ORG
    assert claims["project_id"] == str(ids["job_id"]) and claims["jti"]
    assert claims["exp"] - claims["iat"] <= 300 and claims["iat"] <= time.time() + 1
    body = json.loads(req.content)
    assert set(body) >= {"artifact_version_id", "binding_id", "project_id", "org_id",
                         "target_model", "dbt_schema_fragment", "idempotency_key"}
    assert req.headers["idempotency-key"] == body["idempotency_key"]
    assert body["idempotency_key"] == f"dq-apply:{ids['binding_id']}:{body['artifact_version_id']}"
    assert "severity: error" in body["dbt_schema_fragment"]
    assert body["engine_requirements"]["engine"] == "dbt-core"
    kinds = sorted(e.kind for e in await _rows(db, OutboxEvent))
    assert kinds == ["audit_event", "check_registration"]
    audit = next(e for e in await _rows(db, OutboxEvent) if e.kind == "audit_event")
    assert audit.payload["resource_id"] == body["artifact_version_id"]
    assert audit.payload["detail"]["rule_versions"]
    apps = await _rows(db, ArtifactApplication)
    assert [a.status for a in apps] == ["applied"]


async def test_reapply_same_artifact_is_idempotent(client, db):
    ids = await seed(db)
    url = f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}/schema-tests"
    with respx.mock() as m:
        route = m.post(url).respond(204)
        r1 = await _apply(client, ids["binding_id"])
        r2 = await _apply(client, ids["binding_id"])
    assert r1.json()["applied"] and r2.json()["idempotent_replay"] is True
    assert route.call_count == 1


async def test_unsupported_blocking_rule_fails_apply(client, db):
    ids = await seed(db, mode="blocking", rules=[("not_null", "id", {}, {}),
                                                 ("row_count", "", {}, {})])
    with respx.mock(assert_all_called=False) as m:
        route = m.post(f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}/schema-tests")
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "unsupported_blocking_rules"
    assert r.json()["unsupported_rules"][0]["rule_type"] == "row_count"
    assert not route.called
    assert [a.status for a in await _rows(db, ArtifactApplication)] == ["refused"]


async def test_warn_mode_with_unsupported_rule_still_applies(client, db):
    ids = await seed(db, mode="warn", rules=[("not_null", "id", {}, {}), ("row_count", "", {}, {})])
    with respx.mock() as m:
        m.post(f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}/schema-tests").respond(204)
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 200


async def test_incompatible_engine_version_fails_with_diagnostic(client, db, monkeypatch):
    monkeypatch.setattr(S, "dbtrunner_engine_version", "2.1.0")
    ids = await seed(db)
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "engine_version_incompatible"
    assert body["supported_range"]["max_exclusive"] == "2.0.0" and "remediation" in body


async def test_ops_pin_blocks_unpinned_engine(client, db, monkeypatch):
    monkeypatch.setattr(S, "supported_engine_versions", {"dbt_project": ["1.9"]})
    ids = await seed(db)
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "engine_version_not_pinned"


async def test_runner_side_engine_rejection_is_actionable(client, db):
    ids = await seed(db)
    with respx.mock() as m:
        m.post(f"http://dbtrunner.test/api/v1/projects/{ids['job_id']}/schema-tests").respond(
            422, json={"detail": {"code": "ENGINE_INCOMPATIBLE", "runner_engine_version": "1.4.0"}})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 502 and r.json()["reason"] == "engine_version_incompatible"


async def test_unresolved_binding_cannot_apply(client, db):
    ids = await seed(db, resolved=False)
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "binding_unresolved"


async def test_unapproved_custom_sql_blocks_blocking_apply(client, db):
    ids = await seed(db, mode="blocking", rules=[
        ("not_null", "id", {}, {}),
        ("custom_sql", "", {"condition": "amount > 0"}, {"review_status": "pending"})])
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and "not been approved" in json.dumps(r.json())


def _flink_mocks(m, ids, *, runner_contract=None, quality_valid=True, quality_sinks=None):
    job = ids["job_id"]
    m.get(f"http://flink.test/api/v1/jobs/{job}").respond(
        200, json={"id": str(job), "org_id": ORG, "sql": "-- parent sql", "name": "t"})
    quality_plan = plan_payload()
    quality_plan["sinks"] = [{"name": n, "columns": [], "options": {}}
                             for n in (quality_sinks or ["dq_results_sink"])]
    validate = m.post("http://flink.test/api/v1/validate")
    validate.side_effect = [
        respx.MockResponse(200, json={"valid": True, "errors": [],
                                      "plan": plan_payload(**(runner_contract or {}))}),
        respx.MockResponse(200, json={"valid": quality_valid, "errors": [] if quality_valid
                                      else ["bad"], "plan": quality_plan}),
    ]
    return validate


async def test_flink_apply_compiles_from_plan_and_creates_quality_job(client, db):
    ids = await seed(db, job_type="flink_job", target_table="orders_src")
    with respx.mock() as m:
        validate = _flink_mocks(m, ids)
        create = m.post("http://flink.test/api/v1/jobs").respond(201, json={"id": "q1"})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == "q1"
    payload = json.loads(create.calls[0].request.content)
    assert payload["job_kind"] == "quality" and payload["name"] == f"dq-quality-{ids['binding_id']}"
    assert qc.RESULTS_TOPIC in payload["sql"]
    assert create.calls[0].request.headers["authorization"] == "Bearer user-token"
    tok = validate.calls[0].request.headers["x-internal-service-token"]
    c = jwt.decode(tok, S.flinksql_service_jwt_secret, algorithms=["HS256"],
                   audience="flinksql-runner")
    assert c["scope"] == "flink.jobs.validate" and c["org_id"] == ORG
    assert json.loads(validate.calls[1].request.content)["sql"] == payload["sql"]


async def test_flink_contract_mismatch_refused(client, db):
    ids = await seed(db, job_type="flink_job", target_table="orders_src")
    with respx.mock() as m:
        _flink_mocks(m, ids, runner_contract={"contract_version": "1.0"})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "quality_contract_mismatch"


async def test_flink_topic_mismatch_refused(client, db):
    ids = await seed(db, job_type="flink_job", target_table="orders_src")
    with respx.mock() as m:
        _flink_mocks(m, ids, runner_contract={"topics": {"results": "quality.violations",
                                                         "gate": "g", "quarantine": "q"}})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "quality_topics_mismatch"


async def test_flink_runner_rejecting_compiled_sql_refuses_apply(client, db):
    ids = await seed(db, job_type="flink_job", target_table="orders_src")
    with respx.mock() as m:
        _flink_mocks(m, ids, quality_valid=False)
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "compiled_artifact_rejected"


async def test_flink_incompatible_engine_from_plan_refused(client, db):
    ids = await seed(db, job_type="flink_job", target_table="orders_src")
    with respx.mock() as m:
        job = ids["job_id"]
        m.get(f"http://flink.test/api/v1/jobs/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "sql": "x", "name": "t"})
        m.post("http://flink.test/api/v1/validate").respond(
            200, json={"valid": True, "errors": [], "plan": plan_payload(engine_version="1.14.6")})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["reason"] == "engine_version_incompatible"
    assert r.json()["target_engine_version"] == "1.14.6"


async def test_flink_invalid_parent_job_refused(client, db):
    ids = await seed(db, job_type="flink_job")
    with respx.mock() as m:
        job = ids["job_id"]
        m.get(f"http://flink.test/api/v1/jobs/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "sql": "x", "name": "t"})
        m.post("http://flink.test/api/v1/validate").respond(
            200, json={"valid": False, "errors": ["syntax"], "plan": plan_payload()})
        r = await _apply(client, ids["binding_id"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PARENT_JOB_INVALID"


async def test_python_apply_is_not_applicable(client, db):
    ids = await seed(db, job_type="python_job")
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 200 and r.json()["status"] == "not_applicable"


async def test_apply_requires_permission(client, db, as_principal):
    from tests.conftest import make_principal
    ids = await seed(db)
    as_principal(make_principal(roles=["analyst"]))
    r = await _apply(client, ids["binding_id"])
    assert r.status_code == 403
