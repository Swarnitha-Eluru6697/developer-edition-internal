"""DBT-006 / DBT-007: a run executes exactly what it was submitted with.

Before: submit() stored only the selector. Dispatch re-read the project's CURRENT Git ref (and the Job cloned whatever
that branch pointed to by then) and the isolation layer re-read the project's CURRENT DQ schema blocks, so a run that
waited in the queue could build a newer commit under newer (or different) DQ rules than anyone saw at submission, and
nothing recorded which.

Now submit() freezes a manifest (resolved commit, project settings, DQ blocks with their artifact versions and hashes)
and every later step reads only that. These tests drive the real run service against a real migrated Postgres, in both
runner modes, with the Git host and the executor faked (the real Git side is in tests/test_run_manifest.py).
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, get_settings
from app.main import app
from app.models import Project, Run, ScheduledOccurrence
from app.schemas.project import RunTriggerRequest
from app.services import run_manifest
from app.services.run_service import RunService, dispatch_run
from app.services.scheduler import _maybe_fire

SHA1 = "1" * 40
SHA2 = "2" * 40
URL = "https://github.com/acme/analytics.git"
BLOCK_V1 = "models:\n- name: stg_orders\n  columns:\n  - name: id\n    tests: [unique]\n"
BLOCK_V2 = "models:\n- name: stg_orders\n  columns:\n  - name: id\n    tests: [unique, not_null]\n"
META_V1 = {"artifact_version_id": "av-1", "binding_id": "b-1", "content_hash": "hash-1",
           "renderer_version": "2.0", "idempotency_key": "k-1", "applied_at": "2026-10-14T00:00:00+00:00"}
META_V2 = {"artifact_version_id": "av-2", "binding_id": "b-1", "content_hash": "hash-2",
           "renderer_version": "2.0", "idempotency_key": "k-2", "applied_at": "2026-10-14T01:00:00+00:00"}


# ---------------------------------------------------------------------
# Harness: a Git host whose ref can move, and an executor that records what it was told to run
# ---------------------------------------------------------------------
class Remote:
    """The project's branch on the (fake) Git host. `sha` is what the ref points to NOW."""

    def __init__(self, sha: str = SHA1):
        self.sha = sha
        self.fail: run_manifest.GitRefError | None = None
        self.resolutions = 0


class Harness:
    def __init__(self):
        self.clones: list[dict] = []          # subprocess mode: clone_for_run calls
        self.applied: list[dict] = []         # subprocess mode: DQ blocks merged into the clone
        self.executions: list[dict] = []      # execute_run kwargs
        self.results: list[dict] = []         # scripted results, consumed in order (default: success)
        self.before_result = None             # async hook run inside execute_run

    @property
    def last(self) -> dict:
        return self.executions[-1]


@pytest.fixture
def remote(monkeypatch):
    r = Remote()

    async def resolve(git_url, git_ref, token=None):
        r.resolutions += 1
        if r.fail:
            raise r.fail
        return r.sha

    monkeypatch.setattr("app.services.run_manifest.resolve_commit", resolve)
    return r


@pytest.fixture(params=["subprocess", "k8s_job"])
def mode(request):
    return request.param


@pytest.fixture
def no_dispatch(monkeypatch):
    """submit() fires a background dispatch; these tests drive dispatch themselves."""
    async def _noop(run_id):
        return None
    monkeypatch.setattr("app.services.run_service.dispatch_run", _noop)


@pytest.fixture
def harness(monkeypatch, tmp_path, mode, remote, run_db, no_dispatch):
    h = Harness()
    settings = Settings(runner_mode=mode, environment="dev", artifact_store_backend="none")
    monkeypatch.setattr("app.services.run_service.get_settings", lambda: settings)

    clone_dir = tmp_path / "clone"
    (clone_dir / "dbt" / "sales").mkdir(parents=True)
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    (clone_dir / "dbt" / "sales" / "dbt_project.yml").write_text("name: sales\n")

    class FakeGit:
        def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
            h.clones.append({"git_url": git_url, "git_ref": git_ref, "sha": sha})
            self._head = sha or "not-frozen"
            return clone_dir

        def head_sha(self, path):
            return self._head

        def tree_sha(self, clone_dir, project_dir):
            return "7" * 40

        def cleanup(self, run_id):
            pass

    class FakeIsolation:
        async def execute_run(self, **kwargs):
            h.executions.append(kwargs)
            if h.before_result:
                await h.before_result(kwargs)
            return dict(h.results.pop(0)) if h.results else {"success": True, "errors": [], "nodes": []}

        async def cancel_job(self, **kw):
            return False

    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: FakeGit())
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: FakeIsolation())
    monkeypatch.setattr("app.services.run_service._apply_dq_schema_tests",
                        lambda blocks, project_dir: h.applied.append(dict(blocks)))
    return h


