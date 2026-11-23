import pytest

from app.services.runtime_ownership import pipeline_by_connector_name, pipeline_by_topic_name


def _seed_pipeline(fake_postgres, **overrides) -> dict:
    pipeline = {
        "org_id": "org_ownership_test",
        "connector_name": "crm--salesforce--accounts--cdc--v1",
        "topic_name": "raw-crm-salesforce-accounts-cdc-v1",
        "domain": "crm",
        "source_system": "salesforce",
        "entity": "accounts",
        "version": "v1",
    }
    pipeline.update(overrides)
    return fake_postgres.create("pipelines", pipeline)


@pytest.mark.asyncio
async def test_pipeline_by_connector_name_only_includes_this_org(fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres)
    _seed_pipeline(fake_postgres, org_id="org_other", connector_name="crm--salesforce--invoices--cdc--v1")

    mapping = await pipeline_by_connector_name("org_ownership_test")

    assert mapping[mine["connector_name"]]["id"] == mine["id"]
    assert "crm--salesforce--invoices--cdc--v1" not in mapping


@pytest.mark.asyncio
async def test_pipeline_by_connector_name_excludes_unrelated_cluster_connectors(fake_postgres) -> None:
    """A connector belonging to a completely different application sharing
    the Connect cluster was never created by any DataConnect pipeline, so it
    can't appear in any org's mapping no matter what its name looks like."""
    _seed_pipeline(fake_postgres)

    mapping = await pipeline_by_connector_name("org_ownership_test")

    assert "some-other-apps-debezium-connector" not in mapping


@pytest.mark.asyncio
async def test_pipeline_by_topic_name_includes_raw_prepared_and_quarantine_variants(fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres)

    mapping = await pipeline_by_topic_name("org_ownership_test")

    assert mapping["raw-crm-salesforce-accounts-cdc-v1"]["id"] == mine["id"]
    assert mapping["prepared-crm-salesforce-accounts-cdc-v1"]["id"] == mine["id"]
    # GAP-03: both quarantine topics belong to this pipeline — the quality-
    # validation one and Kafka Connect's own connector-DLQ one, distinct
    # topics for distinct failure classes (see app/services/topic_naming.py)
    # — so both need attributing back to it, or one would show up to
    # monitoring as an unowned/cross-tenant topic.
    assert mapping["quarantine-crm-salesforce-accounts-validation-v1"]["id"] == mine["id"]
    assert mapping["quarantine-crm-salesforce-accounts-connector-v1"]["id"] == mine["id"]


@pytest.mark.asyncio
async def test_pipeline_by_topic_name_only_includes_this_org(fake_postgres) -> None:
    _seed_pipeline(fake_postgres, org_id="org_topic_other", topic_name="raw-crm-salesforce-accounts-cdc-v1")

    mapping = await pipeline_by_topic_name("org_ownership_test")

    assert "raw-crm-salesforce-accounts-cdc-v1" not in mapping
