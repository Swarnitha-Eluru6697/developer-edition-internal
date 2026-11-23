"""The assistant's engine choice (Trino by default; pg_lake and ClickHouse accepted when asked for).

The user picks an engine next to "what to generate"; it travels with each SQL or dbt turn and decides the dialect the model is told to write,
the table-naming rules it gets, and the engine the dbt check validates on. Python and FlinkSQL have one engine each and ignore it.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.codegen_agent import GroundingPack, Harness, HarnessDeps
from app.codegen_agent.contracts import ColumnMeta, TableMeta
from app.schemas.codegen import CodegenPromptRequest
from app.services import codegen as cg


def run(coro):
    return asyncio.run(coro)


# ── which engine a turn runs for ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target,requested,expected", [
    ("sql", "trino", "trino"), ("sql", "pg_lake", "pg_lake"), ("sql", None, "trino"),
    ("dbt", "trino", "trino"), ("dbt", "pg_lake", "pg_lake"), ("dbt", None, "trino"),
    ("python", "trino", "python"), ("flinksql", "trino", "flink"), ("flinksql", "pg_lake", "flink"),   # one engine each: the choice never applies
    ("sql", "clickhouse", "clickhouse"), ("dbt", "clickhouse", "clickhouse"), ("python", "clickhouse", "python"),
    ("flinksql", "clickhouse", "flink"), ("sql", "oracle", "trino"),                                   # never an unsupported engine
])
def test_the_engine_a_turn_runs_for(target, requested, expected):
    assert cg.resolve_turn_engine(target, requested) == expected


def test_the_request_accepts_the_three_engines_and_nothing_else():
    base = {"prompt": "x", "ws_session_id": str(uuid.uuid4())}
    for engine in ("pg_lake", "trino", "clickhouse", None):
        assert CodegenPromptRequest(**base, engine=engine).engine == engine
    assert CodegenPromptRequest(**base).engine is None
    with pytest.raises(ValidationError):
        CodegenPromptRequest(**base, engine="oracle")


# ── what the model is told ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", ["sql", "dbt"])
def test_the_instructions_carry_the_dialect_of_the_chosen_engine(target):
    backend = cg.get_backend(target)
    trino = backend.system_prompt(engine="trino", project_context="CTX")
    pg = backend.system_prompt(engine="pg_lake", project_context="CTX")
    assert "Trino SQL, not PostgreSQL" in trino and "never `::`" in trino and "array_join" in trino and "catalog.schema.table" in trino
    assert "PostgreSQL" in pg and "Trino SQL" not in pg and "never `::`" not in pg
    assert trino != pg and "CTX" in trino and "CTX" in pg                                          # project context still included


def test_an_engine_without_rules_still_gets_a_prompt():
    assert cg.dialect_rules("oracle") == ""


@pytest.mark.parametrize("target", ["sql", "dbt"])
def test_the_clickhouse_instructions_name_its_dialect(target):
    prompt = cg.get_backend(target).system_prompt(engine="clickhouse", project_context="CTX")
    assert "ClickHouse SQL, not PostgreSQL" in prompt and "ENGINE = MergeTree" in prompt and "never `::`" in prompt
    assert "Trino SQL" not in prompt and "CTX" in prompt
    assert "CTX" in cg.get_backend("sql").system_prompt(engine="clickhouse", project_context="CTX")


# ── what the dbt check validates on ──────────────────────────────────────────────────────────────────────────────────────
_REAL_CLIENT = httpx.AsyncClient     # captured once: the patch below replaces the attribute on the shared httpx module


def _validate_with(monkeypatch, **kw):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"valid": True, "errors": []})

    monkeypatch.setattr(cg.httpx, "AsyncClient", lambda **k: _REAL_CLIENT(transport=httpx.MockTransport(handler), **k))
    run(cg.get_backend("dbt").compile_code(code="select 1", path="models/x.sql", companion_code=None, **kw))
    return sent


def test_the_dbt_check_validates_on_the_chosen_engine(monkeypatch):
    assert _validate_with(monkeypatch, engine="trino")["engine"] == "trino"
    assert _validate_with(monkeypatch, engine="pg_lake")["engine"] == "pg_lake"
    assert _validate_with(monkeypatch)["engine"] == "trino"                                     # nothing chosen: the default


def test_the_harness_hands_the_turns_engine_to_the_check():
    seen = []

    class Backend:
        target, default_engine = "dbt", "pg_lake"

        def system_prompt(self, *, engine, project_context):
            return "SYS"

        async def list_sources(self, *, namespace):
            return {"tables": []}

        async def get_table_schema(self, *, table):
            return {"columns": []}

        async def compile_code(self, *, code, path, companion_code, engine=None):
            seen.append(engine)
            return {"ok": True, "errors": []}

    class Model:
        async def complete(self, *, model, system, messages, json_mode=False, **kw):
            from app.codegen_agent.model_client import Completion
            text = '{"intent":"x","tables":[]}' if json_mode else "```sql\nselect 1\n```"
            return Completion(text=text, model=model, input_tokens=1, output_tokens=1)

    async def ground(prompt, target, external):
        return GroundingPack(target=target, tables=[TableMeta(fqn="a.b", columns=[ColumnMeta(name="id", type="bigint")])], external_systems=external)

    async def publish(msg):
        pass

    for engine in ("trino", "pg_lake"):
        deps = HarnessDeps(model=Model(), backend=Backend(), publish=publish, engine=engine, project_context="", ground=ground)
        run(Harness(deps).run(target="dbt", prompt="p", current_file_content=None, model_name="codegen-generation"))
    assert seen and set(seen) == {"trino", "pg_lake"}


# ── the whole turn: what submit_turn hands to the run ────────────────────────────────────────────────────────────────────
class _Db:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        for r in self.rows:
            r.id = r.id or uuid.uuid4()


def _submit(monkeypatch, target, engine):
    got = {}

    async def fake_run_turn(self, **kw):
        got.update(kw)

    monkeypatch.setattr(cg.CodegenService, "_run_turn", fake_run_turn)

    async def go():
        svc = cg.CodegenService()
        session = SimpleNamespace(id=uuid.uuid4(), target="sql", workspace_id=uuid.uuid4())
        await svc.submit_turn(_Db(), session=session, ws_session_id=uuid.uuid4(), prompt="p", current_file_content=None,
                              project_context="", target=target, engine=engine)
        await asyncio.gather(*svc._tasks)

    run(go())
    return got


@pytest.mark.parametrize("target,engine,expected", [
    ("sql", "trino", "trino"), ("dbt", "trino", "trino"), ("sql", None, "trino"), ("sql", "pg_lake", "pg_lake"),
    ("python", "trino", "python"), ("flinksql", "trino", "flink"),
])
def test_a_turn_runs_for_the_chosen_engine(monkeypatch, target, engine, expected):
    assert _submit(monkeypatch, target, engine)["engine"] == expected


def test_the_api_no_longer_refuses_clickhouse_and_records_the_engine_used():
    import inspect

    from app.api import codegen as api
    src = inspect.getsource(api.submit_codegen_prompt)
    assert "ensure_engine_supported" not in src and "engine=payload.engine" in src and '"engine": turn_engine' in src
