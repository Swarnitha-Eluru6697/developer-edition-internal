"""X-006: UCS governs production data actions, not just data access — uniformly, and never by accident of a forgotten route.

Before: only data access (a connection's `query`) asked UCS. Running a dbt project, deploying or cancelling a Flink job, publishing
a Data Quality ruleset, pushing a repository were decided by Workspace's local role matrix alone. Now every route of the dbt, Flink,
Data Quality and Git routers names the product action it performs (app.core.data_policy.ROUTE_ACTIONS); inside the RBAC capability
dependency (after the role check) UCS is asked, with the policy context (environment, classification, resource owner, target
object), in one of three modes: off | audit (record, never block; the default) | enforce (deny = 403, UCS unreachable/unconfigured
= 503: fail closed).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from conftest import _headers, subject_for
from fastapi.routing import APIRoute

from app.api import dbt_proxy, dq_proxy, flink_proxy
from app.api import git as git_api
from app.core import data_policy as dp
from app.core.config import get_settings

JID, BID = str(uuid.uuid4()), str(uuid.uuid4())
ROLES = ["data_engineer"]


# ── the table is complete and consistent (uniform application) ─────────────────
ROUTERS = {"dbt": dbt_proxy.router, "flink": flink_proxy.router, "dq": dq_proxy.router, "git": git_api.router}


def _routes():
    for name, router in ROUTERS.items():
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    yield name, method, route.path, route


def test_every_route_of_the_governed_routers_has_a_policy_entry_and_no_entry_is_stale():
    actual = {(m, p) for _, m, p, _ in _routes()}
    assert len(actual) > 50, "route discovery found too few routes: the guard would be vacuous"
    assert actual == set(dp.ROUTE_ACTIONS), (sorted(actual - set(dp.ROUTE_ACTIONS)), sorted(set(dp.ROUTE_ACTIONS) - actual))


def test_every_gated_route_has_a_dependency_that_applies_the_policy():
    missing = [(m, p) for _, m, p, route in _routes() if dp.ROUTE_ACTIONS[(m, p)] is not None
               and not any(hasattr(d.call, "required_permissions") or hasattr(d.call, "applies_route_policy")
                           for d in route.dependant.dependencies)]
    assert not missing, f"gated routes without a permission dependency would never reach UCS: {missing}"


def test_only_documented_routes_are_exempt():
    exempt = {k for k, v in dp.ROUTE_ACTIONS.items() if v is None}
    assert exempt == set(dp.EXEMPT_REASONS) and all(dp.EXEMPT_REASONS.values())


def test_the_vocabulary_is_the_one_the_finding_asks_for_and_maps_onto_ucs_actions():
    asked = {"dbt.project.read", "dbt.project.edit", "dbt.run", "dbt.deploy", "flink.job.read", "flink.job.edit", "flink.deploy",
             "flink.cancel", "flink.savepoint", "dq.rule.read", "dq.rule.edit", "dq.publish", "dq.execute", "dataset.read",
             "dataset.write", "dataset.preview", "git.repo.read", "git.repo.write"}
    assert set(dp.ACTIONS) == asked
    assert {a.ucs_action for a in dp.ACTIONS.values()} <= {"read", "write", "query", "create_dataset", "publish_dataset"}
    used = {v for v in dp.ROUTE_ACTIONS.values() if v}
    assert used | dp.INLINE_DATASET_ACTIONS | {"dbt.deploy"} == asked          # nothing in the vocabulary is unattached by accident


@pytest.mark.parametrize("method,path,action", [
    ("POST", "/api/v1/dbt/projects/{project_id}/run", "dbt.run"),
    ("POST", "/api/v1/flink/jobs/{job_id}/deploy", "flink.deploy"),
    ("POST", "/api/v1/flink/jobs/{job_id}/cancel", "flink.cancel"),
    ("POST", "/api/v1/flink/jobs/{job_id}/stop", "flink.savepoint"),
    ("POST", "/api/v1/dq/bindings/{binding_id}/apply", "dq.publish"),
    ("POST", "/api/v1/dq/sync/ucs", "dq.publish"),
    ("POST", "/api/v1/dq/render/{binding_id}/refresh", "dq.execute"),
    ("POST", "/api/v1/git/workspaces/{workspace_id}/push", "git.repo.write"),
    ("GET", "/api/v1/dbt/projects", "dbt.project.read"),
])
def test_the_production_actions_are_mapped_to_their_product_action(method, path, action):
    assert dp.ROUTE_ACTIONS[(method, path)] == action


def test_every_read_route_is_a_read_and_every_mutating_route_is_not():
    for (method, path), action in dp.ROUTE_ACTIONS.items():
        if action is None:
            continue
        ucs = dp.ACTIONS[action].ucs_action
        if method == "GET":
            assert ucs in ("read", "query"), (method, path, action)
        elif method in ("PATCH", "DELETE"):
            assert ucs == "write", (method, path, action)


# ── behaviour ────────────────────────────────────────────────────────────────
@pytest.fixture
def world(monkeypatch):
    """A fake UCS + fake dbt/Flink/DQ services. `ucs` controls UCS's answer; `calls` records every outgoing request."""
    s = get_settings()
    for attr, value in {"dbtrunner_delegation_jwt_secret": "d" * 40, "flinksql_delegation_jwt_secret": "f" * 40,
                        "dq_delegation_jwt_secret": "q" * 40, "dbtrunner_url": "http://dbt.test",
                        "flinksql_runner_url": "http://flink.test", "dq_service_url": "http://dq.test",
                        "ucs_service_url": "http://ucs.test", "ucs_gateway_secret": "g", "environment": "dev",
                        "data_policy_mode": "audit", "data_policy_enforced_actions": []}.items():
        monkeypatch.setattr(s, attr, value)
    dp.reset_backoff()
    state = {"decision": "allow", "reason": "granted via grp::role", "ucs_down": False, "classification": ["class.email"]}
    calls: list[httpx.Request] = []
    audits: list[tuple] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.host == "ucs.test":
            if state["ucs_down"]:
                raise httpx.ConnectError("ucs down", request=req)
            if req.url.path.endswith("/authz/check"):
                return httpx.Response(200, json={"decision": state["decision"], "reason": state["reason"]})
            if req.url.path.endswith("/classification"):
                return httpx.Response(200, json={"classifications": [{"tag": t} for t in state["classification"]]})
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def fake_audit(principal, db, decision, action, kind, obj, reason, context):
        audits.append((decision, action, kind, obj, reason))

    monkeypatch.setattr(dp, "_audit", fake_audit)
    return type("W", (), {"state": state, "calls": calls, "audits": audits,
                          "ucs": property(lambda self: [c for c in calls if c.url.host == "ucs.test" and c.url.path.endswith("/authz/check")]),
                          "runner": property(lambda self: [c for c in calls if c.url.host != "ucs.test"])})()


