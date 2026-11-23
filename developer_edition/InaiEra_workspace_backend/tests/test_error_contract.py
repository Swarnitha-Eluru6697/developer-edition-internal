"""X-008: one conflict/error semantics across Workspace, the dbt Runner and the Flink Runner.

The 409 is always `{"code": <STABLE>, "message": ..., "details": {"resource": <kind>, "field": <name>, ...}}` (under the framework's `detail`).
Every deliberate conflict names its reason; every UNIQUE rule of the models maps to a code, so a race on it is the same 409 as the friendly
pre-check; and a unique violation nobody translated is still a 409 through the app-wide handler, never a raw database error.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app.core import errors
from app.db.base import Base
import app.models  # noqa: F401  (registers the tables)
from app.main import app

APP = Path(__file__).resolve().parent.parent / "app"
UPPER_SNAKE = __import__("re").compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


class _PgUnique(Exception):
    sqlstate = "23505"

    def __init__(self, name):
        super().__init__(name)
        self.constraint_name = name


def _unique_names() -> set[str]:
    names = set()
    for t in Base.metadata.tables.values():
        for c in t.constraints:
            if isinstance(c, UniqueConstraint):
                names.add(c.name or f"{t.name}_{'_'.join(col.name for col in c.columns)}_key")
        names |= {i.name for i in t.indexes if i.unique}
    return names


def test_every_unique_rule_of_the_models_has_a_stable_code_resource_and_field():
    missing = _unique_names() - set(errors.CONSTRAINT_CONFLICTS)
    assert not missing, f"unique rules with no conflict mapping: {sorted(missing)}"
    stale = set(errors.CONSTRAINT_CONFLICTS) - _unique_names()
    assert not stale, f"mappings for constraints that no longer exist: {sorted(stale)}"
    for name, (code, resource, field) in errors.CONSTRAINT_CONFLICTS.items():
        assert UPPER_SNAKE.match(code) and code != "CONFLICT", name
        assert resource and field, name


def test_the_conflict_envelope_has_code_message_details_resource_and_field():
    e = errors.conflict("taken", code="THING_TAKEN", resource="thing", field="name", details={"name": "x"})
    assert e.status_code == 409
    assert e.detail == {"code": "THING_TAKEN", "message": "taken", "details": {"name": "x", "resource": "thing", "field": "name"}}


def test_details_is_always_present_even_without_resource_or_field():
    assert errors.conflict("x").detail == {"code": "CONFLICT", "message": "x", "details": {}}


def _conflict_calls():
    for path in APP.rglob("*.py"):
        if path.name == "errors.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "conflict":
                yield path.name, node


def test_every_deliberate_conflict_names_its_code_and_resource():
    bare = []
    for name, call in _conflict_calls():
        kw = {k.arg for k in call.keywords}
        if "code" not in kw or "resource" not in kw:
            bare.append(f"{name}:{call.lineno}")
    assert not bare, f"conflict() without code= and resource=: {bare}"


def test_the_codes_used_are_upper_snake_and_not_the_generic_one():
    for name, call in _conflict_calls():
        for k in call.keywords:
            if k.arg == "code" and isinstance(k.value, ast.Constant):
                assert UPPER_SNAKE.match(k.value.value) and k.value.value != "CONFLICT", f"{name}:{call.lineno}"


@pytest.mark.parametrize("name", sorted(errors.CONSTRAINT_CONFLICTS))
def test_a_unique_violation_translates_to_the_mapped_409(name):
    code, resource, field = errors.CONSTRAINT_CONFLICTS[name]
    c = errors.conflict_for_unique_violation(IntegrityError("INSERT", {}, _PgUnique(name)))
    assert c.status_code == 409 and c.detail["code"] == code
    assert c.detail["details"]["resource"] == resource and c.detail["details"]["field"] == field


def test_other_integrity_errors_are_not_conflicts():
    class _Fk(Exception):
        sqlstate = "23503"
    assert errors.conflict_for_unique_violation(IntegrityError("INSERT", {}, _Fk())) is None
    assert errors.unique_violation(ValueError("x")) is None


def test_an_unmapped_unique_violation_is_still_a_409_with_the_generic_code():
    c = errors.conflict_for_unique_violation(IntegrityError("INSERT", {}, _PgUnique("some_new_constraint")))
    assert c.status_code == 409 and c.detail["code"] == "CONFLICT"


def test_the_app_wide_handler_turns_an_untranslated_unique_violation_into_the_standard_409():
    async def _boom():
        raise IntegrityError("INSERT", {}, _PgUnique(next(iter(errors.CONSTRAINT_CONFLICTS))))
    app.add_api_route("/__unique", _boom, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/__unique")
    finally:
        app.router.routes[:] = [x for x in app.router.routes if getattr(x, "path", "") != "/__unique"]
    code, resource, field = errors.CONSTRAINT_CONFLICTS[next(iter(errors.CONSTRAINT_CONFLICTS))]
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": code, "message": r.json()["detail"]["message"], "details": {"resource": resource, "field": field}}


def test_a_non_unique_integrity_error_is_not_dressed_up_as_a_conflict():
    class _Fk(Exception):
        sqlstate = "23503"

    async def _boom():
        raise IntegrityError("INSERT", {}, _Fk())
    app.add_api_route("/__fk", _boom, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/__fk")
    finally:
        app.router.routes[:] = [x for x in app.router.routes if getattr(x, "path", "") != "/__fk"]
    assert r.status_code == 500
