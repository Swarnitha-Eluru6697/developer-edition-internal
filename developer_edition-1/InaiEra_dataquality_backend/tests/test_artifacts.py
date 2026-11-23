"""DQ-ART-004: immutable, content-addressed ArtifactVersions."""
import pytest
import respx
from sqlalchemy import select, text, update

from app.models import ArtifactVersion, JobBinding, Rule, Ruleset
from app.services.render_service import RenderService, canonical_hash
from tests.dq_fixtures import ORG, seed
from tests.helpers import plan, plan_payload, source_table


async def _compile(db, bid, **kw):
    async with db() as s:
        v, cap = await RenderService(s).compile(ORG, bid, **kw)
        await s.commit()
        return v, cap


async def _count(db):
    async with db() as s:
        return (await s.execute(text("SELECT count(*) FROM dq_artifact_versions"))).scalar()


async def test_identical_inputs_reuse_the_same_row(db):
    ids = await seed(db)
    v1, _ = await _compile(db, ids["binding_id"])
    v2, _ = await _compile(db, ids["binding_id"])
    assert v1.id == v2.id and await _count(db) == 1
    assert v1.kind == "standard" and v1.validation_status == "valid" and v1.hash_version == 2


@pytest.mark.parametrize("mutate", ["rule_version", "rule_params", "ruleset_version",
                                    "binding_mode", "binding_version", "engine_version"])
async def test_every_input_changes_the_hash_and_never_overwrites(db, monkeypatch, mutate):
    ids = await seed(db)
    v1, _ = await _compile(db, ids["binding_id"])
    async with db() as s:
        if mutate == "rule_version":
            await s.execute(update(Rule).values(version=2))
        elif mutate == "rule_params":
            await s.execute(update(Rule).values(parameters={"threshold": 0.1}))
        elif mutate == "ruleset_version":
            await s.execute(update(Ruleset).values(version=9))
        elif mutate == "binding_mode":
            await s.execute(update(JobBinding).values(enforcement_mode="blocking"))
        elif mutate == "binding_version":
            await s.execute(update(JobBinding).values(version=5))
        await s.commit()
    if mutate == "engine_version":
        from app.core.config import get_settings
        monkeypatch.setattr(get_settings(), "dbtrunner_engine_version", "1.9.0")
    v2, _ = await _compile(db, ids["binding_id"])
    assert v2.id != v1.id and v2.content_hash != v1.content_hash
    async with db() as s:
        old = await s.get(ArtifactVersion, v1.id)
        assert old.dbt_artifact == v1.dbt_artifact and old.content_hash == v1.content_hash


async def test_rows_cannot_be_updated(db):
    ids = await seed(db)
    v, _ = await _compile(db, ids["binding_id"])
    with pytest.raises(Exception, match="immutable"):
        async with db() as s:
            await s.execute(update(ArtifactVersion).values(dbt_artifact="tampered"))
            await s.commit()


async def test_preview_overrides_never_contaminate_standard(db):
    ids = await seed(db, job_type="flink_job", rules=[("not_null", "order_id", {}, {})])
    p = plan()
    std1, _ = await _compile(db, ids["binding_id"], plan=p)
    prev, _ = await _compile(db, ids["binding_id"], plan=p, overrides={"window_minutes": 5})
    std2, _ = await _compile(db, ids["binding_id"], plan=p)
    assert prev.kind == "preview" and prev.overrides == {"window_minutes": 5}
    assert "INTERVAL '5' MINUTE" in prev.flinksql_artifact
    assert std2.id == std1.id and "INTERVAL '1' MINUTE" in std2.flinksql_artifact


async def test_overrides_are_typed(db):
    from app.core.errors import DQServiceError
    ids = await seed(db, job_type="flink_job")
    with pytest.raises(DQServiceError):
        await _compile(db, ids["binding_id"], plan=plan(), overrides={"kafka_source_ddl": "x"})
    ids2 = await seed(db)
    with pytest.raises(DQServiceError):
        await _compile(db, ids2["binding_id"], overrides={"window_minutes": 3})


async def test_source_plan_change_changes_hash(db):
    ids = await seed(db, job_type="flink_job")
    v1, _ = await _compile(db, ids["binding_id"], plan=plan())
    v2, _ = await _compile(db, ids["binding_id"], plan=plan(normalized_sql_hash="b" * 64))
    src = source_table(columns=[("order_id", "STRING")])
    v3, _ = await _compile(db, ids["binding_id"], plan=plan(sources=[src]))
    v4, _ = await _compile(db, ids["binding_id"], plan=plan(engine_version="1.18.1"))
    assert len({v1.id, v2.id, v3.id, v4.id}) == 4
    assert v2.normalized_sql_hash == "b" * 64 and v4.engine_version == "1.18.1"


async def test_hash_inputs_cover_the_required_fields(db):
    ids = await seed(db, job_type="flink_job")
    v, _ = await _compile(db, ids["binding_id"], plan=plan())
    hi = v.hash_inputs
    assert set(hi) >= {"ruleset", "rules", "binding", "source", "renderer_version", "engine",
                       "contract", "overrides", "kind"}
    assert hi["rules"][0]["rule_version"] == 1 and hi["source"]["plan_hash"]
    assert canonical_hash(hi) == v.content_hash
    assert v.rule_versions == {str(ids["rule_ids"][0]): 1}
    assert v.supported_engine_versions["engine"] == "flink"


async def test_per_artifact_diagnostics(db):
    ids = await seed(db, rules=[("not_null", "id", {}, {}), ("row_count", "", {}, {})])
    v, cap = await _compile(db, ids["binding_id"])
    assert v.validation_status == "valid"
    assert [d["rule_type"] for d in v.validation_diagnostics] == ["row_count"]
    assert [e["rule_type"] for e in cap["unsupported"]] == ["row_count"]


async def test_render_api_flink_requires_and_uses_structured_plan(client, db):
    ids = await seed(db, job_type="flink_job")
    job = ids["job_id"]
    with respx.mock(assert_all_called=True) as m:
        m.get(f"http://flink.test/api/v1/jobs/{job}").respond(
            200, json={"id": str(job), "org_id": ORG, "sql": "CREATE TABLE ...", "name": "t"})
        val = m.post("http://flink.test/api/v1/validate").respond(
            200, json={"valid": True, "errors": [], "plan": plan_payload()})
        async with client as c:
            r = await c.get(f"/api/v1/render/{ids['binding_id']}")
            versions = await c.get(f"/api/v1/render/{ids['binding_id']}/versions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "standard" and body["source_plan_hash"]
    assert body["compatibility"]["compatible"] is True
    assert "X-Internal-Service-Token" in val.calls[0].request.headers
    assert "authorization" not in {k.lower() for k in val.calls[0].request.headers}
    assert len(versions.json()) == 1


async def test_render_api_cross_org_binding_404(client, db, as_principal):
    from tests.conftest import make_principal
    ids = await seed(db)
    as_principal(make_principal(org_id="other-org"))
    async with client as c:
        r = await c.get(f"/api/v1/render/{ids['binding_id']}")
    assert r.status_code == 404


async def test_refresh_endpoint_is_content_addressed(client, db):
    ids = await seed(db)
    r1 = await client.post(f"/api/v1/render/{ids['binding_id']}/refresh")
    r2 = await client.post(f"/api/v1/render/{ids['binding_id']}/refresh")
    r3 = await client.get(f"/api/v1/render/{ids['binding_id']}")
    assert r1.status_code == 200, r1.text
    assert r1.json()["version_id"] == r2.json()["version_id"] == r3.json()["version_id"]
    assert await _count(db) == 1
