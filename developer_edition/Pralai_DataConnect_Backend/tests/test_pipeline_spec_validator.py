import pytest
from fastapi import HTTPException

from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_spec_validator import validate_pipeline_references


def _payload(**overrides) -> PipelineCreate:
    base = {
        "name": "p",
        "template_id": "tpl",
        "connection_profile_id": "conn",
        "zone": "raw",
        "domain": "d",
        "source_system": "s",
        "entity": "e",
        "mode": "cdc",
        "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)


@pytest.mark.asyncio
async def test_validate_pipeline_references_accepts_same_org_references(fake_postgres) -> None:
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    transform = fake_postgres.create("transform_policies", {"org_id": "org_a"})
    schema = fake_postgres.create("schema_versions", {"org_id": "org_a"})
    quality = fake_postgres.create("quality_policies", {"org_id": "org_a"})
    topic_policy = fake_postgres.create("topic_policies", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a",
        _payload(
            template_id=template["id"],
            connection_profile_id=connection["id"],
            transform_policy_id=transform["id"],
            schema_version_id=schema["id"],
            quality_policy_id=quality["id"],
            topic_policy_id=topic_policy["id"],
        ),
    )

    assert refs.template["id"] == template["id"]
    assert refs.connection["id"] == connection["id"]
    assert refs.transform["id"] == transform["id"]
    assert refs.schema["id"] == schema["id"]
    assert refs.quality_policy["id"] == quality["id"]
    assert refs.topic_policy["id"] == topic_policy["id"]


@pytest.mark.asyncio
async def test_validate_pipeline_references_none_for_unset_optional_refs(fake_postgres) -> None:
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"])
    )

    assert refs.transform is None
    assert refs.schema is None
    assert refs.quality_policy is None
    assert refs.topic_policy is None


@pytest.mark.asyncio
async def test_validate_pipeline_references_missing_template_is_404(fake_postgres) -> None:
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a", _payload(template_id="missing", connection_profile_id=connection["id"])
        )

    assert exc_info.value.status_code == 404
    assert "Connector template" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_missing_connection_is_404(fake_postgres) -> None:
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a", _payload(template_id=template["id"], connection_profile_id="missing")
        )

    assert exc_info.value.status_code == 404
    assert "Connection profile" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_cross_org_connection(fake_postgres) -> None:
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    other_orgs_connection = fake_postgres.create("connection_profiles", {"org_id": "org_b"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(template_id=template["id"], connection_profile_id=other_orgs_connection["id"]),
        )

    assert exc_info.value.status_code == 404
    assert "Connection profile" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_cross_org_transform_policy(fake_postgres) -> None:
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    other_orgs_transform = fake_postgres.create("transform_policies", {"org_id": "org_b"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"],
                connection_profile_id=connection["id"],
                transform_policy_id=other_orgs_transform["id"],
            ),
        )

    assert exc_info.value.status_code == 404
    assert "Transform policy" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_cross_org_schema(fake_postgres) -> None:
    """The gap this validator was added to close: a schema_version_id
    belonging to a different org must be rejected, not silently accepted."""
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    other_orgs_schema = fake_postgres.create("schema_versions", {"org_id": "org_b"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"],
                connection_profile_id=connection["id"],
                schema_version_id=other_orgs_schema["id"],
            ),
        )

    assert exc_info.value.status_code == 404
    assert "Schema version" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_cross_org_quality_policy(fake_postgres) -> None:
    """The other half of the gap: a quality_policy_id from another org."""
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    other_orgs_policy = fake_postgres.create("quality_policies", {"org_id": "org_b"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"],
                connection_profile_id=connection["id"],
                quality_policy_id=other_orgs_policy["id"],
            ),
        )

    assert exc_info.value.status_code == 404
    assert "Quality policy" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_cross_org_topic_policy(fake_postgres) -> None:
    """A topic_policy_id belonging to another org must 404, same as every
    other org-scoped reference — see app/services/topic_policy.py."""
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    other_orgs_policy = fake_postgres.create("topic_policies", {"org_id": "org_b"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"],
                connection_profile_id=connection["id"],
                topic_policy_id=other_orgs_policy["id"],
            ),
        )

    assert exc_info.value.status_code == 404
    assert "Topic policy" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_stale_transform_id(fake_postgres) -> None:
    """A transform_policy_id that doesn't resolve at all (typo, deleted row)
    must 404 too, not silently fall back to "no transform"."""
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(HTTPException) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"],
                connection_profile_id=connection["id"],
                transform_policy_id="does-not-exist",
            ),
        )

    assert exc_info.value.status_code == 404
    assert "Transform policy" in exc_info.value.detail


