import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.errors import PipelineVersionConflictError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.pipeline_service import pipeline_service


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "connection_config": {}})
    return template, connection


def _seed_sink_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    """A SINK template: the only kind of connector Kafka Connect gives a dead-letter queue."""
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSinkConnector", "connector_type": "sink", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "connection_config": {}})
    return template, connection


def _sink_payload(template: dict, connection: dict, **overrides) -> PipelineCreate:
    return PipelineCreate(
        name="Orders sink", template_id=template["id"], connection_profile_id=connection["id"],
        zone="prepared", domain="sales", source_system="orders_app", entity="orders_copy", mode="batch", version="v1",
        connector_config={"topics": f"{connection['org_id']}-raw-sales-orders_app-orders-batch-v1"}, **overrides,
    )


async def _create_pipeline(org_id: str, template: dict, connection: dict) -> dict:
    payload = PipelineCreate(
        name="Orders CDC",
        template_id=template["id"],
        connection_profile_id=connection["id"],
        zone="raw",
        domain="sales",
        source_system="orders_app",
        entity="orders",
        mode="cdc",
        version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


@pytest.mark.asyncio
async def test_update_pipeline_changes_only_requested_field(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_1")
    created = await _create_pipeline("org_upd_1", template, connection)

    updated = await pipeline_service.update_pipeline(
        "org_upd_1", created["id"], PipelineUpdate(name="Renamed Pipeline")
    )

    assert updated["name"] == "Renamed Pipeline"
    assert updated["status"] == created["status"]
    assert updated["topic_name"] == created["topic_name"]
    assert updated["connector_name"] == created["connector_name"]
    assert updated["zone"] == created["zone"]
    assert updated["domain"] == created["domain"]


@pytest.mark.asyncio
async def test_update_pipeline_never_resets_runtime_status(fake_postgres) -> None:
    """The bug this fix closes: PUT used to rebuild the row from
    PipelineCreate (status defaults to "draft"), so any edit silently reset
    a running pipeline's status back to "draft"."""
    template, connection = _seed_org(fake_postgres, "org_upd_2")
    created = await _create_pipeline("org_upd_2", template, connection)
    # Simulate a pipeline that has actually been deployed and is running.
    fake_postgres.pipelines[created["id"]].status = "running"

    updated = await pipeline_service.update_pipeline(
        "org_upd_2", created["id"], PipelineUpdate(description="just a note")
    )

    assert updated["status"] == "running"
    assert updated["description"] == "just a note"


@pytest.mark.asyncio
async def test_update_pipeline_with_no_fields_is_a_no_op(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_3")
    created = await _create_pipeline("org_upd_3", template, connection)
    fake_postgres.pipelines[created["id"]].status = "paused"

    updated = await pipeline_service.update_pipeline("org_upd_3", created["id"], PipelineUpdate())

    assert updated["status"] == "paused"
    assert updated == await pipeline_service.get_or_404("org_upd_3", created["id"])


def test_update_pipeline_rejects_identity_fields() -> None:
    """zone/domain/source_system/entity/mode/version determine the pipeline's
    topic/connector identity and must be impossible to change via update."""
    with pytest.raises(ValidationError):
        PipelineUpdate(zone="curated")

    with pytest.raises(ValidationError):
        PipelineUpdate(status="running")


@pytest.mark.asyncio
async def test_update_pipeline_rejects_cross_org_reference(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_4")
    created = await _create_pipeline("org_upd_4", template, connection)
    other_orgs_connection = fake_postgres.create(
        "connection_profiles", {"org_id": "org_upd_5", "connection_config": {}}
    )

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.update_pipeline(
            "org_upd_4",
            created["id"],
            PipelineUpdate(connection_profile_id=other_orgs_connection["id"]),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_update_pipeline_refreshes_generated_config_preview_for_changed_spec(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_6")
    created = await _create_pipeline("org_upd_6", template, connection)

    updated = await pipeline_service.update_pipeline(
        "org_upd_6", created["id"], PipelineUpdate(connector_config={"tasks.max": "4"})
    )

    assert updated["generated_config"]["tasks.max"] == "4"
    # Identity is unaffected by a spec-only change.
    assert updated["topic_name"] == created["topic_name"]
    assert updated["connector_name"] == created["connector_name"]


# ── GAP-04: error_handling_policy persists through create/update ────────────

@pytest.mark.asyncio
async def test_a_source_pipeline_defaults_to_skip_and_log_with_no_dead_letter_queue(fake_postgres) -> None:
    """Kafka Connect gives a source connector no dead-letter queue, so its default can't be DLQ_AND_CONTINUE."""
    template, connection = _seed_org(fake_postgres, "org_upd_7")
    created = await _create_pipeline("org_upd_7", template, connection)

    assert created["error_handling_policy"] == "SKIP_AND_LOG"
    assert created["generated_config"]["errors.tolerance"] == "all"
    assert "errors.deadletterqueue.topic.name" not in created["generated_config"]


@pytest.mark.asyncio
async def test_a_sink_pipeline_defaults_to_dlq_and_continue(fake_postgres) -> None:
    template, connection = _seed_sink_org(fake_postgres, "org_upd_7b")

    created = await pipeline_service.create_pipeline("org_upd_7b", _sink_payload(template, connection))

    assert created["error_handling_policy"] == "DLQ_AND_CONTINUE"
    assert created["generated_config"]["errors.tolerance"] == "all"
    assert created["generated_config"]["errors.deadletterqueue.topic.name"].endswith("-connector-v1")


@pytest.mark.asyncio
async def test_create_pipeline_with_threshold_policy_persists_its_defaults(fake_postgres) -> None:
    template, connection = _seed_sink_org(fake_postgres, "org_upd_8")

    created = await pipeline_service.create_pipeline(
        "org_upd_8", _sink_payload(template, connection, error_handling_policy="THRESHOLD"),
    )

    assert created["error_handling_policy"] == "THRESHOLD"
    assert created["error_threshold_max_errors"] == 100
    assert created["error_threshold_window_seconds"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["DLQ_AND_CONTINUE", "THRESHOLD"])
async def test_a_source_pipeline_cannot_choose_a_dead_letter_policy(fake_postgres, policy) -> None:
    from app.core.errors import ValidationError

    template, connection = _seed_org(fake_postgres, "org_upd_src_dlq")
    payload = PipelineCreate(
        name="Orders", template_id=template["id"], connection_profile_id=connection["id"], zone="raw", domain="sales",
        source_system="orders_app", entity="orders", mode="cdc", version="v1", error_handling_policy=policy,
    )

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_upd_src_dlq", payload)

    assert "only provides one for SINK connectors" in str(exc_info.value)
    assert exc_info.value.field_errors[0].field == "error_handling_policy"
    assert fake_postgres.pipelines == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["FAIL_FAST", "SKIP_AND_LOG"])
async def test_a_source_pipeline_can_choose_fail_fast_or_skip_and_log(fake_postgres, policy) -> None:
    org = f"org_upd_src_{policy.lower()}"
    template, connection = _seed_org(fake_postgres, org)
    payload = PipelineCreate(
        name="Orders", template_id=template["id"], connection_profile_id=connection["id"], zone="raw", domain="sales",
        source_system="orders_app", entity="orders", mode="cdc", version="v1", error_handling_policy=policy,
    )

    created = await pipeline_service.create_pipeline(org, payload)

    assert created["error_handling_policy"] == policy
    assert created["generated_config"]["errors.tolerance"] == ("none" if policy == "FAIL_FAST" else "all")


@pytest.mark.asyncio
async def test_two_dead_letter_sinks_cannot_share_one_dead_letter_topic(fake_postgres) -> None:
    """The DLQ topic name has no zone/mode, so two sinks differing only there would share it and THRESHOLD would
    count the other's errors."""
    from app.core.errors import ValidationError

    template, connection = _seed_sink_org(fake_postgres, "org_upd_shared")
    await pipeline_service.create_pipeline("org_upd_shared", _sink_payload(template, connection))
    clash = _sink_payload(template, connection, name="Second sink") if False else PipelineCreate(
        name="Second sink", template_id=template["id"], connection_profile_id=connection["id"],
        zone="curated", domain="sales", source_system="orders_app", entity="orders_copy", mode="stream", version="v1",
        connector_config={"topics": "raw-sales-orders_app-orders-batch-v1"},
    )

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_upd_shared", clash)

    assert "already uses this dead-letter queue" in str(exc_info.value)
    assert len(fake_postgres.pipelines) == 1


@pytest.mark.asyncio
async def test_a_sink_without_a_dead_letter_policy_may_share_the_name(fake_postgres) -> None:
    template, connection = _seed_sink_org(fake_postgres, "org_upd_shared2")
    await pipeline_service.create_pipeline("org_upd_shared2", _sink_payload(template, connection))
    other = PipelineCreate(
        name="Second sink", template_id=template["id"], connection_profile_id=connection["id"], zone="curated",
        domain="sales", source_system="orders_app", entity="orders_copy", mode="stream", version="v1",
        connector_config={"topics": "raw-sales-orders_app-orders-batch-v1"}, error_handling_policy="FAIL_FAST",
    )

    created = await pipeline_service.create_pipeline("org_upd_shared2", other)

    assert created["error_handling_policy"] == "FAIL_FAST"


@pytest.mark.asyncio
async def test_editing_a_dead_letter_sink_does_not_collide_with_itself(fake_postgres) -> None:
    template, connection = _seed_sink_org(fake_postgres, "org_upd_self")
    created = await pipeline_service.create_pipeline("org_upd_self", _sink_payload(template, connection))

    updated = await pipeline_service.update_pipeline("org_upd_self", created["id"], PipelineUpdate(name="Renamed"))

    assert updated["name"] == "Renamed"


@pytest.mark.asyncio
async def test_update_pipeline_can_switch_to_fail_fast(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_9")
    created = await _create_pipeline("org_upd_9", template, connection)

    updated = await pipeline_service.update_pipeline(
        "org_upd_9", created["id"], PipelineUpdate(error_handling_policy="FAIL_FAST")
    )

    assert updated["error_handling_policy"] == "FAIL_FAST"
    assert updated["generated_config"]["errors.tolerance"] == "none"
    assert "errors.deadletterqueue.topic.name" not in updated["generated_config"]


@pytest.mark.asyncio
async def test_update_pipeline_switching_into_threshold_gets_the_default_params(fake_postgres) -> None:
    """Switching a pipeline INTO THRESHOLD via a PATCH that only sets the
    policy field must still get real (non-null) max_errors/window — not a
    half-configured policy with nothing for the reconciler to enforce."""
    template, connection = _seed_sink_org(fake_postgres, "org_upd_10")
    created = await pipeline_service.create_pipeline(
        "org_upd_10", _sink_payload(template, connection, error_handling_policy="FAIL_FAST"),
    )

    updated = await pipeline_service.update_pipeline(
        "org_upd_10", created["id"], PipelineUpdate(error_handling_policy="THRESHOLD")
    )

    assert updated["error_handling_policy"] == "THRESHOLD"
    assert updated["error_threshold_max_errors"] == 100
    assert updated["error_threshold_window_seconds"] == 300


@pytest.mark.asyncio
async def test_update_pipeline_unrelated_field_never_touches_error_handling_policy(fake_postgres) -> None:
    """The exact GAP-02-shaped bug this codebase already learned to avoid
    for `status` — a routine edit must not silently reset an unrelated
    operational field."""
    template, connection = _seed_org(fake_postgres, "org_upd_11")
    created = await _create_pipeline("org_upd_11", template, connection)
    await pipeline_service.update_pipeline(
        "org_upd_11", created["id"], PipelineUpdate(error_handling_policy="FAIL_FAST")
    )

    renamed = await pipeline_service.update_pipeline(
        "org_upd_11", created["id"], PipelineUpdate(name="Renamed")
    )

    assert renamed["name"] == "Renamed"
    assert renamed["error_handling_policy"] == "FAIL_FAST"


# ── FE-BUG-14: optimistic concurrency ───────────────────────────────────────

@pytest.mark.asyncio
async def test_created_pipeline_starts_at_revision_one(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_12")
    created = await _create_pipeline("org_upd_12", template, connection)

    assert created["revision"] == 1


@pytest.mark.asyncio
async def test_update_pipeline_bumps_revision_only_on_an_actually_applied_change(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_13")
    created = await _create_pipeline("org_upd_13", template, connection)

    updated = await pipeline_service.update_pipeline(
        "org_upd_13", created["id"], PipelineUpdate(name="Renamed")
    )
    assert updated["revision"] == 2

    # An empty patch (no fields set) is a no-op — nothing to conflict over,
    # nothing to bump either.
    unchanged = await pipeline_service.update_pipeline(
        "org_upd_13", created["id"], PipelineUpdate()
    )
    assert unchanged["revision"] == 2


@pytest.mark.asyncio
async def test_update_pipeline_with_no_expected_revision_keeps_last_write_wins(fake_postgres) -> None:
    """expected_revision is opt-in — a caller that never sends it (today's
    only caller, before the frontend is wired up) gets the exact same
    unconditional-overwrite behavior as before this ticket."""
    template, connection = _seed_org(fake_postgres, "org_upd_14")
    created = await _create_pipeline("org_upd_14", template, connection)
    await pipeline_service.update_pipeline("org_upd_14", created["id"], PipelineUpdate(name="First edit"))

    updated = await pipeline_service.update_pipeline(
        "org_upd_14", created["id"], PipelineUpdate(name="Second edit")
    )

    assert updated["name"] == "Second edit"


@pytest.mark.asyncio
async def test_update_pipeline_with_current_expected_revision_succeeds(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_upd_15")
    created = await _create_pipeline("org_upd_15", template, connection)
    assert created["revision"] == 1

    updated = await pipeline_service.update_pipeline(
        "org_upd_15", created["id"], PipelineUpdate(name="Renamed", expected_revision=1)
    )

    assert updated["name"] == "Renamed"
    assert updated["revision"] == 2


@pytest.mark.asyncio
async def test_update_pipeline_with_stale_expected_revision_raises_conflict(fake_postgres) -> None:
    """The core FE-BUG-14 scenario: Integrator A opens the pipeline
    (revision 1), Integrator B edits + saves first (now revision 2), then
    A's save — still carrying the revision-1 view — must be rejected, not
    silently applied over B's change."""
    template, connection = _seed_org(fake_postgres, "org_upd_16")
    created = await _create_pipeline("org_upd_16", template, connection)

    # Integrator B edits and saves first.
    b_saved = await pipeline_service.update_pipeline(
        "org_upd_16", created["id"], PipelineUpdate(name="B's name", expected_revision=1)
    )
    assert b_saved["revision"] == 2

    # Integrator A, still holding the pre-edit revision, tries to save.
    with pytest.raises(PipelineVersionConflictError) as exc_info:
        await pipeline_service.update_pipeline(
            "org_upd_16", created["id"], PipelineUpdate(name="A's name", expected_revision=1)
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "PIPELINE_VERSION_CONFLICT"
    assert exc_info.value.retryable is False
    # The latest state is handed back in the error itself — the frontend
    # can render "what changed"/offer a reload without a second round trip.
    assert exc_info.value.details["current"]["name"] == "B's name"
    assert exc_info.value.details["current"]["revision"] == 2

    # And B's change is genuinely intact — A's conflicting request never
    # touched the row.
    reloaded = await pipeline_service.get_or_404("org_upd_16", created["id"])
    assert reloaded["name"] == "B's name"
    assert reloaded["revision"] == 2


@pytest.mark.asyncio
async def test_update_pipeline_stale_revision_conflicts_even_with_no_other_field_changes(fake_postgres) -> None:
    """A patch that carries ONLY a stale expected_revision (no other field
    set) still has to conflict — it must not be treated as a no-op just
    because `changes` is otherwise empty."""
    template, connection = _seed_org(fake_postgres, "org_upd_17")
    created = await _create_pipeline("org_upd_17", template, connection)
    await pipeline_service.update_pipeline(
        "org_upd_17", created["id"], PipelineUpdate(name="B's edit", expected_revision=1)
    )

    with pytest.raises(PipelineVersionConflictError):
        await pipeline_service.update_pipeline(
            "org_upd_17", created["id"], PipelineUpdate(expected_revision=1)
        )
