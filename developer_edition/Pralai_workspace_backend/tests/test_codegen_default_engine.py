"""The assistant writes (and checks) SQL and dbt models for Trino by default: Trino is the engine offered everywhere now.

It used to default to Trino, so the dialect it wrote and the engine its dbt check ran on were not the ones the user then ran the code on.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.schemas.codegen import Engine
from app.services import codegen as cg


def run(coro):
    return asyncio.run(coro)


def test_sql_and_dbt_default_to_trino_and_the_others_are_unchanged():
    assert cg.get_backend("sql").default_engine == "trino"
    assert cg.get_backend("dbt").default_engine == "trino"
    assert cg.get_backend("python").default_engine == "python"
    assert cg.get_backend("flinksql").default_engine == "flink"


def test_every_default_is_an_engine_the_api_accepts():
    for target in ("sql", "dbt", "python", "flinksql"):
        assert cg.get_backend(target).default_engine in Engine.__args__


def test_the_prompt_names_the_engine_it_is_given():
    sql = cg.get_backend("sql")
    assert "trino engine" in sql.system_prompt(engine=sql.default_engine, project_context="")
    assert "pg_lake engine" in sql.system_prompt(engine="pg_lake", project_context="")      # an explicit choice is still honoured
    dbt = cg.get_backend("dbt")
    assert "targeting trino" in dbt.system_prompt(engine=dbt.default_engine, project_context="")


def test_the_dbt_check_asks_the_runner_to_validate_on_trino(monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"valid": True, "errors": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(cg.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    result = run(cg.get_backend("dbt").compile_code(code="select 1", path="models/x.sql", companion_code=None))
    assert result == {"ok": True, "errors": []}
    assert sent["engine"] == "trino"


@pytest.mark.parametrize("target", ["sql", "dbt"])
def test_a_turn_with_no_engine_chosen_runs_on_the_targets_default(target):
    """No engine in the request -> the backend default, which is trino for sql and dbt (the choice itself is tested in test_codegen_engine_choice.py)."""
    assert cg.resolve_turn_engine(target, None) == cg.get_backend(target).default_engine == "trino"
