"""Tests for run_service.py: the pure (DB-free) half of DQ schema-test
injection, plus the claim/dispatch/finalize state machine behind
RUN-DBT-003's durable run controller.

_inject_dq_schema_tests() used to fetch from the DB and merge in one
function. RUN-DBT-002 split it into _fetch_dq_schema_blocks() (DB) and
_apply_dq_schema_tests() (pure) so the isolated k8s Job — which gets no
database credentials — can apply blocks the API side already fetched.
The first section here only re-verifies the pure half still merges
correctly after that split; the merge semantics themselves are
pre-existing, unchanged logic.

The second section covers what actually makes a run durable: _claim_run()
(the guarded queued->running transition), dispatch_run() (idempotent —
safe to call more than once for the same run), and _finalize_run() (the
guarded running->terminal transition, also safe to call more than once).
These use a real, schema-backed database (the `run_db` fixture in
conftest.py) rather than mocks, since the whole point is verifying actual
row-state transitions and their guards.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import DbtRunnerError
from app.models import Project, Run, RunEvent
from app.services import run_lease
from app.services.run_service import (
    RunService,
    _apply_dq_schema_tests,
    _claim_run,
    _finalize_run,
    dispatch_run,
)
from tests.conftest import FAKE_COMMIT_SHA


def test_apply_merges_into_existing_schema_yml(tmp_path):
    project_dir = tmp_path / "project"
    models_dir = project_dir / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: stg_orders\n    columns:\n"
        "      - name: id\n        tests: [unique]\n"
    )

    blocks = {
        "stg_orders": yaml.dump({
            "version": 2,
            "models": [{
                "name": "stg_orders",
                "columns": [{"name": "id", "tests": [{"not_null": {"meta": {"dq_rule": "r1"}}}]}],
            }],
        }),
    }

    _apply_dq_schema_tests(blocks, project_dir)

    doc = yaml.safe_load((models_dir / "schema.yml").read_text())
    model = doc["models"][0]
    col = next(c for c in model["columns"] if c["name"] == "id")
    test_names = {next(iter(t)) if isinstance(t, dict) else t for t in col["tests"]}
    assert test_names == {"unique", "not_null"}
    # No standalone generated file — the model was already documented.
    assert not (project_dir / "models" / "_dq_generated_tests.yml").exists()


def test_apply_writes_generated_file_for_undocumented_models(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "models").mkdir(parents=True)

    blocks = {
        "stg_new": yaml.dump({"version": 2, "models": [{"name": "stg_new", "columns": []}]}),
    }
    _apply_dq_schema_tests(blocks, project_dir)

    generated = project_dir / "models" / "_dq_generated_tests.yml"
    assert generated.exists()
    doc = yaml.safe_load(generated.read_text())
    assert doc["models"][0]["name"] == "stg_new"


def test_apply_is_a_noop_for_empty_blocks(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _apply_dq_schema_tests({}, project_dir)  # must not raise
    assert list(project_dir.iterdir()) == []


# ---------------------------------------------------------------------
# RUN-DBT-003: claim / dispatch / finalize state machine (with leases)
# ---------------------------------------------------------------------
async def _make_project_and_run(run_db, *, status: str, **run_kw) -> tuple[uuid.UUID, uuid.UUID]:
    async with run_db() as db:
        project = Project(
            org_id=get_settings().org_id, name="analytics", git_url="https://github.com/acme/analytics.git",
            git_ref="main", engine="pg_lake",
        )
        db.add(project)
        await db.flush()
        run = Run(project_id=project.id, trigger="manual", status=status, **run_kw)
        db.add(run)
        await db.commit()
        return run.id, project.id


class _FakeGitService:
    def __init__(self, clone_dir):
        self._clone_dir = clone_dir

    def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
        return self._clone_dir

    def head_sha(self, path):
        return FAKE_COMMIT_SHA

    def tree_sha(self, clone_dir, project_dir):
        return "7" * 40

    def cleanup(self, run_id):
        pass


class _FakeIsolation:
    def __init__(self, result, hook=None):
        self._result = result
        self._hook = hook
        self.calls = 0

    async def execute_run(self, **kwargs):
        self.calls += 1
        if self._hook:
            await self._hook(kwargs)
        return dict(self._result)

    async def cancel_job(self, **kwargs):
        return False


def _patch_dispatch_deps(monkeypatch, tmp_path, *, result, hook=None):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(exist_ok=True)
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    iso = _FakeIsolation(result, hook)
    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: _FakeGitService(clone_dir))
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: iso)
    return iso


async def test_claim_run_transitions_queued_to_running_with_lease(run_db):
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    attempt = await _claim_run(run_id)
    assert attempt == 1
    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "running"
        assert run.started_at is not None
        assert run.lease_owner == run_lease.INSTANCE_ID
        assert run.lease_expires_at > datetime.now(timezone.utc)
        assert run.attempt == 1


async def test_claim_run_is_none_when_not_queued(run_db):
    run_id, _ = await _make_project_and_run(run_db, status="running")
    assert await _claim_run(run_id) is None


async def test_concurrent_claims_only_one_wins(run_db):
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    results = await asyncio.gather(*[_claim_run(run_id) for _ in range(5)])
    assert sorted(r for r in results if r is not None) == [1]
    assert results.count(None) == 4


async def test_dispatch_run_executes_and_finalizes_success(run_db, monkeypatch, tmp_path):
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    result = {"success": True, "errors": [], "nodes": [{"node_id": "model.x.y", "status": "success"}]}
    _patch_dispatch_deps(monkeypatch, tmp_path, result=result)

    await dispatch_run(run_id)

    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "succeeded"
        assert run.git_sha == FAKE_COMMIT_SHA
        assert run.completed_at is not None
        assert run.lease_owner is None  # lease released on finalize
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        assert len(events) == 1


async def test_dispatch_run_finalizes_as_failed_on_dbt_failure(run_db, monkeypatch, tmp_path):
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    _patch_dispatch_deps(monkeypatch, tmp_path,
                         result={"success": False, "errors": ["Compilation Error"], "nodes": []})
    await dispatch_run(run_id)
    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_message == "Compilation Error"


async def test_dispatch_run_is_a_noop_when_run_is_not_queued(run_db, monkeypatch):
    run_id, _ = await _make_project_and_run(run_db, status="running")

    def _must_not_be_called():
        raise AssertionError("dispatch executed a run that was not queued")

    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: _must_not_be_called())
    await dispatch_run(run_id)
    async with run_db() as db:
        assert (await db.get(Run, run_id)).status == "running"


async def test_finalize_run_is_idempotent(run_db):
    run_id, project_id = await _make_project_and_run(run_db, status="running")
    result = {"success": True, "errors": [], "nodes": [{"node_id": "model.x.y", "status": "success"}]}
    assert await _finalize_run(run_id, project_id, "sha1", result) is True
    assert await _finalize_run(run_id, project_id, "sha1", result) is False
    async with run_db() as db:
        assert (await db.get(Run, run_id)).status == "succeeded"
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        assert len(events) == 1


async def test_finalize_run_by_a_stale_owner_is_rejected(run_db):
    """A process that lost its lease (another replica took over) must not
    finalize the successor's attempt."""
    run_id, project_id = await _make_project_and_run(run_db, status="queued")
    attempt = await _claim_run(run_id)
    ok = await _finalize_run(run_id, project_id, None, {"success": True, "errors": [], "nodes": []},
                             attempt=attempt, owner="some-other-process")
    assert ok is False
    ok = await _finalize_run(run_id, project_id, None, {"success": True, "errors": [], "nodes": []},
                             attempt=attempt + 1, owner=run_lease.INSTANCE_ID)
    assert ok is False
    async with run_db() as db:
        assert (await db.get(Run, run_id)).status == "running"


