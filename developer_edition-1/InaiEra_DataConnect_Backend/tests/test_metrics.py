import inspect

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.core.metrics import operations_total, record_operation, render_metrics
from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)  # /metrics is behind the gateway (app/core/gateway.py)


def _count(**labels) -> float:
    labels.setdefault("service", "dataconnect")
    try:
        return operations_total.labels(**labels)._value.get()
    except KeyError:
        return 0.0


def test_record_operation_only_accepts_the_five_safe_labels() -> None:
    """The signature itself is the enforcement mechanism — there is no
    connector_name/topic_name/error kwarg to accidentally pass."""
    params = set(inspect.signature(record_operation).parameters)

    assert params == {"runtime_pool", "connector_type", "status", "operation"}


def test_record_operation_rejects_unknown_labels() -> None:
    with pytest.raises(TypeError):
        record_operation(  # type: ignore[call-arg]
            runtime_pool="connect-cdc-pool",
            connector_type="source",
            status="running",
            operation="reconcile",
            connector_name="should-not-be-accepted",
        )


def test_record_operation_maps_missing_dimensions_to_bounded_sentinels() -> None:
    before = _count(runtime_pool="none", connector_type="unknown", status="success", operation="kafka_produce")

    record_operation(runtime_pool=None, connector_type=None, status="success", operation="kafka_produce")

    after = _count(runtime_pool="none", connector_type="unknown", status="success", operation="kafka_produce")
    assert after == before + 1


def test_record_operation_increments_the_counter_for_its_exact_label_set() -> None:
    before = _count(runtime_pool="connect-jdbc-pool", connector_type="sink", status="failed", operation="reconcile")

    record_operation(runtime_pool="connect-jdbc-pool", connector_type="sink", status="failed", operation="reconcile")

    after = _count(runtime_pool="connect-jdbc-pool", connector_type="sink", status="failed", operation="reconcile")
    assert after == before + 1


def test_metrics_endpoint_exposes_the_operations_counter() -> None:
    record_operation(runtime_pool=None, connector_type=None, status="success", operation="kafka_produce")

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "dataconnect_operations_total" in resp.text
    assert 'operation="kafka_produce"' in resp.text


def test_render_metrics_matches_generate_latest_output() -> None:
    payload = render_metrics()

    assert b"dataconnect_operations_total" in payload
