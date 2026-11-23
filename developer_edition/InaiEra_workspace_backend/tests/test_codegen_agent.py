"""Unit tests for the self-hosted codegen agent.

These exercise the harness state machine and the Python static-validation
ladder with in-process fakes — no model gateway, no catalog, no database — so
they run in the same "unit tests only" lane as the rest of the suite.

What they pin:
  * a happy path delivers validated code;
  * a schema error routes back to GROUND, re-grounds, and recovers;
  * an unfixable error exhausts the budget and returns a *diagnosis* with the
    last candidate marked failed — never silently-broken code presented as ok;
  * the Python ladder blocks prohibited imports / process escapes / secrets and
    binds Kafka topics against the grounding pack, all without executing code.
"""
from __future__ import annotations

import asyncio

from app.codegen_agent.contracts import ColumnMeta, GroundingPack, KafkaTopicMeta, TableMeta
from app.codegen_agent.harness import Harness, HarnessDeps
from app.codegen_agent.model_client import Completion
from app.codegen_agent.python_checks import validate_python_notebook


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _FakeModel:
    """Serves scripted plan (json_mode) and generate replies independently, so
    re-grounding — which regenerates but does not re-plan — stays in sync."""

    def __init__(self, plan_outs: list[str], gen_outs: list[str]) -> None:
        self._plan = list(plan_outs)
        self._gen = list(gen_outs)

    async def complete(self, *, model, system, messages, temperature=0.6,
                       json_mode=False, max_tokens=None) -> Completion:
        if json_mode:
            text = self._plan.pop(0) if self._plan else '{"intent":"x"}'
        else:
            text = self._gen.pop(0) if self._gen else "```sql\nSELECT 1\n```"
        return Completion(text=text, model=model, input_tokens=5, output_tokens=10)


class _FakeBackend:
    target = "sql"
    default_engine = "trino"

    def __init__(self, compile_results: list[dict]) -> None:
        self._results = list(compile_results)

    def system_prompt(self, *, engine, project_context):
        return "SYS"

    async def list_sources(self, *, namespace):
        return {"tables": ["hive.sales.orders"]}

    async def get_table_schema(self, *, table):
        return {"columns": [{"name": "id", "type": "bigint"},
                            {"name": "amount", "type": "decimal"}]}

    async def compile_code(self, *, code, path, companion_code, engine=None):
        return self._results.pop(0) if self._results else {"ok": True, "errors": []}


async def _grounder(prompt, target, external):
    return GroundingPack(
        target=target,
        tables=[TableMeta(
            fqn="hive.sales.orders",
            columns=[ColumnMeta(name="id", type="bigint"),
                     ColumnMeta(name="amount", type="decimal")],
        )],
        external_systems=external,
    )


def _deps(model, backend, events):
    async def publish(msg):
        events.append(msg.get("stage") or msg["type"])
    return HarnessDeps(model=model, backend=backend, publish=publish,
                       engine="trino", project_context="", ground=_grounder)


# --------------------------------------------------------------------------
# Harness tests
# --------------------------------------------------------------------------
def test_happy_path_delivers_validated_code():
    async def run():
        events: list[str] = []
        model = _FakeModel(
            plan_outs=['{"intent":"sum amount","tables":["hive.sales.orders"]}'],
            gen_outs=["```sql\nSELECT sum(amount) FROM hive.sales.orders\n```"],
        )
        backend = _FakeBackend([{"ok": True, "errors": []}])
        out = await Harness(_deps(model, backend, events)).run(
            target="sql", prompt="total amount from orders",
            current_file_content=None, model_name="codegen-generation",
        )
        assert out.validation_status == "passed"
        assert "SELECT sum(amount)" in out.artifact_code
    asyncio.run(run())