async def test_full_restart_recovery_queued_run_never_dispatched(run_db, monkeypatch, tmp_path):
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": True, "errors": [], "nodes": []})
    await dispatch_run(run_id)
    async with run_db() as db:
        assert (await db.get(Run, run_id)).status == "succeeded"


async def test_heartbeat_renews_the_lease_while_running(run_db, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "run_lease_seconds", 1)
    run_id, _ = await _make_project_and_run(run_db, status="queued")
    seen = {}

    async def slow(kwargs):
        async with run_db() as db:
            seen["before"] = (await db.get(Run, run_id)).heartbeat_at
        await asyncio.sleep(0.8)
        async with run_db() as db:
            seen["after"] = (await db.get(Run, run_id)).heartbeat_at

    _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": True, "errors": [], "nodes": []},
                         hook=slow)
    await dispatch_run(run_id)
    assert seen["after"] > seen["before"]


# ---------------------------------------------------------------------
# RUN-DBT-002j: cancel race
# ---------------------------------------------------------------------
async def _cancel(run_db, run_id, project_id):
    async with run_db() as db:
        project = await db.get(Project, project_id)
        return await RunService(db).cancel(project, run_id)


async def test_cancel_queued_run_then_dispatch_never_starts_it(run_db, monkeypatch, tmp_path):
    run_id, project_id = await _make_project_and_run(run_db, status="queued")
    iso = _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": True, "errors": [], "nodes": []})

    run = await _cancel(run_db, run_id, project_id)
    assert run.status == "cancelled"
    await dispatch_run(run_id)

    assert iso.calls == 0
    async with run_db() as db:
        assert (await db.get(Run, run_id)).status == "cancelled"


