"""
FE-BUG-24: PipelineService.check_deploy_readiness backs the "Ready to
deploy" review screen — a pure, read-only preflight the UI calls BEFORE
Deploy is ever clicked, distinct from run_deploy_preflight (which only runs
as part of an actual deploy() attempt, and only covers Kafka-Connect-worker
compatibility, not connection/topic/schema/transform/quality/secret
validity — see deploy_preflight.py and pipeline_service.py's own
docstrings).

settings.runtime_deploy_enabled is off by default in tests (matching
production's off-by-default posture), which is exactly what lets most of
these tests avoid mocking a live Kafka Connect worker at all — only the
tests that specifically exercise the connector_plugin_installed/
connector_compatibility checks turn it on, the same way
test_deploy_preflight_integration.py does.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.schemas.pipeline import PipelineCreate
from app.services.connection_service import connection_service
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient


def _seed_org(fake_postgres, org_id: str, **template_overrides) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {
            "connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source",
            "default_config": {}, **template_overrides,
        },
    )
    connection = fake_postgres.create(
        "connection_profiles",
        {"org_id": org_id, "name": "Test Connection", "connection_config": {}, "verification_status": "ok"},
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict, **overrides) -> dict:
    payload = PipelineCreate(
        name=overrides.pop("name", "Orders CDC"), template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders",
        mode=overrides.pop("mode", "cdc"), version="v1", **overrides,
    )
    return await pipeline_service.create_pipeline(org_id, payload)


def _by_key(result: dict, key: str) -> dict:
    return next(c for c in result["checks"] if c["key"] == key)


@pytest.mark.asyncio
async def test_a_fully_healthy_pipeline_is_ready(fake_postgres) -> None:
    """Baseline: a template with unrestricted supported_modes (no live
    Kafka Connect call needed, runtime_deploy_enabled is off by default),
    a verified connection, no schema/transform/quality/secret attached —
    every check passes."""
    template, connection = _seed_org(fake_postgres, "org_ready_1")
    created = await _create_pipeline("org_ready_1", template, connection)

    result = await pipeline_service.check_deploy_readiness("org_ready_1", created["id"])

    assert result["ready"] is True
    assert result["topic_name"] == created["topic_name"]
    assert all(c["ok"] for c in result["checks"])
    assert all(c["reason"] is None for c in result["checks"])


@pytest.mark.asyncio
async def test_unverified_connection_fails_connection_verified_only(fake_postgres) -> None:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": "org_ready_2", "name": "c", "connection_config": {}, "verification_status": "error"},
    )
    created = await _create_pipeline("org_ready_2", template, connection)

    result = await pipeline_service.check_deploy_readiness("org_ready_2", created["id"])

    assert result["ready"] is False
    check = _by_key(result, "connection_verified")
    assert check["ok"] is False
    assert "error" in check["reason"]


@pytest.mark.asyncio
async def test_a_never_tested_connection_fails_with_a_clear_reason(fake_postgres, monkeypatch) -> None:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_ready_3", "name": "c", "connection_config": {}})
    created = await _create_pipeline("org_ready_3", template, connection)

    async def _probe_leaves_it_untested(org_id, connection_id):
        return {"status": "error", "message": "unreachable", "details": {}}

    monkeypatch.setattr(connection_service, "probe_and_record", _probe_leaves_it_untested)

    result = await pipeline_service.check_deploy_readiness("org_ready_3", created["id"])

    check = _by_key(result, "connection_verified")
    assert check["ok"] is False
    assert "never been tested" in check["reason"]


def _fake_probe_that(monkeypatch, fake_postgres, *, becomes: str):
    """A probe_and_record stand-in that records the call and persists `becomes` as the new verification."""
    calls: list[str] = []

    async def _probe(org_id, connection_id):
        calls.append(connection_id)
        fake_postgres.connection_verifications[connection_id] = _VerificationRow(
            connection_id, org_id, becomes, datetime.now(UTC),
        )
        return {"status": "ok" if becomes == "ok" else "error", "message": "", "details": {}}

    monkeypatch.setattr(connection_service, "probe_and_record", _probe)
    return calls


def _VerificationRow(connection_id, org_id, status, tested_at):  # noqa: N802 - mirrors a row class
    from tests.conftest import _FakeRow

    return _FakeRow({
        "connection_id": connection_id, "org_id": org_id, "status": status, "tested_at": tested_at,
        "message": "", "latency_ms": None, "error_code": None if status == "ok" else "PROBE_FAILED",
        "verified_revision": 1,
    })


@pytest.mark.asyncio
async def test_a_connection_that_was_fixed_since_its_last_failed_test_is_retested_and_passes(fake_postgres, monkeypatch) -> None:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": "org_ready_fix", "name": "c", "connection_config": {}, "verification_status": "error"},
    )
    created = await _create_pipeline("org_ready_fix", template, connection)
    calls = _fake_probe_that(monkeypatch, fake_postgres, becomes="ok")

    result = await pipeline_service.check_deploy_readiness("org_ready_fix", created["id"])

    assert calls == [connection["id"]]
    assert _by_key(result, "connection_verified")["ok"] is True
    assert result["ready"] is True


@pytest.mark.asyncio
async def test_a_long_stale_pass_is_retested_but_a_fresh_one_is_trusted(fake_postgres, monkeypatch) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_age")
    created = await _create_pipeline("org_ready_age", template, connection)
    calls = _fake_probe_that(monkeypatch, fake_postgres, becomes="error")
    verification = fake_postgres.connection_verifications[connection["id"]]

    verification.tested_at = datetime.now(UTC) - timedelta(minutes=1)
    fresh = await pipeline_service.check_deploy_readiness("org_ready_age", created["id"])
    assert calls == []  # tested a minute ago and passed: no live probe
    assert _by_key(fresh, "connection_verified")["ok"] is True

    verification.status = "ok"
    verification.tested_at = datetime.now(UTC) - timedelta(hours=2)
    stale = await pipeline_service.check_deploy_readiness("org_ready_age", created["id"])
    assert calls == [connection["id"]]  # two hours old: re-tested, and the source has since gone down
    assert _by_key(stale, "connection_verified")["ok"] is False


@pytest.mark.asyncio
async def test_two_pipelines_sharing_a_topic_name_both_fail_topic_name_available(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_4")
    first = await _create_pipeline("org_ready_4", template, connection, name="Orders A")
    second = await _create_pipeline("org_ready_4", template, connection, name="Orders B")
    # Force a genuine collision — same identity fields produce the same
    # generated topic_name, so a second pipeline built against an identical
    # contract naturally collides without needing to hand-edit topic_name.
    assert first["topic_name"] == second["topic_name"]

    result = await pipeline_service.check_deploy_readiness("org_ready_4", second["id"])

    check = _by_key(result, "topic_name_available")
    assert check["ok"] is False
    assert "Orders A" in check["reason"]
    # The other pipeline isn't penalized by a collision that's only
    # reported from its own perspective.
    result_first = await pipeline_service.check_deploy_readiness("org_ready_4", first["id"])
    assert _by_key(result_first, "topic_name_available")["ok"] is False
    assert "Orders B" in _by_key(result_first, "topic_name_available")["reason"]


@pytest.mark.asyncio
async def test_an_invalid_transform_step_fails_transform_configuration_valid(fake_postgres) -> None:
    """A transform this invalid can't actually be attached at pipeline
    creation (generate_pipeline_config validates it immediately) — the real
    case this check exists for is a transform policy edited to become
    invalid AFTER a pipeline already links to it, since nothing re-validates
    it at deploy time today. Simulated the same way
    test_deploy_preflight_integration.py simulates a template narrowed
    after creation: mutate the fake table directly, bypassing the service's
    own edit-time validation on purpose."""
    template, connection = _seed_org(fake_postgres, "org_ready_5")
    transform = fake_postgres.create(
        "transform_policies",
        {"org_id": "org_ready_5", "name": "policy", "transforms": [{"type": "MaskField", "name": "mask_email", "scope": "value"}]},
    )
    created = await _create_pipeline("org_ready_5", template, connection, transform_policy_id=transform["id"])
    fake_postgres.transform_policies[transform["id"]].transforms = [{"type": "NotARealSMT", "name": "x"}]

    result = await pipeline_service.check_deploy_readiness("org_ready_5", created["id"])

    check = _by_key(result, "transform_configuration_valid")
    assert check["ok"] is False
    assert "Unsupported SMT type" in check["reason"]


@pytest.mark.asyncio
async def test_a_valid_transform_policy_passes(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_6")
    transform = fake_postgres.create(
        "transform_policies",
        {"org_id": "org_ready_6", "name": "good-policy", "transforms": [{"type": "MaskField", "name": "mask_email", "scope": "value"}]},
    )
    created = await _create_pipeline("org_ready_6", template, connection, transform_policy_id=transform["id"])

    result = await pipeline_service.check_deploy_readiness("org_ready_6", created["id"])

    assert _by_key(result, "transform_configuration_valid")["ok"] is True


@pytest.mark.asyncio
async def test_an_invalid_quality_policy_fails_quality_policy_valid(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_7")
    quality = fake_postgres.create(
        "quality_policies",
        {"org_id": "org_ready_7", "name": "bad-quality", "quarantine_sink": "not_a_real_sink", "rules": []},
    )
    created = await _create_pipeline("org_ready_7", template, connection, quality_policy_id=quality["id"])

    result = await pipeline_service.check_deploy_readiness("org_ready_7", created["id"])

    check = _by_key(result, "quality_policy_valid")
    assert check["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["block", "drop"])
async def test_a_legacy_policy_with_an_unavailable_failure_action_fails_readiness_and_says_why(fake_postgres, action) -> None:
    """NEW-09: `block` and `drop` were never implemented (they behaved as `log`). A policy that still holds one is reported with the capability's own
    explanation, so the user is told what to change instead of deploying a pipeline whose policy does not do what its name says."""
    template, connection = _seed_org(fake_postgres, f"org_ready_{action}")
    quality = fake_postgres.create(
        "quality_policies", {"org_id": f"org_ready_{action}", "name": "legacy", "on_failure": action, "quarantine_sink": "kafka", "rules": []},
    )
    created = await _create_pipeline(f"org_ready_{action}", template, connection, quality_policy_id=quality["id"])

    result = await pipeline_service.check_deploy_readiness(f"org_ready_{action}", created["id"])

    check = _by_key(result, "quality_policy_valid")
    assert check["ok"] is False
    assert f"on_failure='{action}' is not available" in check["reason"]
    assert "not implemented" in check["reason"]


@pytest.mark.asyncio
async def test_a_supported_failure_action_passes_quality_policy_valid(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_ok")
    quality = fake_postgres.create(
        "quality_policies", {"org_id": "org_ready_ok", "name": "fine", "on_failure": "log", "quarantine_sink": "kafka", "rules": []},
    )
    created = await _create_pipeline("org_ready_ok", template, connection, quality_policy_id=quality["id"])

    assert _by_key(await pipeline_service.check_deploy_readiness("org_ready_ok", created["id"]), "quality_policy_valid")["ok"] is True


@pytest.mark.asyncio
async def test_a_secret_ref_that_does_not_resolve_fails_secret_reference_resolved(fake_postgres) -> None:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles",
        {
            "org_id": "org_ready_8", "name": "c", "connection_config": {}, "verification_status": "ok",
            "secret_ref": "vault://dataconnect/org_ready_8/does-not-exist",
        },
    )
    created = await _create_pipeline("org_ready_8", template, connection)

    result = await pipeline_service.check_deploy_readiness("org_ready_8", created["id"])

    check = _by_key(result, "secret_reference_resolved")
    assert check["ok"] is False


@pytest.mark.asyncio
async def test_a_missing_schema_version_fails_schema_valid(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_9")
    created = await _create_pipeline("org_ready_9", template, connection)
    # A schema_version_id that doesn't resolve — same shape PipelineUpdate
    # would produce for a deleted/stale schema link.
    from app.services.pipeline_service import pipeline_service as svc

    await svc._persist_pipeline_fields("org_ready_9", created["id"], {"schema_version_id": "does-not-exist"})

    result = await pipeline_service.check_deploy_readiness("org_ready_9", created["id"])

    check = _by_key(result, "schema_valid")
    assert check["ok"] is False
    assert "not be found" in check["reason"]


@pytest.mark.asyncio
async def test_mode_the_template_does_not_support_is_caught_by_connector_compatibility(fake_postgres, monkeypatch) -> None:
    """Same underlying check as FE-BUG-23's create-time rejection — this
    proves it's ALSO surfaced on the readiness screen (via
    deploy_preflight.collect_preflight_failures, reused rather than
    reimplemented), not just enforced silently at creation."""
    template, connection = _seed_org(fake_postgres, "org_ready_10", supported_modes=["cdc"])
    created = await _create_pipeline("org_ready_10", template, connection, mode="cdc")
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)
    # Simulate the template being narrowed after this pipeline was created
    # (same technique test_deploy_preflight_integration.py uses) — proves
    # this check reads the pipeline's pinned template_snapshot, same as
    # deploy() itself does, not the live template.
    fake_postgres.pipelines[created["id"]].template_snapshot["supported_modes"] = ["batch"]

    result = await pipeline_service.check_deploy_readiness("org_ready_10", created["id"])

    check = _by_key(result, "connector_compatibility")
    assert check["ok"] is False
    assert "cdc" in check["reason"]
    # The plugin itself IS installed — only the mode failed — so that
    # check must stay independently true, not lumped into one failure.
    assert _by_key(result, "connector_plugin_installed")["ok"] is True


@pytest.mark.asyncio
async def test_an_uninstalled_connector_plugin_fails_only_that_check(fake_postgres, monkeypatch) -> None:
    template, connection = _seed_org(fake_postgres, "org_ready_11")
    created = await _create_pipeline("org_ready_11", template, connection)
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    async def _fake_list_connector_plugins(self):
        return []  # worker has nothing installed

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    result = await pipeline_service.check_deploy_readiness("org_ready_11", created["id"])

    assert result["ready"] is False
    assert _by_key(result, "connector_plugin_installed")["ok"] is False


@pytest.mark.asyncio
async def test_readiness_never_provisions_a_topic_or_deploys_a_connector(fake_postgres, monkeypatch) -> None:
    """The whole point of this being a review screen rather than deploy()
    itself: calling it must have zero side effects."""
    template, connection = _seed_org(fake_postgres, "org_ready_12")
    created = await _create_pipeline("org_ready_12", template, connection)
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(settings, "topic_admin_provisioning_enabled", True)

    calls: list[str] = []

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    async def _tracking_deploy_connector(self, connector_name, config):
        calls.append("deploy_connector")
        return {"config": config}

    async def _tracking_ensure_topic(topic, resolved_config):
        calls.append("ensure_topic")

    async def _tracking_record_desired(*args, **kwargs):
        calls.append("record_desired")

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _tracking_deploy_connector)
    monkeypatch.setattr("app.services.kafka_admin.ensure_topic", _tracking_ensure_topic)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _tracking_record_desired)

    result = await pipeline_service.check_deploy_readiness("org_ready_12", created["id"])

    assert result["ready"] is True
    assert calls == []
    assert (await pipeline_service.get_or_404("org_ready_12", created["id"]))["status"] == "draft"


@pytest.mark.asyncio
async def test_check_deploy_readiness_missing_pipeline_is_404(fake_postgres) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.check_deploy_readiness("org_ready_13", "does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_a_connection_edited_since_its_last_test_is_retested_and_fails_until_the_test_covers_the_edit(fake_postgres, monkeypatch) -> None:
    """GAP-02: a green 'verified' that was earned by the OLD host must not wave a deploy through."""
    template, connection = _seed_org(fake_postgres, "org_ready_edit")  # starts verified "ok" at revision 1
    created = await _create_pipeline("org_ready_edit", template, connection)
    fake_postgres.connection_profiles[connection["id"]].config_revision = 2  # the user edited the host since
    probes: list[str] = []

    async def _probe_that_does_not_cover_the_edit(org_id, connection_id):
        probes.append(connection_id)  # leaves the stored result at revision 1, i.e. still stale
        return {"status": "ok", "message": "", "details": {}}

    monkeypatch.setattr(connection_service, "probe_and_record", _probe_that_does_not_cover_the_edit)

    result = await pipeline_service.check_deploy_readiness("org_ready_edit", created["id"])

    assert probes == [connection["id"]]  # a stale result is not trusted: it re-tests
    check = _by_key(result, "connection_verified")
    assert check["ok"] is False
    assert "changed after it was last tested" in check["reason"]
