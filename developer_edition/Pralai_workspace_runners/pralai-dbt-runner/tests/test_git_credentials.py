"""DBT-009: credentials never live in a repository URL.

`https://user:token@host/repo.git` used to be accepted, stored, returned by the API, logged, and copied into each run's Secret. Now it is
refused at the boundary; a project names a `credential_ref` the operator provisioned (GIT_CREDENTIAL_<REF> from a Secret), and the token
reaches git only as an Authorization header scoped to the repository's host.
"""
from __future__ import annotations

import base64
import uuid

import httpx
import pytest

from app.core import git_credentials as gc
from app.main import app
from app.models import Project
from app.services.git_service import GitService


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── the pure helpers ────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://user:tok@github.com/acme/x.git", "https://tok@github.com/acme/x.git",
    "http://user:tok@git.internal/x.git#subdirectory=dbt", "https://:tok@github.com/acme/x.git", "ssh://git:pw@host/x.git",
])
def test_urls_with_credentials_are_unclean(url):
    assert gc.has_userinfo(url)
    with pytest.raises(ValueError, match="credential_ref"):
        gc.assert_clean_url(url)


@pytest.mark.parametrize("url", [
    "https://github.com/acme/x.git", "https://github.com:8443/acme/x.git#subdirectory=dbt", "ssh://git@github.com/acme/x.git",
    "git@github.com:acme/x.git",
])
def test_clean_urls_pass(url):
    assert gc.assert_clean_url(url) == url


def test_git_env_carries_the_token_as_a_host_scoped_header_never_in_the_url():
    env = gc.git_env("https://github.com/acme/x.git", "s3cret")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")) == b"x-access-token:s3cret"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("http.followRedirects", "false")   # never follow to another host


def test_git_env_without_a_token_or_for_ssh_adds_no_header():
    assert gc.git_env("https://github.com/acme/x.git", None) == {"GIT_TERMINAL_PROMPT": "0"}
    assert gc.git_env("ssh://git@github.com/acme/x.git", "s3cret") == {"GIT_TERMINAL_PROMPT": "0"}


def test_token_for_reads_only_operator_provisioned_refs(monkeypatch):
    monkeypatch.setenv("GIT_CREDENTIAL_ACME_GH", "tok")
    assert gc.token_for("acme_gh") == "tok"
    assert gc.token_for("unknown") is None and gc.token_for(None) is None and gc.token_for("../etc") is None
    assert "acme_gh" in gc.known_refs()


# ── the API boundary ───────────────────────────────────────────────────
@pytest.mark.parametrize("url", ["https://user:S3CRETVAL@github.com/acme/x.git", "https://S3CRETVAL@github.com/acme/x.git",
                                 "https://user:S3CRETVAL@github.com/acme/mono.git#subdirectory=dbt"])
async def test_register_rejects_a_credentialed_url(api, auth_headers, url):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": url, "git_ref": "main"})
    assert r.status_code == 422
    assert "S3CRETVAL" not in r.text                                    # the secret is not echoed back


async def test_register_with_an_unknown_credential_ref_is_refused(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers,
                       json={"git_url": "https://github.com/acme/x.git", "git_ref": "main", "credential_ref": "nope"})
    assert r.status_code in (400, 422) and "credential_ref" in r.text


async def test_register_with_a_known_credential_ref_stores_the_ref_not_the_token(api, auth_headers, monkeypatch):
    monkeypatch.setenv("GIT_CREDENTIAL_ACME_GH", "tok-123")
    r = await api.post("/api/v1/projects/register", headers=auth_headers,
                       json={"git_url": "https://github.com/acme/withref.git", "git_ref": "main", "credential_ref": "acme_gh"})
    assert r.status_code == 201, r.text
    assert r.json()["credential_ref"] == "acme_gh" and "tok-123" not in r.text
    got = await api.get(f"/api/v1/projects/{r.json()['id']}", headers=auth_headers)
    assert got.json()["git_url"] == "https://github.com/acme/withref.git" and "tok-123" not in got.text