def _call(client, gateway_headers, method, path, roles=ROLES, body=None):
    return client.request(method, path, headers=gateway_headers(user_name="alice", roles=roles),
                          json=body if body is not None else ({} if method in ("POST", "PATCH") else None))


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(get_settings(), k, v)


def test_off_never_asks_ucs(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="off")
    assert _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy").status_code < 400
    assert world.ucs == [] and len(world.runner) == 1


def test_audit_asks_ucs_with_the_product_action_and_context_and_lets_the_action_through(client, gateway_headers, world):
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code < 400 and len(world.runner) == 1
    (req,) = world.ucs
    body = json.loads(req.content)
    assert (body["action"], body["object"]) == ("write", f"inaiera_workspace.flink_job.{JID}")
    ctx = body["context"]
    assert (ctx["product_action"], ctx["environment"], ctx["resource_kind"], ctx["resource_id"]) == ("flink.deploy", "dev", "flink_job", JID)
    assert ctx["route"] == "POST /api/v1/flink/jobs/{job_id}/deploy" and "roles" not in ctx
    assert req.headers["X-User-Name"] == "alice"                      # UCS knows the person by name; the id elsewhere is the subject
    assert "X-User-Roles" not in req.headers
    assert world.audits == []                                         # an allow in audit mode is only logged


def test_audit_records_a_would_be_denial_but_does_not_block(client, gateway_headers, world):
    world.state.update(decision="deny", reason="no matching grant")
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code < 400 and len(world.runner) == 1
    assert world.audits == [("would_deny", "flink.deploy", "flink_job", f"inaiera_workspace.flink_job.{JID}", "no matching grant")]


def test_audit_survives_ucs_being_down_and_stops_asking_for_a_while(client, gateway_headers, world):
    world.state["ucs_down"] = True
    for _ in range(3):
        assert _call(client, gateway_headers, "GET", "/api/v1/flink/jobs").status_code < 400
    assert len([c for c in world.calls if c.url.host == "ucs.test"]) == 1                            # one failed try, then backoff
    assert len(world.runner) == 3                                                                    # no request was refused or delayed


