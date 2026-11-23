"""DQ-FLINK-008 — validate_job_sql must expose the structured sources/sinks
contract (app.services.sql_plan_service), not just detected_sources/sinks
name lists.

The Flink Gateway session calls (create_session/execute_statements/
explain_sql/close_session) are mocked — this test isn't re-verifying
Gateway EXPLAIN behavior (untouched by this fix), only that the new
structured fields are computed and threaded through correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.validate_service import validate_job_sql

_SQL = """
CREATE TABLE orders (
  id STRING,
  amount DECIMAL(10,2)
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders-topic',
  'format' = 'json'
);
INSERT INTO quality_sink SELECT id FROM orders WHERE amount < 0;
"""


class _FakeFlinkClient:
    def __init__(self):
        self.create_session = AsyncMock(return_value="sess-1")
        self.execute_statements = AsyncMock(return_value=[])
        self.explain_sql = AsyncMock(return_value={"valid": True, "errors": []})
        self.close_session = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_validate_job_sql_returns_structured_sources_and_sinks():
    with patch("app.services.validate_service.get_flink_client", return_value=_FakeFlinkClient()):
        result = await validate_job_sql(_SQL)

    assert result["valid"] is True
    # Legacy name-only fields are untouched.
    assert set(result["detected_sources"]) == {"orders"}
    assert set(result["detected_sinks"]) == {"quality_sink"}

    # New structured contract (DQ-FLINK-008).
    assert [t["name"] for t in result["sources"]] == ["orders"]
    source = result["sources"][0]
    assert {"name": "id", "type": "STRING"} in source["columns"]
    assert {"name": "amount", "type": "DECIMAL(10,2)"} in source["columns"]
    assert source["options"] == {
        "connector": "kafka", "topic": "orders-topic", "format": "json",
    }
    # quality_sink is referenced by INSERT INTO but never CREATEd in this
    # script — a catalog-defined/pre-existing table, correctly unresolved
    # rather than silently absent.
    assert result["sinks"] == []
    assert result["unresolved_tables"] == ["quality_sink"]


@pytest.mark.asyncio
async def test_validate_job_sql_exception_path_still_returns_empty_structured_fields():
    failing_client = _FakeFlinkClient()
    failing_client.create_session = AsyncMock(side_effect=RuntimeError("gateway unreachable"))
    with patch("app.services.validate_service.get_flink_client", return_value=failing_client):
        result = await validate_job_sql(_SQL)

    assert result["valid"] is False
    # The structured plan is pure parsing: still returned when the Gateway is down.
    assert [t["name"] for t in result["sources"]] == ["orders"]
    assert result["plan"]["sources"] == result["sources"]


@pytest.mark.asyncio
async def test_validate_returns_the_structured_job_plan_contract():
    from app.core import quality_contract as qc
    from app.core.config import get_settings
    from app.schemas import ValidateResponse
    with patch("app.services.validate_service.get_flink_client", return_value=_FakeFlinkClient()):
        result = await validate_job_sql(_SQL)
    plan = ValidateResponse(**result).plan
    assert plan.plan_version == "1" and plan.engine == "flink"
    assert plan.engine_version == get_settings().flink_version
    assert len(plan.normalized_sql_hash) == 64
    assert plan.quality_contract.version == qc.CONTRACT_VERSION
    assert plan.quality_contract.topics["results"] == qc.RESULTS_TOPIC
    assert [t.name for t in plan.sources] == ["orders"]


def test_normalized_sql_hash_ignores_comments_and_whitespace_only():
    from app.services.sql_plan_service import normalized_sql_hash
    a = normalized_sql_hash("CREATE TABLE t (a INT);  -- c\nINSERT INTO s SELECT a FROM t;")
    b = normalized_sql_hash("CREATE   TABLE t (a INT);\n/* x */ INSERT INTO s SELECT a FROM t;")
    c = normalized_sql_hash("CREATE TABLE t (a BIGINT); INSERT INTO s SELECT a FROM t;")
    assert a == b != c


# ---------------------------------------------------------------------
# RUN-FLINK-012 — the validate path uses the parser-safe splitter
# ---------------------------------------------------------------------
_TRICKY_SQL = """
CREATE TABLE src (v STRING) WITH ('connector' = 'kafka', 'topic' = 'a;b', 'format' = 'json');
-- a comment with a ; in it
CREATE TABLE snk (v STRING) WITH ('connector' = 'blackhole');
INSERT INTO snk SELECT CONCAT(v, ';', "x;y") FROM src /* ; */;
EXECUTE STATEMENT SET BEGIN
  INSERT INTO snk SELECT 'it''s; fine' FROM src;
  INSERT INTO snk SELECT v FROM src; -- trailing ; comment
END;
"""


@pytest.mark.asyncio
async def test_validate_splits_like_deploy_and_never_executes_inserts():
    client = _FakeFlinkClient()
    with patch("app.services.validate_service.get_flink_client", return_value=client):
        result = await validate_job_sql(_TRICKY_SQL)

    assert result["valid"] is True
    assert result["statement_count"] == 4          # 2 DDL + INSERT + STATEMENT SET
    executed = [c.args[1] for c in client.execute_statements.call_args_list]
    explained = [c.args[1] for c in client.explain_sql.call_args_list]
    assert len(executed) == 2 and all(s.lstrip().upper().startswith(("CREATE", "--")) for s in executed)
    assert "'a;b'" in executed[0]                   # string literal kept intact
    assert len(explained) == 2
    assert "\"x;y\"" in explained[0] and explained[0].rstrip().endswith("*/")
    assert explained[1].startswith("EXECUTE STATEMENT SET") and explained[1].rstrip().endswith("END")
    assert "'it''s; fine'" in explained[1]


def test_no_naive_semicolon_split_anywhere_in_the_app():
    """Guard: no code in app/ calls <anything>.split(";")."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for p in root.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("split", "rsplit") and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == ";"):
                offenders.append(f"{p}:{node.lineno}")
    assert offenders == []
