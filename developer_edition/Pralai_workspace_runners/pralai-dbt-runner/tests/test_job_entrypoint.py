"""Tests for the isolated Job's entrypoint (RUN-DBT-002).

It must clone the pinned ref, apply the DQ blocks it was handed (no DB
access of its own), run DbtExecutor.run_project(), write the result to the
durable artifact store and print only a small reference — falling back to
a size-capped inline result if the store is unavailable — and clean up its
clone regardless of outcome.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.core.config import Settings
from app.executors import job_entrypoint
from app.executors.job_entrypoint import RESULT_END, RESULT_REF, RESULT_START, size_capped_inline
from app.services.artifact_store import LocalArtifactStore, get_run_result


class _FakeGit:
    def __init__(self, clone_dir, cleaned):
        self.clone_dir = clone_dir
        self.cleaned = cleaned

    def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
        assert git_url == "https://github.com/acme/analytics.git"
        assert git_ref == "main"
        return self.clone_dir

    def head_sha(self, path):
        return "abc123"

    def tree_sha(self, clone_dir, project_dir):
        return "7" * 40

    def cleanup(self, run_id):
        self.cleaned.append(run_id)


@pytest.fixture
def job_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_GIT_URL", "https://github.com/acme/analytics.git")
    monkeypatch.setenv("RUNNER_GIT_REF", "main")
    monkeypatch.setenv("RUNNER_DQ_SCHEMA_TESTS_JSON", json.dumps({"stg_orders": "models: []\n"}))
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    cleaned: list = []
    applied: dict = {}
    monkeypatch.setattr(job_entrypoint, "get_git_service", lambda: _FakeGit(clone_dir, cleaned))
    monkeypatch.setattr("app.services.run_service._apply_dq_schema_tests",
                        lambda blocks, project_dir: applied.update(blocks=blocks, project_dir=project_dir))
    monkeypatch.setattr("app.services.run_service._resolve_project_dir", lambda d, p="": d)
    return {"clone_dir": clone_dir, "cleaned": cleaned, "applied": applied, "tmp": tmp_path}


def _executor(result):
    class FakeExecutor:
        def run_project(self, *, project_dir, engine, select, run_id):
            assert engine == "pg_lake"
            return dict(result)
    return FakeExecutor()


def test_main_writes_result_to_artifact_store_and_prints_only_a_reference(monkeypatch, job_env, capsys):
    store_dir = job_env["tmp"] / "store"
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: Settings(artifact_store_backend="local", artifact_store_local_dir=str(store_dir)))
    dbt_result = {"success": True, "errors": [], "nodes": [{"node_id": "model.x.y", "status": "success"}],
                  "manifest": {"nodes": {"big": "x" * 500_000}}}
    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: _executor(dbt_result))
    run_id = uuid.uuid4()

    rc = job_entrypoint.main(["--run-id", str(run_id), "--attempt", "2", "--engine", "pg_lake"])

    assert rc == 0
    assert job_env["applied"]["blocks"] == {"stg_orders": "models: []\n"}
    assert job_env["cleaned"] == [run_id]
    out = capsys.readouterr().out
    assert RESULT_START not in out
    ref_line = next(line for line in out.splitlines() if line.startswith(RESULT_REF))
    assert len(ref_line) < 1000  # small marker, never the manifest
    ref = json.loads(ref_line[len(RESULT_REF):])
    assert ref["success"] is True
    stored = get_run_result(LocalArtifactStore(store_dir), run_id=run_id, attempt=2,
                            ref=ref["uri"], sha256=ref["sha256"])
    assert stored["manifest"] == dbt_result["manifest"]
    assert stored["git_sha"] == "abc123"


def test_main_falls_back_to_size_capped_inline_result_when_store_fails(monkeypatch, job_env, capsys):
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: Settings(artifact_store_backend="s3", artifact_s3_bucket=None,
                                         runner_inline_result_max_bytes=10_000))
    dbt_result = {"success": False, "errors": ["Compilation Error"],
                  "nodes": [{"node_id": f"model.x.m{i}", "status": "error"} for i in range(2000)],
                  "manifest": {"nodes": {"big": "x" * 500_000}}}
    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: _executor(dbt_result))

    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake"]) == 0
    out = capsys.readouterr().out
    payload = out.split(RESULT_START, 1)[1].split(RESULT_END, 1)[0].strip()
    assert len(payload.encode()) <= 10_000
    printed = json.loads(payload)
    assert printed["success"] is False and printed["_truncated"] is True
    assert "manifest" not in printed
    assert printed["errors"] == ["Compilation Error"]


def test_size_capped_inline_keeps_small_results_intact():
    r = {"success": True, "errors": [], "nodes": [], "manifest": {"a": 1}}
    assert json.loads(size_capped_inline(r, 10_000)) == r


def test_main_cleans_up_clone_even_when_dbt_raises(monkeypatch, job_env):
    class Exploding:
        def run_project(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: Exploding())
    run_id = uuid.uuid4()
    with pytest.raises(RuntimeError, match="boom"):
        job_entrypoint.main(["--run-id", str(run_id), "--engine", "pg_lake"])
    assert job_env["cleaned"] == [run_id]


# ---------------------------------------------------------------------
# DBT-006: the Job checks out exactly the commit frozen at submission
# ---------------------------------------------------------------------
class _ShaGit(_FakeGit):
    def __init__(self, clone_dir, cleaned, *, head):
        super().__init__(clone_dir, cleaned)
        self.head = head
        self.asked_for_sha = "unset"

    def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
        self.asked_for_sha = sha
        return self.clone_dir

    def head_sha(self, path):
        return self.head


def _frozen_job(monkeypatch, job_env, *, head, env_sha="f" * 40):
    git = _ShaGit(job_env["clone_dir"], job_env["cleaned"], head=head)
    monkeypatch.setattr(job_entrypoint, "get_git_service", lambda: git)
    if env_sha is None:
        monkeypatch.delenv("RUNNER_GIT_SHA", raising=False)
    else:
        monkeypatch.setenv("RUNNER_GIT_SHA", env_sha)
    ran = []

    class Executor:
        def run_project(self, **kw):
            ran.append(kw)
            return {"success": True, "errors": [], "nodes": []}

    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: Executor())
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(artifact_store_backend="none"))
    return git, ran


def test_the_job_clones_the_frozen_commit_and_reports_it(monkeypatch, job_env, capsys):
    git, ran = _frozen_job(monkeypatch, job_env, head="f" * 40)
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake"]) == 0
    assert git.asked_for_sha == "f" * 40
    assert ran and json.loads(capsys.readouterr().out.split(RESULT_START)[1].split(RESULT_END)[0])["git_sha"] == "f" * 40


def test_the_job_refuses_to_build_a_commit_other_than_the_frozen_one(monkeypatch, job_env):
    _, ran = _frozen_job(monkeypatch, job_env, head="e" * 40)
    run_id = uuid.uuid4()
    with pytest.raises(RuntimeError, match="not the run's frozen commit"):
        job_entrypoint.main(["--run-id", str(run_id), "--engine", "pg_lake"])
    assert ran == []                                    # dbt never ran
    assert job_env["cleaned"] == [run_id]               # and the clone was still cleaned up


def test_a_job_created_by_an_older_runner_without_a_frozen_commit_still_clones_the_ref(monkeypatch, job_env, capsys):
    git, ran = _frozen_job(monkeypatch, job_env, head="abc123", env_sha=None)
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake"]) == 0
    assert git.asked_for_sha is None and ran


def test_an_empty_frozen_commit_is_treated_as_absent(monkeypatch, job_env):
    git, _ = _frozen_job(monkeypatch, job_env, head="abc123", env_sha="")
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake"]) == 0
    assert git.asked_for_sha is None


def test_the_job_reports_the_tree_hash_of_the_project_it_built(monkeypatch, job_env, capsys):
    """X-004 evidence: Git's tree id of the project folder, taken before DQ rules touch the working tree."""
    _frozen_job(monkeypatch, job_env, head="f" * 40)
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake"]) == 0
    out = json.loads(capsys.readouterr().out.split(RESULT_START)[1].split(RESULT_END)[0])
    assert out["source_tree_sha"] == "7" * 40


def test_the_size_capped_fallback_keeps_the_evidence():
    big = {"success": True, "errors": [], "nodes": [{"node_id": f"model.x.m{i}", "status": "success"} for i in range(5000)],
           "git_sha": "a" * 40, "source_tree_sha": "7" * 40, "manifest": {"nodes": {"x": "y" * 50_000}}}
    slim = json.loads(size_capped_inline(big, 400))
    assert slim["git_sha"] == "a" * 40 and slim["source_tree_sha"] == "7" * 40