async def test_cancel_during_execution_is_not_overwritten_by_finalize(run_db, monkeypatch, tmp_path):
    run_id, project_id = await _make_project_and_run(run_db, status="queued")

    async def cancel_mid_run(kwargs):
        await _cancel(run_db, run_id, project_id)

    _patch_dispatch_deps(monkeypatch, tmp_path, result={"success": True, "errors": [], "nodes": []},
                         hook=cancel_mid_run)
    await dispatch_run(run_id)
    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "cancelled"
        events = (await db.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars().all()
        assert events == []


async def test_concurrent_cancels_exactly_one_wins(run_db):
    run_id, project_id = await _make_project_and_run(run_db, status="running")
    results = await asyncio.gather(*[_cancel(run_db, run_id, project_id) for _ in range(3)],
                                   return_exceptions=True)
    ok = [r for r in results if isinstance(r, Run)]
    conflicts = [r for r in results if isinstance(r, DbtRunnerError) and r.status_code == 409]
    assert len(ok) == 1 and len(conflicts) == 2


async def test_cancel_terminal_run_conflicts_and_unknown_run_404s(run_db):
    run_id, project_id = await _make_project_and_run(run_db, status="succeeded")
    with pytest.raises(DbtRunnerError) as e:
        await _cancel(run_db, run_id, project_id)
    assert e.value.status_code == 409
    with pytest.raises(DbtRunnerError) as e:
        await _cancel(run_db, uuid.uuid4(), project_id)
    assert e.value.status_code == 404


async def test_cancel_races_k8s_release_and_the_job_never_starts(run_db, monkeypatch):
    """End-to-end over the real lease + real isolation code with a fake k8s:
    the run is cancelled after its (suspended) Job was created but before
    release. The release re-check sees 'cancelled', so the Job is never
    unsuspended and is withdrawn."""
    from tests.test_isolation import FakeK8s, _k8s_settings

    from app.executors import isolation

    settings = _k8s_settings()
    run_id, project_id = await _make_project_and_run(run_db, status="queued")
    fake = FakeK8s()
    pending = []
    fake.on_job_created = lambda body: pending.append(body.metadata.name)
    monkeypatch.setattr(isolation, "_ensure_k8s_config", lambda: None)
    monkeypatch.setattr(isolation.client, "BatchV1Api", lambda: fake)
    monkeypatch.setattr(isolation.client, "CoreV1Api", lambda: fake)
    monkeypatch.setattr(isolation, "get_settings", lambda: settings)
    monkeypatch.setattr("app.services.run_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: isolation.RunnerIsolation())

    real_create_secret = fake.create_namespaced_secret

    def create_secret_then_cancel(namespace, body):
        real_create_secret(namespace, body)
        # cancel lands between Secret creation and the release step
        import asyncio as _a
        fut = _a.run_coroutine_threadsafe(_cancel(run_db, run_id, project_id), loop)
        fut.result(timeout=10)

    fake.create_namespaced_secret = create_secret_then_cancel
    loop = asyncio.get_running_loop()

    await dispatch_run(run_id)

    assert pending, "the Job should have been created (suspended)"
    assert fake.patches == [], "a cancelled run's Job must never be released"
    assert fake.jobs == {}, "the suspended Job must be withdrawn"
    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "cancelled"
        assert run.k8s_job_name == isolation._job_name(run_id, 1)
