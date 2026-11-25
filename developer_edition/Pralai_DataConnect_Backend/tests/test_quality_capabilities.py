"""NEW-09 — quality policy capabilities: which quarantine destinations and failure actions can actually be used, as machine-readable availability.

Before, the API accepted any `on_failure` string (so `drop` and `block`, which nothing implements, were accepted and silently behaved like `log`), and
the UI offered them. Now one catalogue says what each option is — supported, requires_configuration or unavailable, with a stable reason code — the
validator refuses an unavailable one with that code, and GET /quality-policies/capabilities hands the same answers to the UI.
"""
from unittest import mock

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS, gateway_headers
from fastapi.testclient import TestClient

from app.core.capability import CapabilityState
from app.main import app
from app.models.quality import QualityPolicy
from app.services import quality_capabilities as caps
from app.services.quality import (
    QUARANTINE_SINKS,
    validate_quality_policy,
    validate_quality_policy_field_codes,
    validate_quality_policy_fields,
)

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
PATH = "/api/v1/dataconnect/quality-policies"


async def _no_audit(**kwargs) -> None:
    return None


@pytest.fixture(autouse=True)
def _audit_is_not_under_test(monkeypatch):
    monkeypatch.setattr("app.api.routes.quality.record_audit", _no_audit)


def _by_id(options):
    return {o.id: o.availability for o in options}


# ── The catalogue ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_quarantine_destinations_say_what_works_and_what_does_not() -> None:
    sinks = _by_id(caps.quarantine_sinks())

    assert sinks["kafka"].state is CapabilityState.SUPPORTED
    assert sinks["object_storage"].state is CapabilityState.UNAVAILABLE
    assert sinks["object_storage"].code == "NOT_IMPLEMENTED"
    assert "future backend" in sinks["object_storage"].message
    assert sinks["object_storage"].remediation


def test_the_failure_actions_say_what_works_and_what_does_not() -> None:
    actions = _by_id(caps.failure_actions())

    assert actions["route_to_quarantine"].state is CapabilityState.SUPPORTED
    assert actions["log"].state is CapabilityState.SUPPORTED
    for unavailable in ("drop", "block"):
        assert actions[unavailable].state is CapabilityState.UNAVAILABLE
        assert actions[unavailable].code == "NOT_IMPLEMENTED"
        assert actions[unavailable].remediation


def test_a_supported_option_has_no_reason_code_and_every_unusable_one_has_one() -> None:
    for option in [*caps.quarantine_sinks(), *caps.failure_actions()]:
        if option.availability.state is CapabilityState.SUPPORTED:
            assert option.availability.code is None, option.id
        else:
            assert option.availability.code, option.id
            assert option.availability.message, option.id


def test_the_database_constraint_allows_exactly_the_destinations_that_are_not_unavailable() -> None:
    """ck_dc_quality_policies_quarantine_sink_implemented must move with the catalogue: a destination can be stored only if it can be used."""
    check = next(c for c in QualityPolicy.__table__.constraints if getattr(c, "name", None) == "ck_dc_quality_policies_quarantine_sink_implemented")
    allowed = set(str(check.sqltext).split("(")[1].rstrip(")").replace("'", "").replace(" ", "").split(","))
    usable = {o.id for o in caps.quarantine_sinks() if o.availability.usable}

    assert allowed == usable == QUARANTINE_SINKS


def test_every_option_has_a_label_and_a_description() -> None:
    for option in [*caps.quarantine_sinks(), *caps.failure_actions()]:
        assert option.label
        assert option.description


# ── Requires configuration: implemented, but this deployment is not set up for it ─────────────────────────────────────────

def test_kafka_needs_a_bootstrap_server_for_the_native_producer(monkeypatch) -> None:
    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "native")
    monkeypatch.setattr(caps.settings, "kafka_bootstrap", "  ")

    kafka = _by_id(caps.quarantine_sinks())["kafka"]

    assert kafka.state is CapabilityState.REQUIRES_CONFIGURATION
    assert kafka.code == "KAFKA_BOOTSTRAP_MISSING"
    assert "DATACONNECT_KAFKA_BOOTSTRAP" in kafka.remediation


