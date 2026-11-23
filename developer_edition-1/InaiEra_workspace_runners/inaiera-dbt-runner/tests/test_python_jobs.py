"""Python jobs: a registered project of kind "python" is one .py script of a repository, run in its own virtualenv.

The scripts here are really run (a child process of the test interpreter); packages come from a local wheel through
`--no-index --find-links`, so nothing needs the network. Everything after execution (manifest, dispatch, schedules, Dependents,
Dagster) is shared with the other kinds and covered by their tests; these cover what is new.
"""
from __future__ import annotations

import base64
import hashlib
import sys
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest

from app.core.config import get_settings
from app.executors import isolation
from app.executors.python_job_executor import engine_environment, run_python_file
from app.main import app
from app.models import Project, Run
from app.services import run_manifest
from app.services.run_service import dispatch_run, freeze_project
from tests.test_isolation import no_real_k8s  # noqa: F401  (autouse: never a real kube config)


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _script(tmp_path: Path, body: str, name="job.py") -> Path:
    f = tmp_path / "jobs" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return f


def _wheel(directory: Path, name="smallpkg", value="from-the-wheel") -> None:
    """A minimal installable wheel (a zip with dist-info), so `pip install` works offline."""
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        f"{name}/__init__.py": f"VALUE = {value!r}\n",
        f"{name}-1.0.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        f"{name}-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{n},sha256={base64.urlsafe_b64encode(hashlib.sha256(c.encode()).digest()).rstrip(b'=').decode()},{len(c)}\n"
                     for n, c in files.items()) + f"{name}-1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(directory / f"{name}-1.0-py3-none-any.whl", "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
        z.writestr(f"{name}-1.0.dist-info/RECORD", record)


# ── running a script ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_script_runs_and_its_output_is_the_steps_output(tmp_path):
    f = _script(tmp_path, "print('hello from the job')\nprint('second line')\n")
    result = run_python_file(py_path=f, engine="pg_lake", run_id="r1", git_sha="a" * 40)
    assert result["success"] is True and result["errors"] == []
    (node,) = result["nodes"]
    assert (node["node_id"], node["status"], node["step"]) == ("step.1", "success", "run script")
    assert node["output"].splitlines() == ["hello from the job", "second line"]


def test_a_failing_script_fails_the_run_with_its_exit_code_and_the_end_of_its_output(tmp_path):
    f = _script(tmp_path, "import sys\nprint('about to fail')\nraise SystemExit(3)\n")
    result = run_python_file(py_path=f, engine="pg_lake")
    assert result["success"] is False
    assert result["nodes"][0]["status"] == "error" and "exit code 3" in result["nodes"][0]["message"]
    assert "about to fail" in result["nodes"][0]["output"]
    assert result["errors"][0].startswith("the script failed (exit code 3)") and "about to fail" in result["errors"][0]


def test_an_exception_shows_its_traceback(tmp_path):
    f = _script(tmp_path, "def go():\n    return 1 / 0\ngo()\n")
    result = run_python_file(py_path=f, engine="pg_lake")
    assert result["success"] is False and "ZeroDivisionError" in result["nodes"][0]["output"]


