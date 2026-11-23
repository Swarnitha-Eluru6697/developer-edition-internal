"""Several dbt projects (folders) in one Git repo: path parsing, folder
resolution and per-folder registration."""
from __future__ import annotations

import pytest

from app.core.project_path import normalize_project_path, split_git_url
from app.schemas.project import ProjectRegister
from app.services.project_service import _default_name
from app.services.run_service import _resolve_project_dir


# ---- path / URL parsing ------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    (None, ""), ("", ""), ("  ", ""), (".", ""), ("sales", "sales"),
    ("/".join(["a", "b"]), "a/b"), ("a//b/", "a/b"), ("./a\\b", "a/b"),
])
def test_normalize_project_path(raw, expected):
    assert normalize_project_path(raw) == expected


@pytest.mark.parametrize("raw", ["../x", "a/../../x", "/etc", "C:/x", ".git/hooks"])
def test_normalize_project_path_rejects_escapes(raw):
    with pytest.raises(ValueError):
        normalize_project_path(raw)


def test_split_git_url():
    assert split_git_url("https://h/o/r.git") == ("https://h/o/r.git", "")
    assert split_git_url("https://h/o/r.git#subdirectory=dbt/sales") == ("https://h/o/r.git", "dbt/sales")
    assert split_git_url("https://h/o/r.git#path=marketing") == ("https://h/o/r.git", "marketing")
    assert split_git_url("https://h/o/r.git#something=else") == ("https://h/o/r.git", "")


def test_register_schema_takes_path_from_field_or_url():
    a = ProjectRegister(git_url="https://h/o/r.git", project_path="sales")
    b = ProjectRegister(git_url="https://h/o/r.git#subdirectory=sales")
    assert (a.git_url, a.project_path) == (b.git_url, b.project_path) == ("https://h/o/r.git", "sales")
    assert ProjectRegister(git_url="https://h/o/r.git").project_path == ""


def test_register_schema_rejects_conflicting_or_unsafe_path():
    with pytest.raises(ValueError):
        ProjectRegister(git_url="https://h/o/r.git#subdirectory=a", project_path="b")
    with pytest.raises(ValueError):
        ProjectRegister(git_url="https://h/o/r.git", project_path="../x")


def test_default_name_includes_folder():
    assert _default_name("https://h/o/analytics.git") == "analytics"
    assert _default_name("https://h/o/analytics.git", "sales") == "analytics/sales"


# ---- folder resolution -------------------------------------------------
def _project(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "dbt_project.yml").write_text("name: x\n")


def test_resolve_picks_the_requested_folder_among_several(tmp_path):
    _project(tmp_path / "sales")
    _project(tmp_path / "dbt" / "marketing")
    assert _resolve_project_dir(tmp_path, "sales") == (tmp_path / "sales").resolve()
    assert _resolve_project_dir(tmp_path, "dbt/marketing") == (tmp_path / "dbt" / "marketing").resolve()


def test_resolve_without_path_keeps_legacy_behaviour(tmp_path):
    _project(tmp_path / "only")
    assert _resolve_project_dir(tmp_path) == tmp_path / "only"
    _project(tmp_path / "other")
    with pytest.raises(RuntimeError, match="Multiple dbt projects.*project path"):
        _resolve_project_dir(tmp_path)


def test_resolve_errors_are_clear(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(RuntimeError, match="does not exist"):
        _resolve_project_dir(tmp_path, "missing")
    with pytest.raises(RuntimeError, match="No dbt_project.yml in folder 'empty'"):
        _resolve_project_dir(tmp_path, "empty")


def test_resolve_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    _project(outside)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "link").symlink_to(outside)
    with pytest.raises(RuntimeError, match="outside the repository"):
        _resolve_project_dir(clone, "link")


# ---- registration (DB-backed; skipped without TEST_DATABASE_URL) -------
async def test_same_repo_registers_once_per_folder(run_db):
    from app.core.errors import DbtRunnerError
    from app.services.project_service import ProjectService

    async def reg(**kw):
        async with run_db() as db:
            p = await ProjectService(db).register(
                org_id="org1", user_id="u", request=ProjectRegister(git_url="https://h/o/mono.git", **kw))
            await db.commit()
            return p

    sales = await reg(project_path="sales")
    mkt = await reg(project_path="marketing")
    assert sales.id != mkt.id and sales.name == "mono/sales"
    with pytest.raises(DbtRunnerError):
        await reg(project_path="sales")
    assert (await reg()).project_path == ""  # repo-root registration is separate


def test_unsafe_project_path_is_a_422_not_a_500(client, auth_headers):
    resp = client.post("/api/v1/projects/register", headers=auth_headers, json={
        "git_url": "https://h/o/r.git", "project_path": "../x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---- registration rejects a wrong project name (real local git repos) ----
import subprocess

import httpx

from app.core.config import Settings
from app.main import app


def _git_repo(path, project_dirs):
    for d in project_dirs:
        folder = path / d if d else path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "dbt_project.yml").write_text("name: x\n")
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("add", ".")
    run("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init")
    return f"file://{path}"


@pytest.fixture
async def checked_api(run_db, monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.run_service.dispatch_run", _noop)
    monkeypatch.setattr("app.api.projects.get_settings", lambda: Settings(validate_project_on_register=True))
    monkeypatch.setattr("app.services.git_service.get_settings", lambda: Settings(git_workdir=str(tmp_path / "w")))
    monkeypatch.setattr("app.services.git_service._service", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _noop(run_id):
    return None


async def _register(api, headers, **body):
    return await api.post("/api/v1/projects/register", headers=headers,
                          json={"engine": "pg_lake", **body})


async def test_register_rejects_a_wrong_project_name(checked_api, auth_headers, tmp_path):
    url = _git_repo(tmp_path / "mono", ["sales", "marketing"])
    resp = await _register(checked_api, auth_headers, git_url=url, project_path="salez")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["details"]["field"] == "project_path"
    assert "'salez' was not found" in detail["message"] and "marketing, sales" in detail["message"]


async def test_register_accepts_a_real_project_name(checked_api, auth_headers, tmp_path):
    url = _git_repo(tmp_path / "mono", ["sales", "marketing"])
    resp = await _register(checked_api, auth_headers, git_url=url, project_path="sales")
    assert resp.status_code == 201, resp.text


async def test_register_requires_a_name_when_the_repo_has_several_projects(checked_api, auth_headers, tmp_path):
    url = _git_repo(tmp_path / "mono", ["sales", "marketing"])
    resp = await _register(checked_api, auth_headers, git_url=url)
    assert resp.status_code == 400
    assert "several dbt projects" in resp.json()["detail"]["message"]


async def test_register_root_project_and_single_folder_project(checked_api, auth_headers, tmp_path):
    root = _git_repo(tmp_path / "rootproj", [""])
    one = _git_repo(tmp_path / "oneproj", ["dbt"])
    assert (await _register(checked_api, auth_headers, git_url=root, project_path=".")).status_code == 201
    assert (await _register(checked_api, auth_headers, git_url=one)).status_code == 201


async def test_register_rejects_an_unreadable_repo_without_leaking_the_error(checked_api, auth_headers, tmp_path):
    resp = await _register(checked_api, auth_headers, git_url=f"file://{tmp_path}/nope", project_path="x")
    assert resp.status_code == 400
    assert resp.json()["detail"]["details"]["field"] == "git_url"
    assert "nope" not in resp.json()["detail"]["message"]