def test_enforce_denial_is_a_403_and_the_runner_is_never_called(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce")
    world.state.update(decision="deny", reason="no matching grant")
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "DATA_POLICY_DENIED" if "detail" in r.json() else r.json()["code"] == "DATA_POLICY_DENIED"
    assert world.runner == []
    assert world.audits and world.audits[0][0] == "deny"


def test_enforce_allow_proceeds(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce")
    assert _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy").status_code < 400
    assert len(world.ucs) == 1 and len(world.runner) == 1


def test_enforce_fails_closed_when_ucs_is_down(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce")
    world.state["ucs_down"] = True
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code == 503 and world.runner == []


def test_enforce_fails_closed_when_ucs_is_not_configured(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce", ucs_service_url=None)
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert r.status_code == 503 and world.runner == []


def test_enforced_actions_narrow_enforce_the_rest_stay_in_audit(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce", data_policy_enforced_actions=["flink.deploy"])
    world.state.update(decision="deny", reason="no matching grant")
    assert _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy").status_code == 403
    assert _call(client, gateway_headers, "GET", "/api/v1/flink/jobs").status_code < 400        # flink.job.read: audit only
    assert any(a[0] == "would_deny" and a[1] == "flink.job.read" for a in world.audits)


def test_rbac_is_the_first_gate_ucs_is_not_asked_when_the_role_is_refused(client, gateway_headers, world, monkeypatch):
    _set(monkeypatch, data_policy_mode="enforce")
    r = _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy", roles=["analyst"])
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"
    assert world.calls == []


def test_ucs_is_asked_once_per_request_even_with_several_capability_dependencies(client, gateway_headers, world):
    _call(client, gateway_headers, "POST", f"/api/v1/flink/jobs/{JID}/deploy")
    assert len(world.ucs) == 1


def test_a_dq_binding_carries_its_target_object_and_its_classification(client, gateway_headers, world):
    r = _call(client, gateway_headers, "POST", "/api/v1/dq/bindings", body={"ucs_fqn": "pg.public.customers", "job_type": "dbt_project"})
    assert r.status_code < 400
    body = json.loads(world.ucs[0].content)
    assert body["object"] == "pg.public.customers" and body["action"] == "write"
    ctx = body["context"]
    assert (ctx["product_action"], ctx["target_data_object"], ctx["classification"]) == ("dq.publish", "pg.public.customers", ["class.email"])


def test_a_git_resource_has_its_owner_in_the_context(gateway_headers, world):
    from fastapi.testclient import TestClient

    from app.main import app
    wid = str(uuid.uuid4())
    # the handler itself needs a database this unit test does not have; only what the gate sent to UCS matters here
    TestClient(app, raise_server_exceptions=False).get(f"/api/v1/git/workspaces/{wid}/status",
                                                         headers=gateway_headers(user_name="alice", roles=ROLES))
    ctx = json.loads(world.ucs[0].content)["context"]
    assert (ctx["product_action"], ctx["resource_owner"]) == ("git.repo.read", subject_for("alice"))


def test_exempt_routes_do_not_ask_ucs(client, gateway_headers, world):
    client.post("/api/v1/git/webhooks/github", json={}, headers=gateway_headers(user_name="alice", roles=ROLES))
    assert world.ucs == []


def test_a_classification_failure_never_blocks_and_leaves_it_unknown(client, gateway_headers, world, monkeypatch):
    world.state["classification"] = []
    r = _call(client, gateway_headers, "POST", "/api/v1/dq/bindings", body={"ucs_fqn": "pg.public.customers"})
    assert r.status_code < 400
    assert json.loads(world.ucs[0].content)["context"]["classification"] == []


def test_defaults_are_audit_and_a_pseudo_catalog():
    s = get_settings().__class__()
    assert s.data_policy_mode == "audit" and s.data_policy_enforced_actions == [] and s.data_policy_catalog == "inaiera_workspace"


def test_off_is_honoured_by_evaluate_itself_not_only_by_the_route_wrapper(world, monkeypatch):
    import asyncio
    import types

    _set(monkeypatch, data_policy_mode="off")
    p = types.SimpleNamespace(user_id=subject_for("alice"), org_id="o", roles=ROLES, email="alice", username="alice")
    asyncio.run(dp.evaluate(p, None, action="flink.deploy", kind="flink_job", resource_id=JID, object_name=None, route="POST x"))
    assert world.calls == []


@pytest.mark.integration
def test_a_denial_is_audited_even_though_the_request_is_rolled_back(world, monkeypatch):
    """The live scenario found it: the audit row was written in the request's own transaction, which a raised 403 rolls back."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models import AuditLog

    monkeypatch.undo()                      # use the real _audit, not the fake the `world` fixture installed
    s = get_settings()
    for attr, value in {"ucs_service_url": "http://ucs.test", "ucs_gateway_secret": "g", "environment": "dev",
                        "data_policy_mode": "enforce", "data_policy_enforced_actions": [],
                        "flinksql_delegation_jwt_secret": "f" * 40, "flinksql_runner_url": "http://flink.test"}.items():
        monkeypatch.setattr(s, attr, value)
    dp.reset_backoff()
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"decision": "deny", "reason": "no matching grant"})), **kw))
    from fastapi.testclient import TestClient

    from app.main import app
    r = TestClient(app, raise_server_exceptions=False).post(f"/api/v1/flink/jobs/{JID}/deploy", json={},
                                                            headers=_headers(user_name="audit-alice", roles=ROLES))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "DATA_POLICY_DENIED"

    async def rows():
        engine = create_async_engine(s.database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                found = (await session.execute(select(AuditLog).where(AuditLog.action == "data_policy.deny")
                                               .where(AuditLog.metadata_["object"].astext == f"inaiera_workspace.flink_job.{JID}"))).scalars().all()
                return [(a.action, a.metadata_["product_action"], a.metadata_["object"]) for a in found]
        finally:
            await engine.dispose()

    assert asyncio.run(rows()) == [("data_policy.deny", "flink.deploy", f"inaiera_workspace.flink_job.{JID}")]