def test_a_script_that_runs_too_long_is_killed_with_its_children(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "python_job_timeout_seconds", 1)
    marker = tmp_path / "child-survived"
    f = _script(tmp_path, f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', \"import time; time.sleep(3); open({str(marker)!r}, 'w')\"])\ntime.sleep(60)\n")
    result = run_python_file(py_path=f, engine="pg_lake")
    assert result["success"] is False and "ran longer than 1s and was stopped" in result["errors"][0]
    import time
    time.sleep(3.5)
    assert not marker.exists()                         # the whole process group died, not just the script


def test_output_is_capped_to_its_end(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "python_job_output_max_chars", 1000)
    f = _script(tmp_path, "for i in range(2000):\n    print('line', i)\n")
    out = run_python_file(py_path=f, engine="pg_lake")["nodes"][0]["output"]
    assert len(out) <= 1001 and out.startswith("…") and out.rstrip().endswith("line 1999")


# ── what the script can see ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_script_gets_a_minimal_environment_never_the_runners_secrets(tmp_path, monkeypatch):
    for name, value in {"SERVICE_JWT_SECRET": "runner-signing-secret", "GIT_TOKEN": "ghp_runnertoken", "SCHEDULER_JWT_SECRET": "sched-secret",
                        "WORKSPACE_JOB_JWT_SECRET": "ws-secret", "RUNNER_GIT_TOKEN": "ghp_other", "DATABASE_PASSWORD": "dbpass"}.items():
        monkeypatch.setenv(name, value)
    f = _script(tmp_path, "import os, json\nprint(json.dumps(sorted(os.environ)))\n")
    import json
    seen = set(json.loads(run_python_file(py_path=f, engine="pg_lake", run_id="r9", git_sha="b" * 40)["nodes"][0]["output"]))
    assert not seen & {"SERVICE_JWT_SECRET", "GIT_TOKEN", "SCHEDULER_JWT_SECRET", "WORKSPACE_JOB_JWT_SECRET", "RUNNER_GIT_TOKEN", "DATABASE_PASSWORD"}
    assert {"INAIERA_RUN_ID", "INAIERA_GIT_SHA", "INAIERA_ENGINE", "INAIERA_JOB_KIND", "PATH", "HOME"} <= seen


def test_the_pg_lake_connection_is_handed_over_and_a_printed_password_is_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "pg_lake_dsn", "postgresql://etl:s3cr3t-pw@lake.internal:5433/warehouse")
    assert engine_environment("pg_lake")["PGHOST"] == "lake.internal"
    f = _script(tmp_path, "import os\nprint(os.environ['PGUSER'], os.environ['PGPORT'], os.environ['PGDATABASE'], os.environ['PGPASSWORD'])\n")
    out = run_python_file(py_path=f, engine="pg_lake")["nodes"][0]["output"]
    assert out.strip() == "etl 5433 warehouse ***"


def test_trino_gets_its_own_variables(monkeypatch):
    monkeypatch.setattr(get_settings(), "trino_host", "trino.internal")
    monkeypatch.setenv("TRINO_JWT", "jwt-value")
    env = engine_environment("trino")
    assert env["INAIERA_TRINO_HOST"] == "trino.internal" and env["INAIERA_TRINO_JWT"] == "jwt-value" and "PGHOST" not in env


# ── requirements.txt ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_requirements_next_to_the_script_are_installed_into_a_virtualenv_of_their_own(tmp_path):
    _wheel(tmp_path / "wheels")
    f = _script(tmp_path, "import smallpkg\nprint(smallpkg.VALUE)\n")
    (f.parent / "requirements.txt").write_text(f"--no-index\n--find-links={tmp_path / 'wheels'}\nsmallpkg==1.0\n")
    result = run_python_file(py_path=f, engine="pg_lake")
    assert result["success"] is True, result
    assert [(n["node_id"], n["status"]) for n in result["nodes"]] == [("step.1", "success"), ("step.2", "success")]
    assert result["nodes"][1]["output"].strip() == "from-the-wheel"
    import importlib.util
    assert importlib.util.find_spec("smallpkg") is None        # it never reached the runner's own interpreter


def test_a_requirements_file_that_cannot_be_installed_fails_before_the_script_runs(tmp_path):
    f = _script(tmp_path, "print('must not run')\n")
    (f.parent / "requirements.txt").write_text(f"--no-index\n--find-links={tmp_path}\nno-such-package==9.9\n")
    result = run_python_file(py_path=f, engine="pg_lake")
    assert result["success"] is False
    assert [n["status"] for n in result["nodes"]] == ["error", "skipped"]
    assert "installing requirements.txt failed" in result["errors"][0] and "no-such-package" in result["errors"][0]
    assert "must not run" not in str(result)


