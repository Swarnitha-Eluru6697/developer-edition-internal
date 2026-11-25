"""FL-002: a job name collision is the standard 409 — never an internal 500 — for create and rename, with a stable machine-readable
code, the same shape as Workspace's and the dbt runner's. Real Postgres (the unique constraint is what backstops the race)."""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import select

from tests.perms import DQ_PUBLISHER, EDITOR, ENGINEER, VIEWER
from app.core.config import get_settings
from app.core.errors import flush_or_conflict
from app.core.service_auth import SERVICE_AUDIENCE
from app.main import app
from app.models import FlinkJob
from app.schemas import FlinkJobCreate, FlinkJobUpdate
from app.services.job_service import JobService

SECRET = "workspace-to-flink-delegation-secret"
ORG = get_settings().org_id
SQL = "INSERT INTO t SELECT 1;"


@pytest.fixture
def flink_db(pg_db):
    return pg_db


async def _create(db_maker, name) -> uuid.UUID:
    async with db_maker() as db:
        job = await JobService(db).create(org_id=ORG, user_id="u", req=FlinkJobCreate(name=name, sql=SQL))
        await db.commit()
        return job.id


async def _names(db_maker) -> dict:
    async with db_maker() as db:
        return {j.id: j.name for j in (await db.execute(select(FlinkJob))).scalars()}


async def test_create_with_a_taken_name_is_a_coded_409(flink_db):
    await _create(flink_db, "orders")
    async with flink_db() as db:
        with pytest.raises(HTTPException) as e:
            await JobService(db).create(org_id=ORG, user_id="u", req=FlinkJobCreate(name="orders", sql=SQL))
    assert e.value.status_code == 409 and e.value.detail["code"] == "JOB_NAME_TAKEN"
    assert e.value.detail["details"] == {"name": "orders", "resource": "job", "field": "name"}


async def test_rename_onto_a_taken_name_is_a_coded_409_and_changes_nothing(flink_db):
    await _create(flink_db, "taken")
    other = await _create(flink_db, "other")
    async with flink_db() as db:
        with pytest.raises(HTTPException) as e:
            await JobService(db).update(ORG, other, FlinkJobUpdate(name="taken"))
        await db.rollback()
    assert e.value.status_code == 409 and e.value.detail["code"] == "JOB_NAME_TAKEN"
    assert (await _names(flink_db))[other] == "other"


async def test_the_race_the_precheck_cannot_see_is_still_a_409(flink_db, monkeypatch):
    """Two renames to the same free name: the up-front check passes for both; the unique constraint decides, and the loser gets the 409
    (the old code let this escape as an IntegrityError -> 500). The pre-check is disabled to force exactly that path."""
    await _create(flink_db, "taken")
    other = await _create(flink_db, "other")
    real_execute = None

    async with flink_db() as db:
        svc = JobService(db)
        real_execute = db.execute

        async def blind_precheck(stmt, *a, **kw):
            if "flink_jobs.name" in str(stmt) and "flink_jobs.id !=" in str(stmt):
                class _R:
                    def scalar_one_or_none(self):
                        return None
                return _R()
            return await real_execute(stmt, *a, **kw)
        monkeypatch.setattr(db, "execute", blind_precheck)
        with pytest.raises(HTTPException) as e:
            await svc.update(ORG, other, FlinkJobUpdate(name="taken"))
    assert e.value.status_code == 409 and e.value.detail["code"] == "JOB_NAME_TAKEN"


async def test_the_session_is_usable_after_a_conflicting_update_and_a_free_name_works(flink_db):
    await _create(flink_db, "taken")
    other = await _create(flink_db, "other")
    async with flink_db() as db:
        svc = JobService(db)
        with pytest.raises(HTTPException):
            await svc.update(ORG, other, FlinkJobUpdate(name="taken"))
        await db.rollback()
    async with flink_db() as db:
        job = await JobService(db).update(ORG, other, FlinkJobUpdate(name="free"))
        await db.commit()
        assert job.name == "free"


async def test_updating_other_fields_with_the_same_name_is_not_a_conflict(flink_db):
    job_id = await _create(flink_db, "same")
    async with flink_db() as db:
        job = await JobService(db).update(ORG, job_id, FlinkJobUpdate(name="same", description="d"))
        assert job.name == "same" and job.description == "d"


async def test_only_the_named_constraint_is_translated(flink_db):
    """flush_or_conflict(constraint=...) never hides a DIFFERENT integrity error behind a misleading 409."""
    from sqlalchemy.exc import IntegrityError
    async with flink_db() as db:
        db.add(FlinkJob(org_id=ORG, name="bad", sql=SQL, status="not-a-status"))      # violates the status CHECK, not the name
        with pytest.raises(IntegrityError):
            await flush_or_conflict(db, "name taken", constraint="uq_flink_job_org_name", code="JOB_NAME_TAKEN")
        await db.rollback()


def _token(scope) -> str:
    now = int(time.time())
    return jwt.encode({"iss": "workspace", "sub": "user-42", "act": {"sub": "workspace"}, "aud": SERVICE_AUDIENCE, "scope": scope,
                       "org_id": ORG, "permissions": ENGINEER, "email": "u@example.com", "jti": uuid.uuid4().hex,
                       "iat": now, "exp": now + 60}, SECRET, algorithm="HS256")


async def test_the_route_answers_409_with_the_code(flink_db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "delegation_jwt_secret", SECRET)
    monkeypatch.setattr(s, "delegation_jwt_allowed_issuers", ["workspace"])
    await _create(flink_db, "taken")
    other = await _create(flink_db, "other")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.patch(f"/api/v1/jobs/{other}", json={"name": "taken"},
                          headers={"Authorization": f"Bearer {_token('flink.jobs.edit')}"})
    assert r.status_code == 409, r.text
    err = r.json()["detail"]
    assert err["code"] == "JOB_NAME_TAKEN" and err["details"] == {"name": "taken", "resource": "job", "field": "name"}