async def _project(run_db, *, git_url=URL, dq=None, dq_meta=None, **kw) -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name=kw.pop("name", "analytics"), git_url=git_url,
                    git_ref=kw.pop("git_ref", "main"), engine=kw.pop("engine", "pg_lake"),
                    dq_schema_tests=dq, dq_schema_tests_meta=dq_meta, **kw)
        db.add(p)
        await db.commit()
        return p.id


async def _submit(run_db, project_id, *, select_=None) -> uuid.UUID:
    async with run_db() as db:
        project = await db.get(Project, project_id)
        run = await RunService(db).submit(project=project, trigger="manual", triggered_by_user_id="u1",
                                          request=RunTriggerRequest(select=select_))
        await db.commit()
        return run.id


async def _run(run_db, run_id) -> Run:
    async with run_db() as db:
        return await db.get(Run, run_id)


async def _change_project(run_db, project_id, **values):
    async with run_db() as db:
        await db.execute(update(Project).where(Project.id == project_id).values(**values))
        await db.commit()


def _blocks_executed(h: Harness, mode: str) -> dict:
    return h.applied[-1] if mode == "subprocess" else h.last["dq_schema_blocks"]


# ---------------------------------------------------------------------
# What submission records
# ---------------------------------------------------------------------
async def test_submit_freezes_the_commit_settings_and_dq_artifacts(run_db, remote, no_dispatch):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1}, project_path="dbt/sales",
                         engine="trino")
    remote.sha = SHA1
    run_id = await _submit(run_db, pid, select_="tag:nightly")

    run = await _run(run_db, run_id)
    assert run.status == "queued"
    assert run.git_sha == SHA1                                     # the commit is known BEFORE the run can execute
    m = run.manifest
    assert m["git"] == {"url": URL, "ref": "main", "sha": SHA1, "source": "ref"}
    assert (m["engine"], m["project_path"], m["select"]) == ("trino", "dbt/sales", "tag:nightly")
    block = m["dq"]["blocks"]["stg_orders"]
    assert block["yaml"] == BLOCK_V1
    assert (block["artifact_version_id"], block["binding_id"], block["content_hash"]) == ("av-1", "b-1", "hash-1")
    assert run.manifest_sha256 == run_manifest.manifest_sha256(m)
    assert run_manifest.verify(m, run.manifest_sha256)


async def test_a_project_with_no_dq_binding_freezes_an_empty_dq_set(run_db, remote, no_dispatch):
    run = await _run(run_db, await _submit(run_db, await _project(run_db)))
    assert run.manifest["dq"] == {"blocks": {}}


async def test_the_manifest_has_no_git_credential_but_dispatch_still_uses_it(run_db, remote, harness, mode):
    secret_url = "https://deploy:tok-abc@github.com/acme/analytics.git"
    pid = await _project(run_db, git_url=secret_url)
    run_id = await _submit(run_db, pid)
    run = await _run(run_db, run_id)
    assert "tok-abc" not in repr(run.manifest)
    await dispatch_run(run_id)
    cloned = (harness.clones[-1]["git_url"] if mode == "subprocess" else harness.last["git_url"])
    assert cloned == secret_url


# ---------------------------------------------------------------------
# DBT-006: the branch moves while the run is queued
# ---------------------------------------------------------------------
async def test_a_branch_that_advances_while_queued_does_not_change_the_commit_that_runs(run_db, remote, harness, mode):
    pid = await _project(run_db)
    remote.sha = SHA1
    run_id = await _submit(run_db, pid)
    remote.sha = SHA2                                   # someone pushes while the run waits in the queue

    await dispatch_run(run_id)

    assert len(harness.executions) == 1
    executed = harness.clones[-1]["sha"] if mode == "subprocess" else harness.last["git_sha"]
    assert executed == SHA1
    run = await _run(run_db, run_id)
    assert run.status == "succeeded" and run.git_sha == SHA1


async def test_dispatch_does_not_even_look_at_the_remote_again(run_db, remote, harness):
    pid = await _project(run_db)
    run_id = await _submit(run_db, pid)
    after_submit = remote.resolutions
    await dispatch_run(run_id)
    assert remote.resolutions == after_submit


async def test_a_new_run_gets_the_new_commit(run_db, remote, harness, mode):
    pid = await _project(run_db)
    remote.sha = SHA1
    first = await _submit(run_db, pid)
    remote.sha = SHA2
    second = await _submit(run_db, pid)
    await dispatch_run(first)
    await dispatch_run(second)
    assert (await _run(run_db, first)).git_sha == SHA1
    assert (await _run(run_db, second)).git_sha == SHA2