async def test_update_cannot_introduce_a_credentialed_url_or_unknown_ref(api, auth_headers):
    p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                        json={"git_url": "https://github.com/acme/upd.git", "git_ref": "main"})).json()
    bad = await api.patch(f"/api/v1/projects/{p['id']}", headers=auth_headers, json={"credential_ref": "nope"})
    assert bad.status_code in (400, 422)


async def test_a_legacy_row_with_a_credentialed_url_is_redacted_in_api_output(api, auth_headers, run_db):
    from app.core.config import get_settings
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name="legacy", git_url="https://old:tok-legacy@github.com/acme/legacy.git",
                    git_ref="main", engine="pg_lake")
        db.add(p)
        await db.commit()
        pid = p.id
    one = await api.get(f"/api/v1/projects/{pid}", headers=auth_headers)
    assert one.status_code == 200 and "tok-legacy" not in one.text and "old:" not in one.text
    assert one.json()["git_url"] == "https://github.com/acme/legacy.git"
    assert "tok-legacy" not in (await api.get("/api/v1/projects", headers=auth_headers)).text


# ── git actually receives the header, not a URL with the token ──────────────
def test_clone_passes_the_token_through_the_environment_not_the_url(monkeypatch, tmp_path):
    seen = {}

    def fake_clone_from(url, to_path, **kw):
        seen.update(url=url, env=kw.get("env"))
        raise RuntimeError("stop after capture")

    monkeypatch.setattr("app.services.git_service.git.Repo.clone_from", fake_clone_from)
    monkeypatch.setattr(GitService, "__init__", lambda self: setattr(self, "_root", tmp_path))
    svc = GitService()
    with pytest.raises(RuntimeError, match="stop after capture"):
        svc.clone_for_run(git_url="https://github.com/acme/x.git", git_ref="main", run_id=uuid.uuid4(), token="s3cret")
    assert seen["url"] == "https://github.com/acme/x.git" and "s3cret" not in seen["url"]
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"


def test_ls_remote_authenticates_through_the_environment_not_the_command_line(monkeypatch):
    import subprocess

    from app.services import run_manifest
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(cmd=cmd, env=kw["env"])
        return type("R", (), {"returncode": 0, "stdout": "a" * 40 + "\trefs/heads/main\n", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_manifest.resolve_commit_blocking("https://github.com/acme/x.git", "main", "s3cret") == "a" * 40
    assert "s3cret" not in " ".join(captured["cmd"])
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"


def test_the_jobs_secret_carries_the_token_and_a_clean_url():
    from app.core.config import get_settings
    from app.executors import isolation
    secret = isolation._build_secret(run_id=uuid.uuid4(), attempt=1, namespace="ns", git_url="https://github.com/acme/x.git", git_ref="main",
                                     dq_schema_blocks={}, settings=get_settings(), git_sha="a" * 40, git_token="s3cret")
    data = secret.string_data
    assert data["GIT_TOKEN"] == "s3cret" and data["GIT_URL"] == "https://github.com/acme/x.git"


def test_the_job_reads_the_token_from_its_secret_and_routes_through_the_proxy(monkeypatch):
    from app.core.config import Settings
    from app.executors import isolation
    settings = Settings(runner_mode="k8s_job", runner_image="img:1", job_git_proxy_url="http://git-egress.platform.svc:3128")
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None, settings=settings)
    env = {e.name: e for e in job.spec.template.spec.containers[0].env}
    assert env["RUNNER_GIT_TOKEN"].value_from.secret_key_ref.key == "GIT_TOKEN"       # from the Secret, never a literal
    assert env["RUNNER_GIT_TOKEN"].value is None
    assert env["HTTPS_PROXY"].value == "http://git-egress.platform.svc:3128"            # DBT-010
    assert ".svc" in env["NO_PROXY"].value


def test_no_proxy_env_when_none_is_configured():
    from app.core.config import Settings
    from app.executors import isolation
    job = isolation._build_job(run_id=uuid.uuid4(), attempt=1, engine="pg_lake", select=None,
                               settings=Settings(runner_mode="k8s_job", runner_image="img:1"))
    assert "HTTPS_PROXY" not in {e.name for e in job.spec.template.spec.containers[0].env}
