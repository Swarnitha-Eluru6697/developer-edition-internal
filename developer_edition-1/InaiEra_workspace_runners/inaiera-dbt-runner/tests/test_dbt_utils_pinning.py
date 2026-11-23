"""DBT-008: the runner never edits a customer's packages.yml.

It used to add `dbt-labs/dbt_utils` with a broad version range whenever DQ-generated tests needed it: it changed the project's dependency
model silently and let two runs of the same commit resolve different package content. Now a project whose schema files use dbt_utils must
declare it EXACTLY pinned; otherwise the run fails before dbt starts with a message saying what to add.
"""
from __future__ import annotations

import pytest

from app.executors import dbt_executor
from app.executors.dbt_executor import DependencyError, _require_pinned_dbt_utils

SHA = "0123456789abcdef0123456789abcdef01234567"
USES = "models:\n  - name: m\n    columns:\n      - name: id\n        tests:\n          - dbt_utils.accepted_range:\n              min_value: 0\n"


def _project(tmp_path, *, schema=USES, packages: str | None = None):
    (tmp_path / "dbt_project.yml").write_text("name: p\n")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "schema.yml").write_text(schema)
    if packages is not None:
        (tmp_path / "packages.yml").write_text(packages)
    return tmp_path


def test_a_project_that_does_not_use_dbt_utils_is_untouched(tmp_path):
    p = _project(tmp_path, schema="models:\n  - name: m\n")
    assert _require_pinned_dbt_utils(p) is False
    assert not (p / "packages.yml").exists()


def test_using_dbt_utils_without_declaring_it_fails_and_writes_nothing(tmp_path):
    p = _project(tmp_path)
    with pytest.raises(DependencyError, match="does not declare it") as e:
        _require_pinned_dbt_utils(p)
    assert "dbt-labs/dbt_utils" in str(e.value) and "version:" in str(e.value)
    assert not (p / "packages.yml").exists()                          # the customer's files are never created or edited


@pytest.mark.parametrize("packages", [
    "packages:\n  - package: dbt-labs/dbt_utils\n    version: [\">=1.0.0\", \"<2.0.0\"]\n",       # a range
    "packages:\n  - package: dbt-labs/dbt_utils\n    version: \">=1.0.0\"\n",
    "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.x\n",
    "packages:\n  - package: dbt-labs/dbt_utils\n",                                                # no version at all
    "packages:\n  - git: https://github.com/dbt-labs/dbt-utils.git\n    revision: main\n",        # a branch
    "packages:\n  - git: https://github.com/dbt-labs/dbt-utils.git\n    revision: 1.3.0\n",       # a tag can move
    "packages:\n  - git: https://github.com/dbt-labs/dbt-utils.git\n",
])
def test_an_unpinned_dbt_utils_fails_the_run_before_dbt_starts(tmp_path, packages):
    p = _project(tmp_path, packages=packages)
    before = (p / "packages.yml").read_text()
    with pytest.raises(DependencyError, match="Pin it exactly"):
        _require_pinned_dbt_utils(p)
    assert (p / "packages.yml").read_text() == before                  # byte-identical


@pytest.mark.parametrize("packages", [
    "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
    "packages:\n  - package: dbt-labs/dbt_utils\n    version: [\"1.3.0\"]\n",
    f"packages:\n  - git: https://github.com/dbt-labs/dbt-utils.git\n    revision: {SHA}\n",
    "packages:\n  - local: ../vendored/dbt_utils\n",
])
def test_an_exactly_pinned_dbt_utils_is_accepted(tmp_path, packages):
    p = _project(tmp_path, packages=packages)
    before = (p / "packages.yml").read_text()
    assert _require_pinned_dbt_utils(p) is True
    assert (p / "packages.yml").read_text() == before


def test_other_packages_do_not_satisfy_the_requirement(tmp_path):
    p = _project(tmp_path, packages="packages:\n  - package: calogica/dbt_expectations\n    version: 0.10.1\n")
    with pytest.raises(DependencyError):
        _require_pinned_dbt_utils(p)


def _executor(tmp_path, monkeypatch, packages, deps_ok=True):
    from types import SimpleNamespace
    p = _project(tmp_path, packages=packages)
    calls = []

    def fake_run(args, project_dir, profiles_dir):
        calls.append(args)
        ok = deps_ok if args[0] == "deps" else True
        return SimpleNamespace(success=ok, result=SimpleNamespace(results=[]), exception="boom" if not ok else None)

    monkeypatch.setattr(dbt_executor, "_run_dbt", fake_run)
    monkeypatch.setattr(dbt_executor, "_force_profile", lambda d, n: None)
    return p, calls


def test_run_project_fails_before_dbt_when_the_pin_is_missing(tmp_path, monkeypatch):
    p, calls = _executor(tmp_path, monkeypatch, "packages:\n  - package: dbt-labs/dbt_utils\n    version: \">=1.0.0\"\n")
    out = dbt_executor.DbtExecutor().run_project(project_dir=p, engine="pg_lake", select=None)
    assert out["success"] is False and "Pin it exactly" in out["errors"][0]
    assert calls == []                                                 # neither `deps` nor `build` ran


def test_run_project_runs_deps_then_build_for_a_pinned_project(tmp_path, monkeypatch):
    p, calls = _executor(tmp_path, monkeypatch, "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n")
    out = dbt_executor.DbtExecutor().run_project(project_dir=p, engine="pg_lake", select=None)
    assert out["success"] is True and [c[0] for c in calls] == ["deps", "build"]


def test_a_failed_deps_fails_the_run_instead_of_building_without_the_package(tmp_path, monkeypatch):
    p, calls = _executor(tmp_path, monkeypatch, "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n", deps_ok=False)
    out = dbt_executor.DbtExecutor().run_project(project_dir=p, engine="pg_lake", select=None)
    assert out["success"] is False and "dbt deps failed" in out["errors"][0]
    assert [c[0] for c in calls] == ["deps"]
