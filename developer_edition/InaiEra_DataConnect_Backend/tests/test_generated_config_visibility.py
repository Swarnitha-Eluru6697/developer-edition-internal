"""BUG-07 (follow-up): a caller without dataconnect:pipelines:deploy must not get a pipeline's generated connector
config through ANY route.

The dedicated GET .../generated-config endpoint requires that permission, but the same data also rides inside the
Pipeline object (list/get/create/update) and the audit log's pipeline snapshots, which pipelines:view / audit:view
holders can read. These tests drive each of those through the real HTTP stack as each seeded role persona
(tests/personas.py).
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import gateway_headers
from fastapi.testclient import TestClient
from gateway_auth import GatewayIdentity, Principal
from personas import PERSONAS

from app.core.response_filters import (
    GENERATED_CONFIG_PERMISSION,
    audit_state_for,
    pipeline_for,
    pipelines_for,
)
from app.main import app
from tests.test_rbac import _async, _role_header

ROLE_SEES = [("admin", True), ("integrator", True), ("viewer", False)]


def _who(persona_or_perms) -> Principal:
    perms = PERSONAS[persona_or_perms] if isinstance(persona_or_perms, str) else frozenset(persona_or_perms)
    return Principal(GatewayIdentity(username="t", permissions=perms))

client = TestClient(app)

CONFIG = {"connector.class": "io.debezium.connector.postgresql.PostgresConnector", "topic.prefix": "org_1001.raw.x"}


def _pipeline(**overrides) -> dict:
    return {
        "id": "p1", "org_id": "org-rbac-test", "name": "Orders", "status": "running", "topic_name": "t",
        "catalog_qualified_name": "t", "connector_name": "c", "direction": "source", "zone": "raw", "domain": "d",
        "source_system": "s", "entity": "e", "mode": "cdc", "version": "v1",
        "template_id": "tpl", "connection_profile_id": "conn", "generated_config": dict(CONFIG), **overrides,
    }


# ── the helpers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("role", "sees"), ROLE_SEES)
def test_pipeline_for_only_shows_the_config_to_roles_that_hold_the_capability(role: str, sees: bool) -> None:
    shown = pipeline_for(_who(role), _pipeline())["generated_config"]

    assert (shown == CONFIG) is sees
    assert shown == ({} if not sees else CONFIG)


def test_the_filter_never_mutates_the_original_and_leaves_other_fields_alone() -> None:
    original = _pipeline()

    filtered = pipeline_for(_who("viewer"), original)

    assert original["generated_config"] == CONFIG  # the stored/audited object is untouched
    assert {k: v for k, v in filtered.items() if k != "generated_config"} == {
        k: v for k, v in original.items() if k != "generated_config"
    }


def test_the_filter_follows_the_permission_not_a_role() -> None:
    assert pipeline_for(_who({"dataconnect:pipelines:view"}), _pipeline())["generated_config"] == {}
    assert pipeline_for(_who({GENERATED_CONFIG_PERMISSION}), _pipeline())["generated_config"] == CONFIG


def test_audit_state_helper_handles_non_pipeline_and_missing_snapshots() -> None:
    assert audit_state_for(_who("viewer"), None) is None
    other = {"name": "a connection", "connection_config": {"hostname": "h"}}
    assert audit_state_for(_who("viewer"), other) == other
    assert audit_state_for(_who("viewer"), _pipeline())["generated_config"] == {}


def test_pipelines_for_a_list() -> None:
    rows = pipelines_for(_who("viewer"), [_pipeline(id="a"), _pipeline(id="b")])
    assert [r["generated_config"] for r in rows] == [{}, {}]
    assert pipelines_for(_who("admin"), [_pipeline()])[0]["generated_config"] == CONFIG


# ── over HTTP ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("role", "sees"), ROLE_SEES)
def test_listing_and_getting_pipelines_over_http(monkeypatch: pytest.MonkeyPatch, role: str, sees: bool) -> None:
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.list_pipelines", lambda *a, **k: _async([_pipeline()]))
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", lambda *a, **k: _async(_pipeline()))
    expected = CONFIG if sees else {}

    listed = client.get("/api/v1/dataconnect/pipelines", headers=_role_header(role))
    fetched = client.get("/api/v1/dataconnect/pipelines/p1", headers=_role_header(role))

    assert listed.status_code == fetched.status_code == 200
    assert listed.json()[0]["generated_config"] == expected
    assert fetched.json()["generated_config"] == expected
    assert fetched.json()["connector_name"] == "c"  # everything else a viewer needs is still there


def test_an_update_response_is_filtered_but_the_audit_row_keeps_the_full_state(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict = {}

    async def _audit(**kwargs) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", lambda *a, **k: _async(_pipeline()))
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.update_pipeline", lambda *a, **k: _async(_pipeline(name="New")))
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _audit)
    # An editor WITHOUT pipelines:deploy: proves the response filter on this route is live.
    editor = gateway_headers(permissions=["dataconnect:pipelines:edit"], tenant_id="org-rbac-test")

    resp = client.patch("/api/v1/dataconnect/pipelines/p1", json={"name": "New"}, headers=editor)

    assert resp.status_code == 200
    assert resp.json()["generated_config"] == {}
    assert recorded["after_state"]["generated_config"] == CONFIG  # the audit trail is not degraded


def _audit_rows():
    state = _pipeline()
    return [SimpleNamespace(
        id="a1", org_id="org-rbac-test", actor="lithish", action="updated", resource_type="pipeline",
        resource_id="p1", before_state=state, after_state=state, created_at=datetime.now(UTC),
    )]


class _FakeSession:
    async def execute(self, _stmt):
        rows = _audit_rows()
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.parametrize(("role", "sees"), ROLE_SEES)
def test_the_audit_log_hides_generated_config_from_roles_without_the_capability(role: str, sees: bool) -> None:
    from app.db.session import get_session

    app.dependency_overrides[get_session] = lambda: _FakeSession()
    try:
        resp = client.get("/api/v1/dataconnect/audit", headers=_role_header(role))
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    entry = resp.json()[0]
    expected = CONFIG if sees else {}
    assert entry["before_state"]["generated_config"] == expected
    assert entry["after_state"]["generated_config"] == expected
    assert entry["after_state"]["name"] == "Orders"  # the rest of the history is still readable
