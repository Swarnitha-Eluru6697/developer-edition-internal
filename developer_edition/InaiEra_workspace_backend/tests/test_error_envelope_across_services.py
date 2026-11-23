"""X-008, across repositories: Workspace, the dbt Runner and the Flink Runner produce the SAME 409 envelope.

Each service's own tests (tests/test_error_contract.py) pin its constraint map and call sites; this one puts the three side by side by loading
their `app/core/errors.py` (which depend on FastAPI only) under separate module names. Skipped for a repo that is not checked out next to this one.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = Path(os.environ.get("RUNNERS_REPO", ROOT / "InaiEra_workspace_runners"))
FILES = {
    "workspace": Path(__file__).resolve().parents[1] / "app/core/errors.py",
    "dbt": RUNNERS / "inaiera-dbt-runner/app/core/errors.py",
    "flink": RUNNERS / "inaiera-flinksql-runner/app/core/errors.py",
}


def _load(name: str):
    path = FILES[name]
    if not path.exists():
        pytest.skip(f"{name} repo is not checked out next to this one")
    spec = importlib.util.spec_from_file_location(f"x008_{name}_errors", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def modules():
    return {n: _load(n) for n in FILES}


def test_the_same_call_gives_the_same_409_everywhere(modules):
    bodies = {n: m.conflict("taken", code="THING_TAKEN", resource="thing", field="name", details={"name": "x"}) for n, m in modules.items()}
    assert {b.status_code for b in bodies.values()} == {409}
    details = [b.detail for b in bodies.values()]
    assert details[0] == details[1] == details[2] == {
        "code": "THING_TAKEN", "message": "taken", "details": {"name": "x", "resource": "thing", "field": "name"}}


def test_the_bare_conflict_is_the_same_everywhere(modules):
    assert {str(m.conflict("x").detail) for m in modules.values()} == {str({"code": "CONFLICT", "message": "x", "details": {}})}


def test_the_conflict_signature_is_the_same_everywhere(modules):
    import inspect
    sigs = {n: [(p.name, p.kind.name, p.default) for p in inspect.signature(m.conflict).parameters.values()] for n, m in modules.items()}
    assert sigs["workspace"] == sigs["dbt"] == sigs["flink"], sigs


def test_every_service_translates_a_unique_violation_the_same_way(modules):
    from sqlalchemy.exc import IntegrityError

    class _Pg(Exception):
        sqlstate = "23505"
        constraint_name = "no_such_constraint"
    out = {n: m.conflict_for_unique_violation(IntegrityError("INSERT", {}, _Pg())).detail for n, m in modules.items()}
    assert out["workspace"] == out["dbt"] == out["flink"]
    assert out["workspace"]["code"] == "CONFLICT"


def test_no_stable_code_means_two_different_things_in_two_services(modules):
    """The same code with a different resource in another service would make a client's `if code == ...` ambiguous."""
    seen: dict[str, tuple] = {}
    for n, m in modules.items():
        for _constraint, (code, resource, field) in m.CONSTRAINT_CONFLICTS.items():
            if code in seen:
                assert seen[code][1:] == (resource, field) or code == "GIT_CREDENTIAL_EXISTS", (code, n, seen[code])
            seen[code] = (n, resource, field)