def test_kafka_needs_the_rest_proxy_url_when_that_backend_is_chosen(monkeypatch) -> None:
    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "rest_proxy")
    monkeypatch.setattr(caps.settings, "kafka_rest_proxy_url", "")

    kafka = _by_id(caps.quarantine_sinks())["kafka"]

    assert (kafka.state, kafka.code) == (CapabilityState.REQUIRES_CONFIGURATION, "KAFKA_REST_PROXY_URL_MISSING")


def test_the_rest_proxy_url_is_not_needed_by_the_native_producer_and_the_bootstrap_not_by_the_proxy(monkeypatch) -> None:
    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "native")
    monkeypatch.setattr(caps.settings, "kafka_bootstrap", "kafka:9092")
    monkeypatch.setattr(caps.settings, "kafka_rest_proxy_url", "")
    assert _by_id(caps.quarantine_sinks())["kafka"].state is CapabilityState.SUPPORTED

    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "rest_proxy")
    monkeypatch.setattr(caps.settings, "kafka_bootstrap", "")
    monkeypatch.setattr(caps.settings, "kafka_rest_proxy_url", "http://proxy:8082")
    assert _by_id(caps.quarantine_sinks())["kafka"].state is CapabilityState.SUPPORTED


def test_routing_to_quarantine_is_exactly_as_available_as_the_kafka_destination_it_uses(monkeypatch) -> None:
    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "native")
    monkeypatch.setattr(caps.settings, "kafka_bootstrap", "")

    route = _by_id(caps.failure_actions())["route_to_quarantine"]
    log = _by_id(caps.failure_actions())["log"]

    assert (route.state, route.code) == (CapabilityState.REQUIRES_CONFIGURATION, "KAFKA_BOOTSTRAP_MISSING")
    assert log.state is CapabilityState.SUPPORTED                          # logging needs nothing configured


# ── Validation refuses what is unavailable, with the code ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("field", "value"), [("quarantine_sink", "object_storage"), ("on_failure", "drop"), ("on_failure", "block")])
def test_an_unavailable_choice_is_refused_with_its_machine_readable_code(field, value) -> None:
    errors = validate_quality_policy_field_codes({field: value, "rules": []})

    assert [(f, c) for f, _m, c in errors] == [(field, "NOT_IMPLEMENTED")]
    assert "not available" in errors[0][1]
    assert errors[0][1].endswith(next(o for o in [*caps.quarantine_sinks(), *caps.failure_actions()] if o.id == value).availability.remediation)


@pytest.mark.parametrize("value", ["route_to_quarantine", "log"])
def test_a_supported_failure_action_is_accepted(value) -> None:
    assert validate_quality_policy_fields({"on_failure": value, "rules": []}) == []


def test_an_unrecognised_value_is_refused_without_a_capability_code() -> None:
    """Unsupported (a typo or an unknown value) is different from unavailable (a known option that cannot run): no code, a plain message."""
    errors = validate_quality_policy_field_codes({"on_failure": "explode", "quarantine_sink": "tape", "rules": []})

    assert sorted((f, c) for f, _m, c in errors) == [("on_failure", None), ("quarantine_sink", None)]
    assert all(m.startswith("Unsupported ") for _f, m, _c in errors)


def test_a_non_text_value_is_refused_not_crashed_on() -> None:
    assert [f for f, _m, _c in validate_quality_policy_field_codes({"on_failure": ["log"], "quarantine_sink": 5, "rules": []})] == ["quarantine_sink", "on_failure"]


def test_a_destination_that_only_needs_configuration_does_not_make_the_policy_invalid(monkeypatch) -> None:
    """The option is implemented; an operator can set it up. Refusing the policy would be refusing something that is not wrong."""
    monkeypatch.setattr(caps.settings, "kafka_producer_backend", "native")
    monkeypatch.setattr(caps.settings, "kafka_bootstrap", "")

    assert validate_quality_policy({"quarantine_sink": "kafka", "on_failure": "route_to_quarantine", "rules": []}) == {"valid": True, "errors": []}