def test_without_requirements_the_runners_own_interpreter_is_used(tmp_path):
    f = _script(tmp_path, "import sys\nprint(sys.prefix, sys.version_info[:2])\n")
    assert run_python_file(py_path=f, engine="pg_lake")["nodes"][0]["output"].strip() == f"{sys.prefix} {sys.version_info[:2]}"


# ── registration and the frozen manifest ─────────────────────────────────────────────────────────────────────────────────
def _body(**over):
    return {"git_url": f"https://github.com/acme/jobs-{uuid.uuid4().hex[:6]}.git", "git_ref": "main", "engine": "pg_lake", **over}


async def test_a_python_job_is_registered_with_its_script(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="python", project_path="jobs/pipeline.py"))
    assert r.status_code == 201, r.text
    assert (r.json()["kind"], r.json()["project_path"]) == ("python", "jobs/pipeline.py")
    listed = (await api.get("/api/v1/projects?kind=python", headers=auth_headers)).json()
    assert [p["id"] for p in listed] == [r.json()["id"]]


@pytest.mark.parametrize("body", [{"kind": "python", "project_path": "jobs/pipeline.sql"}, {"kind": "python"}, {"kind": "python", "project_path": "jobs/"},
                                  {"kind": "sql", "project_path": "jobs/pipeline.py"}, {"kind": "dbt", "project_path": "jobs/pipeline.py"}])
async def test_a_python_job_needs_a_py_file_and_the_other_kinds_cannot_point_at_one(api, auth_headers, body):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(**body))
    assert r.status_code == 422, r.text


async def test_registration_checks_the_script_is_in_the_repository(api, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "validate_project_on_register", True)

    class Git:
        def file_exists(self, *, git_url, git_ref, path, token=None):
            return path == "jobs/pipeline.py"
    monkeypatch.setattr("app.api.projects.get_git_service", lambda: Git())
    assert (await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="python", project_path="jobs/pipeline.py"))).status_code == 201
    missing = await api.post("/api/v1/projects/register", headers=auth_headers, json=_body(kind="python", project_path="jobs/nope.py"))
    assert missing.status_code == 400 and "was not found in the repository" in missing.json()["detail"]["message"]


async def test_the_manifest_says_python_and_carries_no_dq_blocks(run_db):
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="jobs/pipeline", kind="python", project_path="jobs/pipeline.py", engine="pg_lake",
                    git_url="https://github.com/acme/jobs.git", git_ref="main", dq_schema_tests={"m": "version: 2"})
        db.add(p)
        await db.commit()
        manifest = await freeze_project(db, p.id, None)
    assert run_manifest.job_kind(manifest) == "python" and manifest["dq"]["blocks"] == {}