async def test_changing_the_projects_ref_engine_or_selector_after_submit_changes_nothing(run_db, remote, harness, mode):
    pid = await _project(run_db, engine="pg_lake", project_path="dbt/sales")
    run_id = await _submit(run_db, pid, select_="tag:a")
    remote.sha = SHA2
    await _change_project(run_db, pid, git_ref="develop", engine="trino", project_path="")

    await dispatch_run(run_id)

    ex = harness.last
    assert (ex["engine"], ex["select"], ex["git_ref"], ex["project_path"]) == ("pg_lake", "tag:a", "main", "dbt/sales")
    assert (await _run(run_db, run_id)).git_sha == SHA1


async def test_a_retry_after_a_never_started_attempt_runs_the_same_commit(run_db, remote, harness, mode):
    pid = await _project(run_db)
    run_id = await _submit(run_db, pid)
    harness.results = [{"_never_started": True, "success": False, "errors": [], "nodes": []}]

    await dispatch_run(run_id)                          # attempt 1: the Job never started -> requeued
    assert (await _run(run_db, run_id)).status == "queued"
    remote.sha = SHA2                                   # the branch moves between the attempts
    await dispatch_run(run_id)                          # attempt 2

    run = await _run(run_db, run_id)
    assert run.attempt == 2 and run.status == "succeeded" and run.git_sha == SHA1
    executed = [c["sha"] for c in harness.clones] if mode == "subprocess" else [e["git_sha"] for e in harness.executions]
    assert executed == [SHA1, SHA1]


# ---------------------------------------------------------------------
# DBT-007: DQ rules change while the run is queued
# ---------------------------------------------------------------------
async def test_dq_rules_replaced_after_submit_are_not_what_the_run_executes(run_db, remote, harness, mode):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    run_id = await _submit(run_db, pid)
    # DQ applies a new artifact version while the run waits
    await _change_project(run_db, pid, dq_schema_tests={"stg_orders": BLOCK_V2, "extra": "models: []\n"},
                          dq_schema_tests_meta={"stg_orders": META_V2})

    await dispatch_run(run_id)

    assert len(harness.executions) == 1
    assert _blocks_executed(harness, mode) == {"stg_orders": BLOCK_V1}
    assert (await _run(run_db, run_id)).manifest["dq"]["blocks"]["stg_orders"]["artifact_version_id"] == "av-1"


async def test_dq_rules_removed_after_submit_are_still_applied(run_db, remote, harness, mode):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    run_id = await _submit(run_db, pid)
    await _change_project(run_db, pid, dq_schema_tests=None, dq_schema_tests_meta=None)
    await dispatch_run(run_id)
    assert _blocks_executed(harness, mode) == {"stg_orders": BLOCK_V1}


async def test_dq_rules_added_after_submit_are_not_applied(run_db, remote, harness, mode):
    pid = await _project(run_db)
    run_id = await _submit(run_db, pid)
    await _change_project(run_db, pid, dq_schema_tests={"stg_orders": BLOCK_V1})
    await dispatch_run(run_id)
    assert not _blocks_executed(harness, mode)


async def test_a_retry_reuses_the_same_dq_artifacts(run_db, remote, harness, mode):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    run_id = await _submit(run_db, pid)
    harness.results = [{"_never_started": True, "success": False, "errors": [], "nodes": []}]
    await dispatch_run(run_id)
    await _change_project(run_db, pid, dq_schema_tests={"stg_orders": BLOCK_V2}, dq_schema_tests_meta={"stg_orders": META_V2})
    await dispatch_run(run_id)

    assert (await _run(run_db, run_id)).attempt == 2
    first, second = (harness.applied if mode == "subprocess" else [e["dq_schema_blocks"] for e in harness.executions])
    assert first == second == {"stg_orders": BLOCK_V1}


async def test_a_new_run_snapshots_the_current_rules(run_db, remote, harness, mode):
    """Explicitly starting a new run is how new rules take effect."""
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    old = await _submit(run_db, pid)
    await _change_project(run_db, pid, dq_schema_tests={"stg_orders": BLOCK_V2}, dq_schema_tests_meta={"stg_orders": META_V2})
    new = await _submit(run_db, pid)
    await dispatch_run(new)
    assert len(harness.executions) == 1
    assert _blocks_executed(harness, mode) == {"stg_orders": BLOCK_V2}
    assert (await _run(run_db, new)).manifest["dq"]["blocks"]["stg_orders"]["content_hash"] == "hash-2"
    assert (await _run(run_db, old)).manifest["dq"]["blocks"]["stg_orders"]["content_hash"] == "hash-1"