def test_rule_errors_are_still_reported_alongside() -> None:
    errors = validate_quality_policy_field_codes({"on_failure": "block", "rules": [{"name": "r", "type": "nonsense"}]})

    assert [(f, c) for f, _m, c in errors] == [("on_failure", "NOT_IMPLEMENTED"), ("rules[0].type", None)]


# ── The API ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_capabilities_endpoint_returns_every_option_with_its_availability() -> None:
    response = client.get(f"{PATH}/capabilities", headers={"X-Tenant-Id": "org_1"})

    assert response.status_code == 200
    body = response.json()
    assert [o["id"] for o in body["quarantine_sinks"]] == ["kafka", "object_storage"]
    assert [o["id"] for o in body["failure_actions"]] == ["route_to_quarantine", "log", "drop", "block"]
    by_id = {o["id"]: o["availability"] for o in [*body["quarantine_sinks"], *body["failure_actions"]]}
    assert by_id["kafka"]["state"] == "supported"
    assert by_id["object_storage"] == {
        "state": "unavailable", "code": "NOT_IMPLEMENTED", "message": by_id["object_storage"]["message"],
        "remediation": "Use the Kafka quarantine topic.", "verified": True,
    }
    assert {a["state"] for a in by_id.values()} <= {"supported", "requires_configuration", "unavailable"}


def test_capabilities_is_not_mistaken_for_a_policy_id() -> None:
    """GET /quality-policies/{policy_id} would answer 404 for the id "capabilities"."""
    response = client.get(f"{PATH}/capabilities", headers={"X-Tenant-Id": "org_1"})

    assert response.status_code == 200
    assert "quarantine_sinks" in response.json()


def test_a_quality_viewer_can_read_the_capabilities_and_a_caller_without_it_cannot() -> None:
    viewer = gateway_headers(permissions=["dataconnect:quality:view"])
    nothing = gateway_headers(permissions=[])
    assert TestClient(app, headers=viewer).get(f"{PATH}/capabilities", headers={"X-Tenant-Id": "org_1"}).status_code == 200
    assert TestClient(app, headers=nothing).get(f"{PATH}/capabilities", headers={"X-Tenant-Id": "org_1"}).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("on_failure", "block"), ("on_failure", "drop"), ("quarantine_sink", "object_storage")])
async def test_creating_a_policy_with_an_unavailable_choice_is_refused_with_the_code_in_the_response(fake_postgres, field, value) -> None:
    response = client.post(PATH, headers={"X-Tenant-Id": "org_cap"}, json={"name": "p", "rules": [], field: value})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "QUALITY_POLICY_INVALID"
    assert body["field_errors"] == [{"field": field, "message": body["field_errors"][0]["message"], "code": "NOT_IMPLEMENTED"}]


@pytest.mark.asyncio
async def test_updating_a_policy_to_an_unavailable_choice_is_refused_too(fake_postgres) -> None:
    created = client.post(PATH, headers={"X-Tenant-Id": "org_cap"}, json={"name": "p", "rules": [], "on_failure": "log"})
    assert created.status_code == 201

    response = client.put(f"{PATH}/{created.json()['id']}", headers={"X-Tenant-Id": "org_cap"}, json={"name": "p", "rules": [], "on_failure": "block"})

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["code"] == "NOT_IMPLEMENTED"


@pytest.mark.asyncio
async def test_a_supported_choice_is_still_created(fake_postgres) -> None:
    for action in ("route_to_quarantine", "log"):
        assert client.post(PATH, headers={"X-Tenant-Id": "org_cap"}, json={"name": f"p-{action}", "rules": [], "on_failure": action}).status_code == 201


@pytest.mark.asyncio
async def test_the_validate_endpoint_reports_an_unavailable_choice_too(fake_postgres) -> None:
    from app.services.quality_service import quality_service

    async def _get(org_id, policy_id):
        return {"id": policy_id, "org_id": org_id, "name": "legacy", "rules": [], "on_failure": "block", "quarantine_sink": "kafka"}

    with mock.patch.object(quality_service, "get_policy", _get):
        response = client.post(f"{PATH}/p1/validate", headers={"X-Tenant-Id": "org_cap"})

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "block" in response.json()["errors"][0]