# ── dispatch end to end ──────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_scheduled_python_job_runs_end_to_end_and_records_its_steps(run_db, tmp_path, monkeypatch):
    from tests.test_sql_jobs import _fake_git

    _script(tmp_path, "print('nightly')\n", "pipeline.py")
    _fake_git(monkeypatch, tmp_path / "jobs")
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="jobs/pipeline", kind="python", project_path="pipeline.py", engine="pg_lake",
                    git_url="https://github.com/acme/jobs.git", git_ref="main")
        db.add(p)
        await db.flush()
        run = Run(project_id=p.id, trigger="scheduled", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    await dispatch_run(run.id)
    async with run_db() as db:
        done = await db.get(Run, run.id)
    assert done.status == "succeeded" and done.models_total == 1 and done.models_succeeded == 1
    assert done.manifest["kind"] == "python" and done.source_tree_sha == "b" * 40


async def test_a_failing_python_job_fails_its_own_run_and_leaves_the_jobs_below_to_dagster(run_db, tmp_path, monkeypatch):
    from app.services import dependents
    from tests.test_sql_jobs import _fake_git

    _script(tmp_path, "raise SystemExit(7)\n", "pipeline.py")
    _fake_git(monkeypatch, tmp_path / "jobs")
    async with run_db() as db:
        up = Project(org_id=get_settings().org_id, name="jobs/pipeline", kind="python", project_path="pipeline.py", engine="pg_lake",
                     git_url="https://github.com/acme/a.git", git_ref="main")
        down = Project(org_id=get_settings().org_id, name="jobs/report", kind="dbt", project_path="report", engine="pg_lake",
                       git_url="https://github.com/acme/b.git", git_ref="main")
        db.add_all([up, down])
        await db.flush()
        await dependents.set_dependents(db, up, [down.id])
        failing = Run(project_id=up.id, trigger="scheduled", status="queued", attempt=0)
        waiting = Run(project_id=down.id, trigger="scheduled", status="queued", attempt=0)
        db.add_all([failing, waiting])
        await db.commit()
    await dispatch_run(failing.id)
    async with run_db() as db:
        a, b = await db.get(Run, failing.id), await db.get(Run, waiting.id)
    assert a.status == "failed" and "exit code 7" in a.error_message
    assert b.status == "queued"            # the rule for the jobs below is Dagster's (dependencies.py), not the Runner's


# ── Kubernetes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_python_run_in_kubernetes_mode_creates_a_python_job_with_the_package_index(monkeypatch):
    from tests.conftest import FAKE_COMMIT_SHA
    from tests.test_isolation import FakeK8s, FakeLease, _inline_log, _k8s_settings, _use

    settings = _k8s_settings(pip_index_url="https://pypi.internal/simple")
    fake = FakeK8s(statuses=[(1, None)], pod_log=_inline_log({"success": True, "errors": [], "nodes": []}))
    _use(monkeypatch, fake, settings)
    built = []
    real = isolation._build_job

    def spy(**kw):
        job = real(**kw)
        container = job.spec.template.spec.containers[0]
        built.append((container.args, {e.name: e.value for e in container.env if e.value is not None}))
        return job
    monkeypatch.setattr(isolation, "_build_job", spy)
    await isolation.RunnerIsolation().execute_run(
        run_id=uuid.uuid4(), project_dir=None, engine="pg_lake", select=None, git_url="https://github.com/acme/jobs.git", git_ref="main",
        project_id=uuid.uuid4(), lease=FakeLease(), project_path="jobs/pipeline.py", git_sha=FAKE_COMMIT_SHA, dq_schema_blocks={}, kind="python")
    (args, env), = built
    assert args[-2:] == ["--kind", "python"]
    assert env["PIP_INDEX_URL"] == "https://pypi.internal/simple" and env["PYTHON_JOB_TIMEOUT_SECONDS"] == "3600"


def test_the_job_entrypoint_runs_the_script_instead_of_dbt(tmp_path, monkeypatch, capsys):
    from app.executors import job_entrypoint
    from tests.conftest import FAKE_COMMIT_SHA
    from tests.test_sql_jobs import _fake_git

    _script(tmp_path, "print('x')\n", "pipeline.py")
    _fake_git(monkeypatch, tmp_path / "jobs")
    seen = {}

    def fake_run(*, py_path, engine, run_id=None, git_sha=None):
        seen.update(path=py_path, engine=engine, sha=git_sha)
        return {"success": True, "errors": [], "nodes": [{"node_id": "step.1", "status": "success"}]}
    monkeypatch.setattr("app.executors.python_job_executor.run_python_file", fake_run)
    monkeypatch.setattr(job_entrypoint, "get_dbt_executor", lambda: pytest.fail("dbt must not run for a Python job"))
    for k, v in {"RUNNER_GIT_URL": "https://github.com/acme/jobs.git", "RUNNER_GIT_REF": "main", "RUNNER_GIT_SHA": FAKE_COMMIT_SHA,
                 "RUNNER_PROJECT_PATH": "pipeline.py"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(get_settings(), "artifact_store_backend", "none")
    assert job_entrypoint.main(["--run-id", str(uuid.uuid4()), "--engine", "pg_lake", "--kind", "python"]) == 0
    assert seen["path"].name == "pipeline.py" and seen["engine"] == "pg_lake" and seen["sha"] == FAKE_COMMIT_SHA
    assert '"step.1"' in capsys.readouterr().out