def test_schema_error_routes_to_ground_then_recovers():
    async def run():
        events: list[str] = []
        model = _FakeModel(
            plan_outs=['{"intent":"get id","tables":["hive.sales.orders"]}'],
            gen_outs=[
                "```sql\nSELECT region_code FROM hive.sales.orders\n```",  # bad column
                "```sql\nSELECT id FROM hive.sales.orders\n```",           # fixed
            ],
        )
        backend = _FakeBackend([
            {"ok": False, "errors": ["Column 'region_code' cannot be resolved"]},
            {"ok": True, "errors": []},
        ])
        out = await Harness(_deps(model, backend, events)).run(
            target="sql", prompt="get id from orders",
            current_file_content=None, model_name="codegen-generation",
        )
        assert out.validation_status == "passed"
        assert "SELECT id" in out.artifact_code
        assert "repair" in events  # the failure was routed, not just retried blindly
    asyncio.run(run())


def test_budget_exhaustion_returns_diagnosis_not_broken_code():
    async def run():
        events: list[str] = []
        model = _FakeModel(
            plan_outs=['{"intent":"x"}'],
            gen_outs=["```sql\nSELECT bad FROM hive.sales.orders\n```"] * 10,
        )
        backend = _FakeBackend(
            [{"ok": False, "errors": ["Column 'bad' cannot be resolved"]}] * 10
        )
        out = await Harness(_deps(model, backend, events)).run(
            target="sql", prompt="x", current_file_content=None,
            model_name="codegen-generation",
        )
        assert out.validation_status == "failed"
        # the reply is a diagnosis, and any code is explicitly marked unvalidated
        assert "validation" in out.reply_text.lower() or "couldn't" in out.reply_text.lower()
    asyncio.run(run())


# --------------------------------------------------------------------------
# Python static ladder tests (never executes code)
# --------------------------------------------------------------------------
def _gp(**kw):
    return GroundingPack(target="python", **kw)


def test_python_clean_file_processing_passes():
    r = validate_python_notebook(
        code="import pandas as pd\ndf = pd.read_csv('x.csv')\ndf.to_parquet('o.parquet')\n",
        path="cell.py", grounding=_gp(),
    )
    assert r.passed


def test_python_blocks_prohibited_import():
    r = validate_python_notebook(
        code="import subprocess\nsubprocess.run(['ls'])\n", path="cell.py", grounding=_gp(),
    )
    assert not r.passed
    assert r.first_failure.error_class.value == "PY_PROHIBITED_IMPORT"
    assert r.first_failure.route_to.value == "GENERATE"


def test_python_blocks_process_escape():
    r = validate_python_notebook(
        code="import os\nos.system('rm -rf /')\n", path="cell.py", grounding=_gp(),
    )
    assert not r.passed
    assert r.first_failure.error_class.value == "PY_SECURITY_FINDING"


def test_python_blocks_hardcoded_secret():
    r = validate_python_notebook(
        code="import pandas\napi_key = 'sk-supersecretvalue123'\n", path="cell.py", grounding=_gp(),
    )
    assert not r.passed
    assert r.first_failure.error_class.value == "PY_SECURITY_FINDING"


def test_python_unknown_kafka_topic_routes_to_ground():
    g = _gp(external_systems=["kafka"],
            kafka_topics=[KafkaTopicMeta(topic="orders", schema_type="avro")])
    r = validate_python_notebook(
        code="from confluent_kafka import Producer\np=Producer({})\np.produce('nope', b'x')\n",
        path="cell.py", grounding=g,
    )
    assert not r.passed
    assert r.first_failure.error_class.value == "PY_KAFKA_TOPIC_UNKNOWN"
    assert r.first_failure.route_to.value == "GROUND"


def test_python_known_kafka_topic_passes():
    g = _gp(external_systems=["kafka"],
            kafka_topics=[KafkaTopicMeta(topic="orders", schema_type="avro")])
    r = validate_python_notebook(
        code="from confluent_kafka import Producer\np=Producer({})\np.produce('orders', b'x')\n",
        path="cell.py", grounding=g,
    )
    assert r.passed


def test_python_syntax_error_fails_at_l0():
    r = validate_python_notebook(code="def f(:\n  pass\n", path="cell.py", grounding=_gp())
    assert not r.passed
    assert r.first_failure.rung == "L0"