async def test_concurrent_submissions_each_get_a_consistent_snapshot(run_db, remote, no_dispatch):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    ids = await asyncio.gather(*[_submit(run_db, pid) for _ in range(5)])
    for run_id in ids:
        m = (await _run(run_db, run_id)).manifest
        assert m["dq"]["blocks"]["stg_orders"]["yaml"] == BLOCK_V1 and m["git"]["sha"] == SHA1


# ---------------------------------------------------------------------
# A ref that cannot be resolved never becomes a queued run
# ---------------------------------------------------------------------
@pytest.mark.parametrize("code,status", [("GIT_REF_NOT_FOUND", 422), ("GIT_UNREACHABLE", 502)])
async def test_an_unresolvable_ref_refuses_the_submission_and_queues_nothing(run_db, remote, no_dispatch, code, status):
    pid = await _project(run_db)
    remote.fail = run_manifest.GitRefError(code, "boom")
    with pytest.raises(HTTPException) as e:
        await _submit(run_db, pid)
    assert e.value.status_code == status and e.value.detail["code"] == code
    async with run_db() as db:
        assert (await db.execute(select(Run))).scalars().all() == []


async def test_the_api_reports_an_unresolvable_ref_and_creates_no_run(run_db, remote, auth_headers, no_dispatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                            json={"git_url": URL, "git_ref": "main"})).json()
        remote.fail = run_manifest.GitRefError("GIT_REF_NOT_FOUND", "ref 'main' does not exist")
        r = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "GIT_REF_NOT_FOUND"
        remote.fail = run_manifest.GitRefError("GIT_UNREACHABLE", "timed out")
        assert (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).status_code == 502
        assert (await api.get(f"/api/v1/projects/{p['id']}/runs", headers=auth_headers)).json() == []
        remote.fail = None
        ok = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})
        assert ok.status_code == 202 and ok.json()["git_sha"] == SHA1


async def test_the_scheduler_records_nothing_when_the_ref_cannot_be_resolved_and_succeeds_next_tick(run_db, remote,
                                                                                                    harness):
    now = datetime.now(UTC)
    pid = await _project(run_db, schedule_cron="* * * * *", schedule_enabled=True,
                         updated_at=now - timedelta(minutes=5))
    remote.fail = run_manifest.GitRefError("GIT_UNREACHABLE", "git host down")
    async with run_db() as db:
        project = await db.get(Project, pid)
        with pytest.raises(HTTPException):
            await _maybe_fire(project, now)
    async with run_db() as db:
        assert (await db.execute(select(ScheduledOccurrence))).scalars().all() == []   # not consumed: it will retry
        assert (await db.execute(select(Run))).scalars().all() == []

    remote.fail = None
    async with run_db() as db:
        await _maybe_fire(await db.get(Project, pid), now)
    await asyncio.sleep(0.2)
    async with run_db() as db:
        runs = (await db.execute(select(Run))).scalars().all()
        assert len(runs) == 1 and runs[0].git_sha == SHA1 and runs[0].manifest is not None


# ---------------------------------------------------------------------
# Runs queued by an older runner version (no manifest) — a rolling deploy
# ---------------------------------------------------------------------
async def _legacy_run(run_db, pid, **kw) -> uuid.UUID:
    async with run_db() as db:
        run = Run(project_id=pid, trigger="manual", status="queued", **kw)
        db.add(run)
        await db.commit()
        return run.id


async def test_a_legacy_queued_run_is_frozen_before_it_becomes_executable(run_db, remote, harness, mode):
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    run_id = await _legacy_run(run_db, pid, select="tag:x")
    remote.sha = SHA2

    await dispatch_run(run_id)

    run = await _run(run_db, run_id)
    assert run.status == "succeeded" and run.git_sha == SHA2
    assert run.manifest["git"]["sha"] == SHA2 and run.manifest["select"] == "tag:x"
    assert run_manifest.verify(run.manifest, run.manifest_sha256)
    assert _blocks_executed(harness, mode) == {"stg_orders": BLOCK_V1}


async def test_two_dispatches_of_a_legacy_run_freeze_it_once_and_execute_once(run_db, remote, harness):
    pid = await _project(run_db)
    run_id = await _legacy_run(run_db, pid)
    await asyncio.gather(dispatch_run(run_id), dispatch_run(run_id))
    assert len(harness.executions) == 1
    assert (await _run(run_db, run_id)).manifest["git"]["sha"] == SHA1


