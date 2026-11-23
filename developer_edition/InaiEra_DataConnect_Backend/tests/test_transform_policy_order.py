"""The lists with a "Created at" column are newest first: something just created (by hand, or by a pipeline) is at the top, not buried at the bottom."""

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories.dataconnect import (
    ConnectionProfileRepository,
    PipelineRepository,
    QualityPolicyRepository,
    SchemaVersionRepository,
    TopicPolicyRepository,
    TransformPolicyRepository,
)


def _repo_that_records_its_query(base):
    class _Recording(base):
        def __init__(self) -> None:  # no session needed
            self.captured = None

        async def list(self, stmt=None, *args, **kwargs):  # type: ignore[override]
            self.captured = stmt
            return []

    return _Recording()


async def _sql(base) -> str:
    repo = _repo_that_records_its_query(base)
    await repo.list_for_org("org_1001")
    return str(repo.captured.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repo", "table"),
    [
        (ConnectionProfileRepository, "dc_connection_profiles"),
        (PipelineRepository, "dc_pipelines"),
        (SchemaVersionRepository, "dc_schema_versions"),
        (QualityPolicyRepository, "dc_quality_policies"),
        (TransformPolicyRepository, "dc_transform_policies"),
    ],
)
async def test_the_lists_with_a_created_at_column_are_newest_first_for_one_org_only(repo, table):
    sql = await _sql(repo)
    assert "org_id = 'org_1001'" in sql
    assert f"ORDER BY {table}.created_at DESC, {table}.id" in sql


@pytest.mark.asyncio
async def test_other_lists_keep_the_databases_own_order():
    assert "ORDER BY" not in await _sql(TopicPolicyRepository)
