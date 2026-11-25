"""Tests for RUN-DBT-002 (k8s Job isolation).

Exercise the real Job-building, suspended-create/release, async polling,
artifact/inline result collection, ownership-checked cleanup and cancel
logic against a fake Kubernetes client (no cluster needed).
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.core.config import Settings
from app.executors import isolation
from app.executors.job_entrypoint import RESULT_END, RESULT_REF, RESULT_START
from app.services.artifact_store import LocalArtifactStore, put_run_result
from tests.conftest import FAKE_COMMIT_SHA


# ---------------------------------------------------------------------
# Fake Kubernetes client
# ---------------------------------------------------------------------
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _job_obj(body, uid, *, succeeded=None, failed=None):
    return _Obj(metadata=_Obj(name=body.metadata.name, labels=dict(body.metadata.labels), uid=uid),
                spec=_Obj(suspend=body.spec.suspend),
                status=_Obj(succeeded=succeeded, failed=failed))


class FakeK8s:
    """Single fake standing in for both BatchV1Api and CoreV1Api."""

    def __init__(self, *, statuses=None, pod_log=""):
        self.jobs: dict[str, object] = {}
        self.secrets: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.deleted_jobs: list[str] = []
        self.patches: list[tuple] = []
        self._statuses = statuses or [(1, None)]  # (succeeded, failed) per status read
        self._status_reads = 0
        self.pod_log = pod_log
        self.fail_job_create: int | None = None
        self.fail_secret_create: int | None = None
        self.on_job_created = None

    # batch
    def create_namespaced_job(self, namespace, body):
        self.calls.append(("create_job", body.metadata.name, body.spec.suspend))
        if self.fail_job_create:
            raise isolation.ApiException(status=self.fail_job_create)
        if body.metadata.name in self.jobs:
            raise isolation.ApiException(status=409)
        obj = _job_obj(body, uid=str(uuid.uuid4()))
        self.jobs[body.metadata.name] = obj
        if self.on_job_created:
            self.on_job_created(body)
        return obj

    def read_namespaced_job(self, name, namespace):
        if name not in self.jobs:
            raise isolation.ApiException(status=404)
        return self.jobs[name]

    def read_namespaced_job_status(self, name, namespace):
        if name not in self.jobs:
            raise isolation.ApiException(status=404)
        succeeded, failed = self._statuses[min(self._status_reads, len(self._statuses) - 1)]
        self._status_reads += 1
        job = self.jobs[name]
        job.status = _Obj(succeeded=succeeded, failed=failed)
        return job

    def patch_namespaced_job(self, name, namespace, body):
        self.patches.append((name, body))
        if name not in self.jobs:
            raise isolation.ApiException(status=404)
        self.jobs[name].spec.suspend = body["spec"]["suspend"]

    def delete_namespaced_job(self, name, namespace, body=None, **kw):
        job = self.jobs.get(name)
        if job is None:
            raise isolation.ApiException(status=404)
        if body is not None and body.preconditions and body.preconditions.uid != job.metadata.uid:
            raise isolation.ApiException(status=409)
        self.deleted_jobs.append(name)
        del self.jobs[name]
        # ownerReference GC of the attempt's Secret
        for sname, s in list(self.secrets.items()):
            refs = s.metadata.owner_references or []
            if any(r.uid == job.metadata.uid for r in refs):
                del self.secrets[sname]

    # core
    def create_namespaced_secret(self, namespace, body):
        self.calls.append(("create_secret", body.metadata.name))
        if self.fail_secret_create:
            raise isolation.ApiException(status=self.fail_secret_create)
        self.secrets[body.metadata.name] = body

    def list_namespaced_pod(self, namespace, label_selector=None):
        return _Obj(items=[_Obj(metadata=_Obj(name="dbtrun-pod-abc123"))])

    def read_namespaced_pod_log(self, name, namespace, **kw):
        return self.pod_log


class FakeLease:
    def __init__(self, *, attempt=1, owned=True, release_ok=True):
        self.attempt = attempt
        self.owner = "test-owner"
        self.lost = asyncio.Event()
        self.owned = owned
        self.release_ok = release_ok
        self.recorded: list[str] = []
        self.released = False

    async def still_owner(self):
        return self.owned

    async def record_job_name(self, name):
        if not self.owned:
            return False
        self.recorded.append(name)
        return True

    async def release_job(self, release):
        if not (self.owned and self.release_ok):
            return False
        await release()
        self.released = True
        return True


@pytest.fixture(autouse=True)
def no_real_k8s(monkeypatch):
    monkeypatch.setattr(isolation, "_ensure_k8s_config", lambda: None)
    monkeypatch.setattr(isolation, "_POLL_INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(isolation, "_POLL_MAX_DELAY_SECONDS", 0.01)


def _use(monkeypatch, fake: FakeK8s, settings: Settings):
    monkeypatch.setattr(isolation.client, "BatchV1Api", lambda: fake)
    monkeypatch.setattr(isolation.client, "CoreV1Api", lambda: fake)
    monkeypatch.setattr(isolation, "get_settings", lambda: settings)


def _k8s_settings(**kw) -> Settings:
    base = dict(runner_mode="k8s_job", environment="dev", artifact_store_backend="none",
                runner_job_timeout_seconds=60)
    base.update(kw)
    return Settings(**base)


def _inline_log(result: dict) -> str:
    return f"some dbt log noise\n{RESULT_START}\n{json.dumps(result)}\n{RESULT_END}\ndone\n"


# ---------------------------------------------------------------------
# Mode dispatch / environment guard (RUN-DBT-002a)
# ---------------------------------------------------------------------
async def test_subprocess_mode_runs_off_the_event_loop(monkeypatch, tmp_path):
    settings = Settings(runner_mode="subprocess", environment="dev")
    monkeypatch.setattr(isolation, "get_settings", lambda: settings)
    called = {}

    class FakeExecutor:
        def run_project(self, *, project_dir, engine, select, run_id):
            import threading
            called["thread"] = threading.current_thread().name
            called["run_id"] = run_id
            return {"success": True, "errors": [], "nodes": []}

    monkeypatch.setattr(isolation, "get_dbt_executor", lambda: FakeExecutor())
    run_id = uuid.uuid4()
    result = await isolation.RunnerIsolation().execute_run(
        run_id=run_id, project_dir=tmp_path, engine="pg_lake", select=None)
    assert result["success"] is True
    assert called["run_id"] == run_id
    assert called["thread"].startswith("dbt-subprocess")


@pytest.mark.parametrize("env", ["staging", "prod"])
async def test_subprocess_mode_refused_in_staging_and_prod(monkeypatch, tmp_path, env):
    monkeypatch.setattr(isolation, "get_settings",
                        lambda: Settings(runner_mode="subprocess", environment=env))
    with pytest.raises(RuntimeError, match="not permitted"):
        await isolation.RunnerIsolation().execute_run(
            run_id=uuid.uuid4(), project_dir=tmp_path, engine="pg_lake", select=None)


async def test_subprocess_mode_does_not_start_when_no_longer_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "get_settings", lambda: Settings(runner_mode="subprocess"))
    monkeypatch.setattr(isolation, "get_dbt_executor", lambda: pytest.fail("must not run"))
    result = await isolation.RunnerIsolation().execute_run(
        run_id=uuid.uuid4(), project_dir=tmp_path, engine="pg_lake", select=None,
        lease=FakeLease(owned=False))
    assert result["_aborted"] is True


async def test_k8s_job_mode_requires_git_project_and_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "get_settings", lambda: _k8s_settings())
    with pytest.raises(ValueError):
        await isolation.RunnerIsolation().execute_run(
            run_id=uuid.uuid4(), project_dir=tmp_path, engine="pg_lake", select=None)
    with pytest.raises(ValueError, match="lease"):
        await isolation.RunnerIsolation().execute_run(
            run_id=uuid.uuid4(), project_dir=None, engine="pg_lake", select=None,
            git_url="https://x/y.git", git_ref="main", project_id=uuid.uuid4(), git_sha=FAKE_COMMIT_SHA)


# ---------------------------------------------------------------------
# Job spec (RUN-DBT-002 d/e/f)
# ---------------------------------------------------------------------
def _env(job):
    return {e.name: e for e in job.spec.template.spec.containers[0].env}


def test_build_job_references_existing_secret_for_pg_lake_dsn():
    settings = _k8s_settings(pg_lake_dsn_secret_name="dbtrunner-pglake-dsn", pg_lake_dsn_secret_key="dsn",
                             pg_lake_dsn="postgresql://should-not-appear-in-spec")
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None,
                               settings=settings)
    dsn = _env(job)["PG_LAKE_DSN"]
    assert dsn.value is None
    assert dsn.value_from.secret_key_ref.name == "dbtrunner-pglake-dsn"
    assert "should-not-appear" not in json.dumps(job.to_dict(), default=str)


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_build_job_refuses_plaintext_dsn_outside_dev(env):
    settings = _k8s_settings(environment=env, pg_lake_dsn="postgresql://u:p@h/db",
                             artifact_store_backend="s3", artifact_s3_bucket="b")
    with pytest.raises(isolation.JobSpecError, match="PG_LAKE_DSN_SECRET_NAME"):
        isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None,
                             settings=settings)


def test_build_job_plaintext_dsn_allowed_only_in_dev():
    settings = _k8s_settings(pg_lake_dsn="postgresql://dev-only-dsn")
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None,
                               settings=settings)
    assert _env(job)["PG_LAKE_DSN"].value == "postgresql://dev-only-dsn"


def test_build_job_injects_trino_jwt_from_secret():
    settings = _k8s_settings(environment="prod", trino_host="trino.svc", trino_jwt_secret_name="trino-jwt",
                             trino_jwt_secret_key="token", artifact_store_backend="s3",
                             artifact_s3_bucket="runs", artifact_s3_credentials_secret_name="s3-creds")
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="trino", select=None,
                               settings=settings)
    env = _env(job)
    assert env["TRINO_JWT"].value is None
    assert env["TRINO_JWT"].value_from.secret_key_ref.name == "trino-jwt"
    assert env["TRINO_HOST"].value == "trino.svc"
    assert env["AWS_ACCESS_KEY_ID"].value_from.secret_key_ref.name == "s3-creds"
    assert env["ARTIFACT_STORE_BACKEND"].value == "s3"
    assert env["OPENLINEAGE_NAMESPACE"].value == settings.openlineage_namespace


def test_build_job_refuses_trino_without_jwt_secret_outside_dev():
    settings = _k8s_settings(environment="staging", trino_host="trino.svc",
                             artifact_store_backend="s3", artifact_s3_bucket="b")
    with pytest.raises(isolation.JobSpecError, match="TRINO_JWT_SECRET_NAME"):
        isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="trino", select=None,
                             settings=settings)


def test_build_job_requires_durable_artifact_store_outside_dev():
    settings = _k8s_settings(environment="prod", pg_lake_dsn_secret_name="s", artifact_store_backend="local")
    with pytest.raises(isolation.JobSpecError, match="ARTIFACT_STORE_BACKEND=s3"):
        isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None,
                             settings=settings)


def test_build_job_security_resources_ttl_and_suspend():
    settings = _k8s_settings(
        runner_job_timeout_seconds=1800, runner_job_ttl_seconds=900,
        runner_job_cpu_request="500m", runner_job_cpu_limit="4",
        runner_job_memory_request="1Gi", runner_job_memory_limit="4Gi",
        runner_job_service_account="dbt-runner-job", runner_job_pod_labels={"app.kubernetes.io/instance": "x"},
    )
    run_id = uuid.uuid4()
    job = isolation._build_job(run_id=run_id, attempt=3, engine="pg_lake", select="tag:nightly",
                               settings=settings)
    assert job.metadata.name == f"dbtrun-{run_id}-a3" and len(job.metadata.name) <= 63
    assert job.spec.suspend is True
    assert job.spec.backoff_limit == 0
    assert job.spec.active_deadline_seconds == 1800
    assert job.spec.ttl_seconds_after_finished == 900

    pod = job.spec.template.spec
    assert pod.restart_policy == "Never"
    assert pod.automount_service_account_token is False
    assert pod.service_account_name == "dbt-runner-job"
    assert pod.security_context.seccomp_profile.type == "RuntimeDefault"
    assert pod.security_context.run_as_user == 10001
    assert pod.security_context.run_as_group == 10001
    assert job.spec.template.metadata.labels["app.kubernetes.io/instance"] == "x"
    assert job.spec.template.metadata.labels[isolation.ATTEMPT_LABEL] == "3"

    c = pod.containers[0]
    sc = c.security_context
    assert sc.run_as_non_root and sc.allow_privilege_escalation is False
    assert sc.read_only_root_filesystem and sc.capabilities.drop == ["ALL"]
    assert sc.seccomp_profile.type == "RuntimeDefault"
    assert sc.run_as_user == 10001 and sc.run_as_group == 10001
    assert c.resources.requests == {"cpu": "500m", "memory": "1Gi"}
    assert c.resources.limits == {"cpu": "4", "memory": "4Gi"}
    assert c.args == ["--run-id", str(run_id), "--attempt", "3", "--engine", "pg_lake",
                      "--select", "tag:nightly"]
    for key in ("RUNNER_GIT_URL", "RUNNER_GIT_REF", "RUNNER_GIT_SHA", "RUNNER_PROJECT_PATH",
                "RUNNER_DQ_SCHEMA_TESTS_JSON"):
        assert _env(job)[key].value_from.secret_key_ref.name == isolation._secret_name(run_id, 3)


def test_build_secret_is_owned_by_the_job():
    run_id = uuid.uuid4()
    secret = isolation._build_secret(
        run_id=run_id, attempt=2, namespace="inaiera", git_url="https://x-token:abc@github.com/a/b.git",
        git_ref="main", dq_schema_blocks={"stg_orders": "models: []\n"}, settings=_k8s_settings(),
        owner_job_uid="uid-123", project_path="dbt/sales", git_sha=FAKE_COMMIT_SHA)
    assert secret.string_data["GIT_SHA"] == FAKE_COMMIT_SHA
    assert secret.metadata.name == isolation._secret_name(run_id, 2)
    ref = secret.metadata.owner_references[0]
    assert (ref.kind, ref.name, ref.uid) == ("Job", isolation._job_name(run_id, 2), "uid-123")
    assert secret.string_data["GIT_URL"].startswith("https://x-token")
    assert secret.string_data["PROJECT_PATH"] == "dbt/sales"
    assert json.loads(secret.string_data["DQ_SCHEMA_TESTS_JSON"]) == {"stg_orders": "models: []\n"}


# ---------------------------------------------------------------------
# Submit / release / poll / collect
# ---------------------------------------------------------------------
async def _run(fake, settings, monkeypatch, lease=None, run_id=None):
    _use(monkeypatch, fake, settings)
    lease = lease or FakeLease()
    run_id = run_id or uuid.uuid4()
    result = await isolation.RunnerIsolation()._execute_k8s_job(
        lease=lease, run_id=run_id, engine="pg_lake", select=None,
        git_url="https://github.com/acme/analytics.git", git_ref="main", project_id=uuid.uuid4(),
        git_sha=FAKE_COMMIT_SHA, dq_schema_blocks={"stg_orders": "models: []\n"})
    return result, lease, run_id


async def test_job_is_created_suspended_then_released_and_result_read_inline(monkeypatch):
    dbt_result = {"success": True, "errors": [], "nodes": [{"node_id": "model.x.y", "status": "success"}]}
    fake = FakeK8s(statuses=[(None, None), (1, None)], pod_log=_inline_log(dbt_result))
    result, lease, run_id = await _run(fake, _k8s_settings(), monkeypatch)

    job_name = isolation._job_name(run_id, 1)
    assert lease.recorded == [job_name]  # name recorded before creation
    assert fake.calls[0] == ("create_job", job_name, True)  # created suspended
    assert fake.calls[1] == ("create_secret", isolation._secret_name(run_id, 1))
    assert fake.patches == [(job_name, {"spec": {"suspend": False}})]
    assert result.pop("_k8s_job") == {"name": job_name, "attempt": 1}
    assert result == dbt_result
    assert fake.deleted_jobs == []  # cleanup is the caller's job, after persisting


async def test_result_is_read_from_artifact_store_via_reference_marker(monkeypatch, tmp_path):
    settings = _k8s_settings(artifact_store_backend="local", artifact_store_local_dir=str(tmp_path))
    run_id = uuid.uuid4()
    big = {"success": True, "errors": [], "nodes": [], "manifest": {"nodes": {"x": "y" * 100000}}}
    ref = put_run_result(LocalArtifactStore(tmp_path), run_id=run_id, attempt=1, result=big)
    fake = FakeK8s(statuses=[(1, None)], pod_log=f"noise\n{RESULT_REF} {json.dumps(ref)}\n")
    result, _, _ = await _run(fake, settings, monkeypatch, run_id=run_id)
    assert result["manifest"] == big["manifest"]
    assert result["_artifact_uri"] == ref["uri"]


async def test_result_falls_back_to_deterministic_key_when_logs_are_gone(monkeypatch, tmp_path):
    settings = _k8s_settings(artifact_store_backend="local", artifact_store_local_dir=str(tmp_path))
    run_id = uuid.uuid4()
    put_run_result(LocalArtifactStore(tmp_path), run_id=run_id, attempt=1,
                   result={"success": False, "errors": ["boom"], "nodes": []})
    fake = FakeK8s(statuses=[(None, 1)], pod_log="")
    result, _, _ = await _run(fake, settings, monkeypatch, run_id=run_id)
    assert result["errors"] == ["boom"]


async def test_pod_crash_without_result_is_a_clear_failure(monkeypatch):
    fake = FakeK8s(statuses=[(None, 1)], pod_log="Traceback (most recent call last):\nOOMKilled\n")
    result, _, _ = await _run(fake, _k8s_settings(), monkeypatch)
    assert result["success"] is False
    assert "OOMKilled" in result["errors"][0]


async def test_cancelled_before_creation_creates_nothing(monkeypatch):
    fake = FakeK8s()
    result, _, _ = await _run(fake, _k8s_settings(), monkeypatch, lease=FakeLease(owned=False))
    assert result["_aborted"] is True
    assert fake.calls == []


async def test_cancelled_before_release_withdraws_job_and_never_unsuspends(monkeypatch):
    fake = FakeK8s()
    result, _, run_id = await _run(fake, _k8s_settings(), monkeypatch, lease=FakeLease(release_ok=False))
    assert result["_aborted"] is True
    assert fake.patches == []  # never released -> no pod ever started
    assert fake.deleted_jobs == [isolation._job_name(run_id, 1)]
    assert fake.secrets == {}  # GC'd with its owner Job


async def test_job_create_failure_reports_and_creates_no_secret(monkeypatch):
    fake = FakeK8s()
    fake.fail_job_create = 500
    result, _, _ = await _run(fake, _k8s_settings(), monkeypatch)
    assert result["success"] is False and "failed to submit" in result["errors"][0]
    assert [c[0] for c in fake.calls] == ["create_job"]


async def test_job_name_conflict_with_foreign_job_is_never_deleted(monkeypatch):
    """409 on create for a Job this attempt did not create: fail without
    touching (deleting/adopting) the other resource."""
    run_id = uuid.uuid4()
    fake = FakeK8s()
    foreign = _Obj(metadata=_Obj(name=isolation._job_name(run_id, 1), labels={"app": "someone-else"},
                                 uid="foreign"), spec=_Obj(suspend=False), status=_Obj())
    fake.jobs[isolation._job_name(run_id, 1)] = foreign
    result, _, _ = await _run(fake, _k8s_settings(), monkeypatch, run_id=run_id)
    assert result["success"] is False and "did not create" in result["errors"][0]
    assert fake.jobs[isolation._job_name(run_id, 1)] is foreign
    assert fake.deleted_jobs == []


async def test_secret_conflict_withdraws_own_job_but_leaves_foreign_secret(monkeypatch):
    fake = FakeK8s()
    fake.fail_secret_create = 409
    result, _, run_id = await _run(fake, _k8s_settings(), monkeypatch)
    assert result["success"] is False
    assert fake.deleted_jobs == [isolation._job_name(run_id, 1)]
    assert fake.patches == []


async def test_lost_lease_stops_monitoring_without_deleting(monkeypatch):
    fake = FakeK8s(statuses=[(None, None)])
    lease = FakeLease()

    async def lose_soon():
        await asyncio.sleep(0.05)
        lease.lost.set()

    asyncio.create_task(lose_soon())
    result, _, run_id = await _run(fake, _k8s_settings(), monkeypatch, lease=lease)
    assert result["_aborted"] is True
    assert fake.deleted_jobs == []  # a successor (or the cancel path) owns it now


async def test_polling_does_not_pin_a_thread_per_run(monkeypatch):
    """RUN-DBT-002i: many concurrently-polling runs share a pool smaller
    than their number; each k8s call borrows a thread only briefly."""
    settings = _k8s_settings(runner_k8s_client_threads=2)
    monkeypatch.setattr(isolation, "_k8s_pool", None)
    dbt_result = {"success": True, "errors": [], "nodes": []}
    fake = FakeK8s(statuses=[(None, None)] * 20 + [(1, None)], pod_log=_inline_log(dbt_result))
    _use(monkeypatch, fake, settings)

    async def one():
        return await isolation.RunnerIsolation()._execute_k8s_job(
            lease=FakeLease(), run_id=uuid.uuid4(), engine="pg_lake", select=None,
            git_url="https://x/y.git", git_ref="main", project_id=uuid.uuid4(),
            git_sha=FAKE_COMMIT_SHA, dq_schema_blocks={})

    results = await asyncio.wait_for(asyncio.gather(*[one() for _ in range(10)]), timeout=30)
    assert all(r["success"] for r in results)
    assert isolation._pool()._max_workers == 2
    monkeypatch.setattr(isolation, "_k8s_pool", None)


# ---------------------------------------------------------------------
# Ownership-checked cleanup / cancel / adoption
# ---------------------------------------------------------------------
async def test_cleanup_only_deletes_this_attempts_job(monkeypatch):
    run_id = uuid.uuid4()
    fake = FakeK8s()
    _use(monkeypatch, fake, _k8s_settings())
    job = isolation._build_job(run_id=run_id, attempt=2, engine="pg_lake", select=None,
                               settings=_k8s_settings())
    fake.create_namespaced_job("inaiera", job)
    iso = isolation.RunnerIsolation()
    # wrong attempt -> refused
    assert await iso.cleanup_job(run_id=run_id, attempt=1, job_name=job.metadata.name) is False
    assert job.metadata.name in fake.jobs
    assert await iso.cleanup_job(run_id=run_id, attempt=2, job_name=job.metadata.name) is True
    assert fake.deleted_jobs == [job.metadata.name]


async def test_cancel_job_missing_returns_false(monkeypatch):
    _use(monkeypatch, FakeK8s(), _k8s_settings())
    iso = isolation.RunnerIsolation()
    assert await iso.cancel_job(run_id=uuid.uuid4(), attempt=1, job_name="dbtrun-x-a1") is False
    assert await iso.cancel_job(run_id=uuid.uuid4(), attempt=1, job_name=None) is False


async def test_adopt_suspended_job_is_withdrawn_as_never_started(monkeypatch):
    run_id = uuid.uuid4()
    fake = FakeK8s()
    _use(monkeypatch, fake, _k8s_settings())
    job = isolation._build_job(run_id=run_id, attempt=1, engine="pg_lake", select=None,
                               settings=_k8s_settings())
    fake.create_namespaced_job("inaiera", job)
    result = await isolation.RunnerIsolation().adopt(lease=FakeLease(), run_id=run_id,
                                                     job_name=job.metadata.name, released=False)
    assert result["_never_started"] is True
    assert fake.deleted_jobs == [job.metadata.name]


async def test_missing_released_job_without_artifact_fails_explicitly_not_rerun(monkeypatch):
    _use(monkeypatch, FakeK8s(), _k8s_settings())
    result = await isolation.RunnerIsolation().adopt(lease=FakeLease(), run_id=uuid.uuid4(),
                                                     job_name="dbtrun-gone-a1", released=True)
    assert result["success"] is False
    assert "Not re-running" in result["errors"][0]
    assert not result.get("_never_started")


async def test_missing_job_uses_recorded_artifact(monkeypatch, tmp_path):
    settings = _k8s_settings(artifact_store_backend="local", artifact_store_local_dir=str(tmp_path))
    _use(monkeypatch, FakeK8s(), settings)
    run_id = uuid.uuid4()
    put_run_result(LocalArtifactStore(tmp_path), run_id=run_id, attempt=1,
                   result={"success": True, "errors": [], "nodes": []})
    result = await isolation.RunnerIsolation().adopt(lease=FakeLease(), run_id=run_id,
                                                     job_name="dbtrun-gone-a1", released=True)
    assert result["success"] is True


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------
def test_extract_result_parses_between_markers():
    payload = {"success": True, "errors": [], "nodes": []}
    assert isolation._extract_result(_inline_log(payload)) == payload


def test_extract_result_returns_none_without_markers():
    assert isolation._extract_result("plain dbt log output, no markers") is None


def test_extract_ref_takes_last_marker():
    text = f"{RESULT_REF} {{\"uri\": \"a\"}}\nmore\n{RESULT_REF} {{\"uri\": \"b\"}}\n"
    assert isolation._extract_ref(text) == {"uri": "b"}


# ---------------------------------------------------------------------
# DBT-006/007: the Job is handed the commit and DQ blocks frozen at submission
# ---------------------------------------------------------------------
async def test_k8s_job_mode_refuses_to_run_without_a_frozen_commit(monkeypatch):
    """A ref alone can have moved since submission; the Job must be told exactly which commit to build."""
    monkeypatch.setattr(isolation, "get_settings", lambda: _k8s_settings())
    with pytest.raises(ValueError, match="frozen git_sha"):
        await isolation.RunnerIsolation().execute_run(
            run_id=uuid.uuid4(), project_dir=None, engine="pg_lake", select=None, git_url="https://x/y.git",
            git_ref="main", project_id=uuid.uuid4(), lease=FakeLease())


async def test_the_jobs_secret_carries_the_frozen_commit_and_the_frozen_dq_blocks(monkeypatch):
    fake = FakeK8s(statuses=[(1, None)], pod_log=_inline_log({"success": True, "errors": [], "nodes": []}))
    _, _, run_id = await _run(fake, _k8s_settings(), monkeypatch)
    secret = fake.secrets[isolation._secret_name(run_id, 1)]
    assert secret.string_data["GIT_SHA"] == FAKE_COMMIT_SHA
    assert json.loads(secret.string_data["DQ_SCHEMA_TESTS_JSON"]) == {"stg_orders": "models: []\n"}


async def test_execution_never_reads_dq_blocks_from_the_database(monkeypatch):
    """The pre-fix code called run_service._fetch_dq_schema_blocks(project_id) here; the blocks now come only from the
    run's manifest, so a project whose rules changed after submission cannot change what the Job receives."""
    from app.services import run_service
    assert not hasattr(run_service, "_fetch_dq_schema_blocks")
    assert not hasattr(run_service, "_inject_dq_schema_tests")

    seen = {}
    real = isolation._build_secret

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(isolation, "_build_secret", spy)
    fake = FakeK8s(statuses=[(1, None)], pod_log=_inline_log({"success": True, "errors": [], "nodes": []}))
    await _run(fake, _k8s_settings(), monkeypatch)
    assert seen["git_sha"] == FAKE_COMMIT_SHA and seen["dq_schema_blocks"] == {"stg_orders": "models: []\n"}