async def test_two_replicas_freezing_a_legacy_run_while_the_branch_moves_agree_on_one_commit(run_db, remote, harness,
                                                                                         monkeypatch):
    """Both read the run before either has frozen it, and the branch moves between their two resolutions. Only the
    first write may win; the loser must adopt the winner's manifest, not overwrite it (the trigger would refuse)."""
    pid = await _project(run_db)
    run_id = await _legacy_run(run_db, pid)
    arrived, gate = [], asyncio.Event()

    async def resolve(git_url, git_ref, token=None):
        arrived.append(1)
        n = len(arrived)
        if n == 2:
            gate.set()
        await gate.wait()                      # both are now inside resolve: neither has written yet
        return SHA1 if n == 1 else SHA2

    monkeypatch.setattr("app.services.run_manifest.resolve_commit", resolve)
    await asyncio.gather(dispatch_run(run_id), dispatch_run(run_id))

    run = await _run(run_db, run_id)
    assert len(harness.executions) == 1
    assert run.status == "succeeded"
    assert run.git_sha == run.manifest["git"]["sha"] and run.git_sha in (SHA1, SHA2)
    executed = harness.clones[-1]["sha"] if harness.clones else harness.last["git_sha"]
    assert executed == run.git_sha


async def test_a_legacy_run_whose_ref_cannot_be_resolved_fails_and_never_executes(run_db, remote, harness):
    pid = await _project(run_db)
    run_id = await _legacy_run(run_db, pid)
    remote.fail = run_manifest.GitRefError("GIT_REF_NOT_FOUND", "ref 'main' does not exist")
    await dispatch_run(run_id)
    run = await _run(run_db, run_id)
    assert run.status == "failed" and "could not freeze" in run.error_message and run.completed_at is not None
    assert harness.executions == []


# ---------------------------------------------------------------------
# Integrity: what is stored cannot be changed, and a changed one is never run
# ---------------------------------------------------------------------
async def test_the_database_refuses_to_change_a_manifest_its_hash_or_the_frozen_commit(run_db, remote, no_dispatch):
    run_id = await _submit(run_db, await _project(run_db))
    for values in ({"manifest": {"x": 1}}, {"manifest_sha256": "0" * 64}, {"git_sha": SHA2},
                   {"manifest": None}, {"git_sha": None}):
        async with run_db() as db:
            with pytest.raises(DBAPIError, match="immutable"):
                await db.execute(update(Run).where(Run.id == run_id).values(**values))
    run = await _run(run_db, run_id)
    assert run.git_sha == SHA1 and run.manifest["git"]["sha"] == SHA1


async def test_everything_else_on_the_run_can_still_change(run_db, remote, no_dispatch):
    run_id = await _submit(run_db, await _project(run_db))
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(status="running", error_message="x", attempt=3))
        await db.commit()
    assert (await _run(run_db, run_id)).attempt == 3


async def test_a_manifest_can_be_set_once_on_a_run_that_has_none(run_db, remote):
    run_id = await _legacy_run(run_db, await _project(run_db))
    m = run_manifest.build_manifest(git_url=URL, git_ref="main", git_sha=SHA1, project_path="", engine="pg_lake",
                                    select=None, dq_blocks=None, dq_meta=None)
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(
            manifest=m, manifest_sha256=run_manifest.manifest_sha256(m), git_sha=SHA1))
        await db.commit()
    async with run_db() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(update(Run).where(Run.id == run_id).values(git_sha=SHA2))


async def test_a_tampered_manifest_is_refused_not_executed(run_db, remote, harness):
    """The trigger normally prevents this; it is switched off here to prove the application re-checks anyway."""
    pid = await _project(run_db, dq={"stg_orders": BLOCK_V1}, dq_meta={"stg_orders": META_V1})
    run_id = await _submit(run_db, pid)
    tampered = copy.deepcopy((await _run(run_db, run_id)).manifest)
    tampered["dq"]["blocks"]["stg_orders"]["yaml"] = "models: []\n"
    async with run_db() as db:
        await db.execute(text("ALTER TABLE runs DISABLE TRIGGER trg_runs_manifest_immutable"))
        await db.execute(update(Run).where(Run.id == run_id).values(manifest=tampered))
        await db.execute(text("ALTER TABLE runs ENABLE TRIGGER trg_runs_manifest_immutable"))
        await db.commit()

    await dispatch_run(run_id)

    run = await _run(run_db, run_id)
    assert run.status == "failed" and "refusing to run" in run.error_message
    assert harness.executions == []


async def test_a_project_repointed_at_another_repository_is_refused(run_db, remote, harness):
    pid = await _project(run_db)
    run_id = await _submit(run_db, pid)
    await _change_project(run_db, pid, git_url="https://github.com/evil/other.git")
    await dispatch_run(run_id)
    run = await _run(run_db, run_id)
    assert run.status == "failed" and "not the one this run was submitted for" in run.error_message
    assert harness.executions == []


