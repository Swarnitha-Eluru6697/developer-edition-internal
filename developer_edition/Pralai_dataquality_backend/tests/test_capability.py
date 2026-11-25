"""DQ-MODEL-002 matrix + DQ-CAP-007 classification."""
from app.renderers.capability import ENGINE_CAPABILITY_MATRIX, classify_items
from tests.helpers import item, source_table


def test_matrix_semantics_exactly_per_review():
    m = ENGINE_CAPABILITY_MATRIX
    assert m["dbt_project"]["modes"]["blocking"]["mechanism"] == "test config.severity=error"
    assert "fails `dbt build`" in m["dbt_project"]["modes"]["blocking"]["semantics"]
    assert m["dbt_project"]["modes"]["warn"]["mechanism"] == "test config.severity=warn"
    assert "separate `dbt test" in m["dbt_project"]["modes"]["async"]["semantics"]
    fb = m["flink_job"]["modes"]["blocking"]
    assert "quarantine" in fb["mechanism"] and "gate" in fb["mechanism"]
    assert "ONLY when the binding explicitly sets flink_fail_stop=true" in fb["semantics"]
    assert "fail (non-zero exit) the task" in m["python_job"]["modes"]["blocking"]["semantics"]
    for jt in m:
        assert set(m[jt]["modes"]) == {"blocking", "warn", "async"}


def test_classification_per_engine():
    items = [item("not_null", "order_id"), item("row_count"), item("unique", "order_id"),
             item("custom_sql", params={"condition": "a > 0"}, review_status="pending"),
             item("not_null", "x", enabled=False)]
    dbt = classify_items(items, "dbt_project", "blocking")
    assert [e["rule_type"] for e in dbt["supported"]] == ["not_null", "unique"]
    assert {e["rule_type"] for e in dbt["unsupported"]} == {"row_count", "custom_sql"}
    assert dbt["degraded"] == []
    fl = classify_items(items, "flink_job", "blocking", source=source_table())
    assert {e["rule_type"] for e in fl["unsupported"]} == {"row_count", "unique", "custom_sql"}
    assert [e["rule_type"] for e in fl["degraded"]] == ["not_null"]
    assert "keeps running" in fl["degraded"][0]["reason"]
    fl_fs = classify_items(items, "flink_job", "blocking", flink_fail_stop=True)
    assert "stops the parent job" in fl_fs["degraded"][0]["reason"]
    assert classify_items(items, "flink_job", "warn")["degraded"] == []
    py = classify_items(items, "python_job", "blocking")
    assert {e["rule_type"] for e in py["supported"]} == {"not_null", "row_count", "unique"}


async def test_capability_api_lists_matrix(client):
    async with client as c:
        r = await c.get("/api/v1/capabilities")
        one = await c.get("/api/v1/capabilities/flink_job")
        missing = await c.get("/api/v1/capabilities/nope")
    assert r.status_code == 200
    body = r.json()
    assert set(body["engines"]) == {"dbt_project", "flink_job", "python_job"}
    assert body["engines"]["flink_job"]["supported_engine_versions"]["min"] == "1.17.0"
    assert one.json()["modes"]["blocking"]["guarantee"] == "degraded"
    assert missing.status_code == 404
