"""Deleting a quality policy against a REAL, migrated PostgreSQL (DATACONNECT_REAL_DB_TESTS=1).

A pipeline's delete keeps its quality results but unlinks them from the pipeline, so they still referenced the policy and the database refused
the policy's delete with a misleading "references data that doesn't exist": a policy whose pipelines were all deleted could never be deleted.
Now a policy still attached to a pipeline is refused with the pipelines named, and an unused one goes together with its own history."""

import uuid

import pytest
from alerts_real_db import (  # noqa: F401 - the fixtures are found by pytest under their own names: factory, scratch_url
    add_pipeline,
    factory_fixture,
    new_org,
    scratch_url_fixture,
    skip_without_db,
)
from sqlalchemy import text

from app.core.errors import QualityPolicyInUseError
from app.services import quality_service as quality_module
from app.services.quality_service import quality_service

pytestmark = skip_without_db


async def _policy_with_history(make, org: str) -> str:
    pid = str(uuid.uuid4())
    async with make() as s:
        await s.execute(text("""INSERT INTO dc_quality_policies (id, org_id, name, rules, on_failure, quarantine_sink)
                                VALUES (:id, :o, 'checks', '[]', 'route_to_quarantine', 'kafka')"""), {"id": pid, "o": org})
        await s.execute(text("""INSERT INTO dc_quality_results (id, org_id, pipeline_id, quality_policy_id, topic_name, rule_name, severity,
                                    status, failed_count, total_count, sample_failures)
                                VALUES (:id, :o, NULL, :p, 't', '*', 'error', 'skipped', 0, 0, '[]')"""),
                        {"id": str(uuid.uuid4()), "o": org, "p": pid})
        await s.commit()
    return pid


async def _count(make, table: str, policy_id: str) -> int:
    async with make() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table} WHERE quality_policy_id = :p"), {"p": policy_id})).scalar()


@pytest.mark.asyncio
async def test_a_policy_whose_pipeline_was_deleted_can_be_deleted_with_its_history(factory, monkeypatch):
    monkeypatch.setattr(quality_module, "AsyncSessionLocal", factory)
    org = await new_org(factory)
    policy = await _policy_with_history(factory, org)        # a result left behind by a deleted pipeline (pipeline_id NULL)

    await quality_service.delete_policy(org, policy)

    async with factory() as s:
        assert (await s.execute(text("SELECT count(*) FROM dc_quality_policies WHERE id = :p"), {"p": policy})).scalar() == 0
    assert await _count(factory, "dc_quality_results", policy) == 0


@pytest.mark.asyncio
async def test_a_policy_still_attached_to_a_pipeline_is_refused_and_names_it(factory, monkeypatch):
    monkeypatch.setattr(quality_module, "AsyncSessionLocal", factory)
    org = await new_org(factory)
    policy = await _policy_with_history(factory, org)
    pipeline = await add_pipeline(factory, org, "orders-src")
    async with factory() as s:
        await s.execute(text("UPDATE dc_pipelines SET quality_policy_id = :p WHERE id = :id"), {"p": policy, "id": pipeline})
        await s.commit()

    with pytest.raises(QualityPolicyInUseError) as err:
        await quality_service.delete_policy(org, policy)

    assert err.value.details == {"pipelines": ["orders-src"]}
    assert "orders-src" in err.value.message
    assert await _count(factory, "dc_quality_results", policy) == 1   # nothing was deleted