async def test_a_rotated_clone_credential_is_accepted_and_used(run_db, remote, harness, mode):
    pid = await _project(run_db, git_url="https://deploy:old@github.com/acme/analytics.git")
    run_id = await _submit(run_db, pid)
    await _change_project(run_db, pid, git_url="https://deploy:new@github.com/acme/analytics.git")
    await dispatch_run(run_id)
    cloned = harness.clones[-1]["git_url"] if mode == "subprocess" else harness.last["git_url"]
    assert cloned == "https://deploy:new@github.com/acme/analytics.git"
    assert (await _run(run_db, run_id)).status == "succeeded"


# ---------------------------------------------------------------------
# The result must be for the frozen commit
# ---------------------------------------------------------------------
async def test_a_build_that_reports_a_different_commit_is_not_recorded_as_a_success(run_db, remote, harness, mode):
    if mode != "k8s_job":
        pytest.skip("subprocess mode checks out the frozen commit itself; the Job reports its own")
    pid = await _project(run_db)
    run_id = await _submit(run_db, pid)
    harness.results = [{"success": True, "errors": [], "nodes": [], "git_sha": SHA2}]
    await dispatch_run(run_id)
    run = await _run(run_db, run_id)
    assert run.status == "failed" and SHA1 in run.error_message and SHA2 in run.error_message
    assert run.git_sha == SHA1


async def test_a_build_that_reports_the_frozen_commit_succeeds(run_db, remote, harness, mode):
    if mode != "k8s_job":
        pytest.skip("covered by the other tests in subprocess mode")
    run_id = await _submit(run_db, await _project(run_db))
    harness.results = [{"success": True, "errors": [], "nodes": [], "git_sha": SHA1}]
    await dispatch_run(run_id)
    run = await _run(run_db, run_id)
    assert run.status == "succeeded" and run.git_sha == SHA1


# ---------------------------------------------------------------------
# The audit record is readable, and only by the project's owner
# ---------------------------------------------------------------------
async def test_the_manifest_is_readable_through_the_api_without_credentials(run_db, remote, auth_headers, no_dispatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                            json={"git_url": "https://github.com/acme/m.git", "git_ref": "main"})).json()
        async with run_db() as db:
            # a row written before credentials were refused (DBT-009): its manifest and API output still carry none
            await db.execute(update(Project).where(Project.id == uuid.UUID(p["id"])).values(
                git_url="https://deploy:tok-zzz@github.com/acme/m.git",
                dq_schema_tests={"stg_orders": BLOCK_V1}, dq_schema_tests_meta={"stg_orders": META_V1}))
            await db.commit()
        run = (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).json()

        r = await api.get(f"/api/v1/projects/{p['id']}/runs/{run['id']}/manifest", headers=auth_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["git_sha"] == SHA1 and body["manifest"]["git"]["sha"] == SHA1
        assert body["manifest"]["dq"]["blocks"]["stg_orders"]["artifact_version_id"] == "av-1"
        assert body["manifest_sha256"] == run_manifest.manifest_sha256(body["manifest"])
        assert "tok-zzz" not in r.text

        other = (await api.get(f"/api/v1/projects/{uuid.uuid4()}/runs/{run['id']}/manifest", headers=auth_headers))
        assert other.status_code == 404
        assert (await api.get(f"/api/v1/projects/{p['id']}/runs/{uuid.uuid4()}/manifest",
                              headers=auth_headers)).status_code == 404
        assert (await api.get(f"/api/v1/projects/{p['id']}/runs/{run['id']}/manifest")).status_code == 401


async def test_the_run_list_still_shows_the_commit_before_the_run_starts(run_db, remote, auth_headers, no_dispatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                            json={"git_url": URL, "git_ref": "main"})).json()
        run = (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).json()
        assert run["status"] == "queued" and run["git_sha"] == SHA1
        assert "manifest" not in run                     # the run list stays small; the manifest has its own route


# ---------------------------------------------------------------------
# DG-007: Dagster's git sensor pins the commit it saw; the Runner builds exactly that
# ---------------------------------------------------------------------
SENSOR_SHA = "d" * 40


@pytest.fixture
def scheduler_api(run_db, monkeypatch, no_dispatch):
    """The real app with Dagster's scoped identity configured, and a registered project (via the DB)."""
    import time

    from jose import jwt
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", "dagster-scheduler-signing-secret")
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])

    def trigger_token() -> str:
        now = int(time.time())
        return jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": "dbt.runs.trigger",
                           "org_id": s.org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60},
                          "dagster-scheduler-signing-secret", algorithm="HS256")

    class Api:
        async def post_run(self, pid, body, *, trigger="api", key=None):
            headers = {"X-Internal-Service-Token": trigger_token(), **({"Idempotency-Key": key} if key else {})}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.post(f"/api/v1/internal/projects/{pid}/run?trigger={trigger}", json=body,
                                    headers=headers)

    return Api()