@pytest.mark.asyncio
async def test_validate_pipeline_references_resolves_direction_from_the_templates_connector_type(fake_postgres) -> None:
    """GAP-12: refs.direction is derived here, once, from the resolved
    template — not re-inferred by every caller."""
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "x", "connector_type": "sink"},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a",
        _payload(
            template_id=template["id"], connection_profile_id=connection["id"],
            connector_config={"topics": "raw-t-s-some_topic-batch-v1"},
        ),
    )

    assert refs.direction == "SINK"


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_a_sink_with_no_input_topic(fake_postgres) -> None:
    from app.core.errors import ValidationError

    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "sink"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(ValidationError):
        await validate_pipeline_references(
            "org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"]),
        )


# ── FE-BUG-23: template capabilities are enforced here, at creation, not ──
# only at deploy — see _validate_capabilities in pipeline_spec_validator.py.

@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_a_mode_the_template_does_not_support(fake_postgres) -> None:
    from app.core.errors import ValidationError

    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "x", "connector_type": "source", "supported_modes": ["batch"]},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(ValidationError) as exc_info:
        await validate_pipeline_references(
            "org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], mode="cdc"),
        )

    assert exc_info.value.field_errors[0].field == "mode"


@pytest.mark.asyncio
async def test_validate_pipeline_references_accepts_a_mode_the_template_declares_supported(fake_postgres) -> None:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "x", "connector_type": "source", "supported_modes": ["cdc", "snapshot"]},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], mode="cdc"),
    )

    assert refs.template["id"] == template["id"]


@pytest.mark.asyncio
async def test_validate_pipeline_references_accepts_any_mode_when_supported_modes_is_undeclared(fake_postgres) -> None:
    """Empty/undeclared supported_modes means unrestricted, not "supports
    nothing" — same convention as deploy_preflight.py."""
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], mode="cdc"),
    )

    assert refs.template["id"] == template["id"]


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_dlq_routing_when_template_does_not_support_it(fake_postgres) -> None:
    from app.core.errors import ValidationError

    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "x", "connector_type": "source", "supports_dlq": False},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(ValidationError) as exc_info:
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"], connection_profile_id=connection["id"],
                error_handling_policy="DLQ_AND_CONTINUE",
            ),
        )

    assert exc_info.value.field_errors[0].field == "error_handling_policy"


@pytest.mark.asyncio
async def test_validate_pipeline_references_rejects_threshold_policy_when_template_does_not_support_dlq(fake_postgres) -> None:
    """THRESHOLD routes to the same connector-DLQ topic as DLQ_AND_CONTINUE
    (see VALID_ERROR_HANDLING_POLICIES' own docstring in schemas/pipeline.py)
    — it needs the same capability, not just DLQ_AND_CONTINUE by name."""
    from app.core.errors import ValidationError

    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "x", "connector_type": "source", "supports_dlq": False},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    with pytest.raises(ValidationError):
        await validate_pipeline_references(
            "org_a",
            _payload(
                template_id=template["id"], connection_profile_id=connection["id"],
                error_handling_policy="THRESHOLD",
            ),
        )


@pytest.mark.asyncio
async def test_validate_pipeline_references_allows_fail_fast_regardless_of_dlq_support(fake_postgres) -> None:
    """FAIL_FAST never routes anywhere — it must stay available even for a
    template with supports_dlq: False."""
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "x", "connector_type": "source", "supports_dlq": False},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})

    refs = await validate_pipeline_references(
        "org_a",
        _payload(
            template_id=template["id"], connection_profile_id=connection["id"],
            error_handling_policy="FAIL_FAST",
        ),
    )

    assert refs.template["id"] == template["id"]