async def test_a_pinned_run_freezes_exactly_the_commit_the_sensor_saw(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    remote.sha = SHA2                                           # the branch has ALREADY moved past what the sensor saw
    r = await scheduler_api.post_run(pid, {"git_sha": SENSOR_SHA})
    assert r.status_code == 202, r.text
    assert r.json()["git_sha"] == SENSOR_SHA                    # reported back at once, so the caller can check it
    run = await _run(run_db, uuid.UUID(r.json()["id"]))
    assert run.git_sha == SENSOR_SHA and run.manifest["git"]["sha"] == SENSOR_SHA
    assert run.manifest["git"]["source"] == "request" and run.manifest["git"]["ref"] == "main"
    assert run_manifest.verify(run.manifest, run.manifest_sha256)


async def test_a_pinned_run_does_not_consult_the_ref_at_all(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    remote.fail = run_manifest.GitRefError("GIT_UNREACHABLE", "git host down")     # resolving would fail
    r = await scheduler_api.post_run(pid, {"git_sha": SENSOR_SHA})
    assert r.status_code == 202 and remote.resolutions == 0


async def test_the_pinned_commit_is_what_gets_built_even_if_the_branch_moved(run_db, remote, harness, mode,
                                                                            scheduler_api):
    pid = await _project(run_db)
    remote.sha = SHA1
    r = await scheduler_api.post_run(pid, {"git_sha": SENSOR_SHA})
    run_id = uuid.UUID(r.json()["id"])
    remote.sha = SHA2
    await dispatch_run(run_id)
    assert len(harness.executions) == 1
    executed = harness.clones[-1]["sha"] if mode == "subprocess" else harness.last["git_sha"]
    assert executed == SENSOR_SHA
    assert (await _run(run_db, run_id)).git_sha == SENSOR_SHA


async def test_an_uppercase_sha_is_frozen_lowercase(run_db, remote, scheduler_api):
    r = await scheduler_api.post_run(await _project(run_db), {"git_sha": SENSOR_SHA.upper()})
    assert r.status_code == 202 and r.json()["git_sha"] == SENSOR_SHA


@pytest.mark.parametrize("bad", ["", "abc123", "d" * 39, "d" * 41, "g" * 40, "main", " " + "d" * 40])
async def test_anything_but_a_full_sha_is_refused_and_queues_nothing(run_db, remote, scheduler_api, bad):
    pid = await _project(run_db)
    r = await scheduler_api.post_run(pid, {"git_sha": bad})
    assert r.status_code == 422
    async with run_db() as db:
        assert (await db.execute(select(Run))).scalars().all() == []


async def test_a_run_with_no_sha_still_resolves_the_ref(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    remote.sha = SHA2
    r = await scheduler_api.post_run(pid, {})
    run = await _run(run_db, uuid.UUID(r.json()["id"]))
    assert run.git_sha == SHA2 and run.manifest["git"]["source"] == "ref" and remote.resolutions == 1


async def test_a_user_cannot_pin_a_commit(run_db, remote, auth_headers, no_dispatch):
    """Pinning is the scheduler's ability: a user must not be able to run a commit that is not on the project's ref,
    and must not be silently given the tip when they asked for something else."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                            json={"git_url": URL, "git_ref": "main"})).json()
        r = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={"git_sha": SENSOR_SHA})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "GIT_SHA_NOT_ACCEPTED"
        assert (await api.get(f"/api/v1/projects/{p['id']}/runs", headers=auth_headers)).json() == []
        assert (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).status_code == 202


# ---------------------------------------------------------------------
# X-004: subject / org / workspace in the manifest
# ---------------------------------------------------------------------
async def test_a_users_manifest_names_the_user_the_org_and_the_workspace(run_db, remote, auth_headers, no_dispatch):
    ws = uuid.uuid4()
    pid = await _project(run_db, workspace_id=ws)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        run = (await api.post(f"/api/v1/projects/{pid}/run", headers=auth_headers, json={})).json()
        m = (await api.get(f"/api/v1/projects/{pid}/runs/{run['id']}/manifest", headers=auth_headers)).json()["manifest"]
    assert m["org_id"] == get_settings().org_id and m["workspace_id"] == str(ws)
    assert m["subject_id"] and m["trigger"] == "manual" and m["manifest_version"] == "1"


async def test_a_scheduler_run_names_its_identity_and_the_builtin_scheduler_names_itself(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    r = await scheduler_api.post_run(pid, {})
    assert (await _run(run_db, uuid.UUID(r.json()["id"]))).manifest["subject_id"] == "dagster"
    internal = await _submit(run_db, pid)                       # RunService.submit with no subject: the cron scheduler
    assert (await _run(run_db, internal)).manifest["subject_id"] in ("u1", "scheduler")


# ---------------------------------------------------------------------
# X-004: an Idempotency-Key makes a retried submission return the run it created
# ---------------------------------------------------------------------
async def _post(scheduler_api, pid, body, key=None):
    return await scheduler_api.post_run(pid, body, key=key)


async def _count_runs(run_db) -> int:
    async with run_db() as db:
        return len((await db.execute(select(Run))).scalars().all())


async def test_a_retried_submission_returns_the_same_run_and_creates_no_second(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    a = await _post(scheduler_api, pid, {"select": "tag:a"}, key="dagster-run-1")
    b = await _post(scheduler_api, pid, {"select": "tag:a"}, key="dagster-run-1")
    assert a.status_code == b.status_code == 202 and a.json()["id"] == b.json()["id"]
    assert await _count_runs(run_db) == 1
    assert (await _run(run_db, uuid.UUID(a.json()["id"]))).manifest["idempotency_key"] == "dagster-run-1"


async def test_the_same_key_with_a_different_request_is_refused(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    await _post(scheduler_api, pid, {"select": "tag:a"}, key="k")
    for other in ({"select": "tag:b"}, {"select": "tag:a", "git_sha": SENSOR_SHA}, {}):
        r = await _post(scheduler_api, pid, other, key="k")
        assert r.status_code == 422 and r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await _count_runs(run_db) == 1


async def test_different_keys_are_different_runs_and_no_key_is_never_deduplicated(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    ids = {(await _post(scheduler_api, pid, {}, key=k)).json()["id"] for k in ("a", "b", None, None)}
    assert len(ids) == 4


async def test_a_key_is_scoped_to_its_project(run_db, remote, scheduler_api):
    p1, p2 = await _project(run_db, name="one"), await _project(run_db, name="two", git_url="https://github.com/acme/two.git")
    a, b = await _post(scheduler_api, p1, {}, key="same"), await _post(scheduler_api, p2, {}, key="same")
    assert a.json()["id"] != b.json()["id"]


async def test_concurrent_submissions_with_one_key_create_exactly_one_run(run_db, remote, scheduler_api):
    pid = await _project(run_db)
    rs = await asyncio.gather(*[_post(scheduler_api, pid, {}, key="race") for _ in range(8)])
    assert {r.status_code for r in rs} == {202}
    assert len({r.json()["id"] for r in rs}) == 1 and await _count_runs(run_db) == 1


async def test_a_retry_after_the_run_finished_returns_the_finished_run(run_db, remote, harness, scheduler_api):
    pid = await _project(run_db)
    first = (await _post(scheduler_api, pid, {}, key="done")).json()
    await dispatch_run(uuid.UUID(first["id"]))
    again = (await _post(scheduler_api, pid, {}, key="done")).json()
    assert again["id"] == first["id"] and again["status"] == "succeeded"
    assert len(harness.executions) == 1


async def test_a_key_longer_than_255_characters_is_refused(run_db, remote, scheduler_api):
    r = await _post(scheduler_api, await _project(run_db), {}, key="k" * 256)
    assert r.status_code == 422


async def test_the_user_route_honours_the_key_too(run_db, remote, auth_headers, no_dispatch):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": URL, "git_ref": "main"})).json()
        h = {**auth_headers, "Idempotency-Key": "ui-click-1"}
        a = await api.post(f"/api/v1/projects/{p['id']}/run", headers=h, json={})
        b = await api.post(f"/api/v1/projects/{p['id']}/run", headers=h, json={})
        assert a.json()["id"] == b.json()["id"]


# ---------------------------------------------------------------------
# X-004: evidence of what was built
# ---------------------------------------------------------------------
async def test_the_trees_hash_of_what_was_built_is_recorded_and_shown_in_the_audit_record(run_db, remote, harness, mode,
                                                                                         auth_headers):
    if mode == "k8s_job":
        harness.results = [{"success": True, "errors": [], "nodes": [], "git_sha": SHA1, "source_tree_sha": "5" * 40}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        p = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": URL, "git_ref": "main"})).json()
        run = (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).json()
        await dispatch_run(uuid.UUID(run["id"]))
        body = (await api.get(f"/api/v1/projects/{p['id']}/runs/{run['id']}/manifest", headers=auth_headers)).json()
    expect = "5" * 40 if mode == "k8s_job" else "7" * 40          # the Job reports it / the dev-mode clone computes it
    assert body["source_tree_sha"] == expect and body["git_sha"] == SHA1
