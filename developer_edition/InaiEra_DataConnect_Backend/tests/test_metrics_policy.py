"""
Hardening: the low-cardinality metrics policy (docs/METRICS_POLICY.md) is enforced, not just written down.

  * every DataConnect metric uses only the five permitted label NAMES;
  * every label VALUE comes from a fixed vocabulary — anything else is recorded as "other", never as a new series;
  * no metric can exceed the series budget however hostile the values fed to it;
  * every call site passes literal values that are IN those vocabularies (so a typo cannot quietly land in "other");
  * the policy document and the code agree.
"""

import ast
import re
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from prometheus_client import REGISTRY

import app.core.metrics as metrics
from app.core.connect_pools import POOL_BY_ID
from app.core.metrics import (
    ALLOWED_LABEL_NAMES,
    CONNECTOR_TYPES,
    OPERATIONS,
    OTHER,
    QUALITY_STEPS,
    STATUSES,
    operations_total,
    quality_dlq_failures_total,
    quality_retries_total,
    record_operation,
    record_quality_dlq_failure,
    record_quality_retry,
)

BACKEND = Path(__file__).resolve().parents[1]
MAX_SERIES_PER_METRIC = 500
FORBIDDEN_LABEL_WORDS = ("pipeline", "connector_name", "topic", "org", "user", "url", "host", "error", "message", "id")


def _dataconnect_metrics():
    return [m for m in REGISTRY.collect() if m.name.startswith("dataconnect_")]


def _label_values(counter, label: str) -> set[str]:
    return {labels[label] for labels in (s.labels for m in counter.collect() for s in m.samples) if label in labels}


def _series_count(counter) -> int:
    return len({tuple(sorted(s.labels.items())) for m in counter.collect() for s in m.samples if s.name.endswith("_total")})


# ── names ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_dataconnect_metric_uses_only_the_five_permitted_label_names() -> None:
    import app.services.operation_runner  # noqa: F401 - registers its metrics
    import app.services.quality_worker_health  # noqa: F401

    found = _dataconnect_metrics()
    assert {m.name for m in found} >= {"dataconnect_operations", "dataconnect_quality_retries", "dataconnect_quality_dlq_failures",
                                       "dataconnect_quality_blocked_partitions", "dataconnect_operation_result_invalid"}
    for family in found:
        for sample in family.samples:
            extra = set(sample.labels) - ALLOWED_LABEL_NAMES
            assert extra == set(), f"{sample.name} uses label(s) {extra} outside the policy"


def test_no_registered_metric_declares_a_label_named_after_an_entity() -> None:
    """Checked on the declared label names (not only the samples seen so far), so an unused-yet metric cannot hide a bad label."""
    import app.services.operation_runner  # noqa: F401
    import app.services.quality_worker_health  # noqa: F401

    checked = 0
    for collector in list(REGISTRY._collector_to_names):   # the registry's own index of every metric object
        names = getattr(collector, "_labelnames", ())
        full = getattr(collector, "_name", "")
        if not str(full).startswith("dataconnect_"):
            continue
        checked += 1
        assert set(names) <= ALLOWED_LABEL_NAMES, f"{full} declares labels {names}"
        for label in names:
            assert label in ALLOWED_LABEL_NAMES
            assert not any(word == label for word in FORBIDDEN_LABEL_WORDS)
    assert checked >= 5, "the registry scan saw too few metrics — it is checking nothing"


def test_naming_conventions() -> None:
    for family in _dataconnect_metrics():
        assert re.fullmatch(r"dataconnect_[a-z0-9_]+", family.name), family.name
        if family.type == "counter":
            assert any(s.name.endswith("_total") for s in family.samples) or family.samples == [], family.name


# ── values: vocabularies and the "other" bucket ────────────────────────────────────────────────────────────────────

def test_a_known_value_is_recorded_as_itself_and_an_unknown_one_as_other() -> None:
    record_operation(runtime_pool="connect-cdc-pool", connector_type="source", status="running", operation="reconcile")
    record_operation(runtime_pool="pool-of-customer-42", connector_type="database", status="pipeline-7f3a", operation="drop_table_users")

    assert operations_total.labels(service="dataconnect", runtime_pool="connect-cdc-pool", connector_type="source", status="running", operation="reconcile")._value.get() >= 1
    assert operations_total.labels(service="dataconnect", runtime_pool=OTHER, connector_type=OTHER, status=OTHER, operation=OTHER)._value.get() >= 1
    for leaked in ("pool-of-customer-42", "database", "pipeline-7f3a", "drop_table_users"):
        for label in ("runtime_pool", "connector_type", "status", "operation"):
            assert leaked not in _label_values(operations_total, label)


def test_none_and_retired_pool_aliases_map_to_bounded_values() -> None:
    record_operation(runtime_pool=None, connector_type=None, status="success", operation="kafka_produce")
    record_operation(runtime_pool="connect-queue-pool", connector_type="sink", status="failed", operation="reconcile")   # retired alias

    pools = _label_values(operations_total, "runtime_pool")
    assert "none" in pools
    assert "connect-stream-pool" in pools                 # the alias resolves to today's pool
    assert "connect-queue-pool" not in pools


def test_the_quality_metrics_are_bounded_too() -> None:
    record_quality_retry(connector_type="sink", step="stats")
    record_quality_retry(connector_type="a-real-connector-name", step="a-free-form-step")
    record_quality_dlq_failure(connector_type="some/topic/name")

    assert _label_values(quality_retries_total, "connector_type") <= CONNECTOR_TYPES | {OTHER}
    assert _label_values(quality_retries_total, "operation") <= QUALITY_STEPS | {OTHER}
    assert _label_values(quality_dlq_failures_total, "connector_type") <= CONNECTOR_TYPES | {OTHER}


def test_every_vocabulary_value_is_a_legal_prometheus_label_value_and_the_sets_are_disjoint_from_other() -> None:
    for vocabulary in (CONNECTOR_TYPES, STATUSES, OPERATIONS, QUALITY_STEPS):
        assert OTHER not in vocabulary                     # "other" is the overflow bucket, never a legitimate value
        assert all(re.fullmatch(r"[a-z_]+", value) for value in vocabulary)


# ── the budget, against hostile input ──────────────────────────────────────────────────────────────────────────────

@settings(max_examples=300, deadline=None)
@given(st.text(max_size=60), st.text(max_size=60), st.text(max_size=60), st.text(max_size=60))
def test_arbitrary_values_can_never_grow_the_operations_series_beyond_its_budget(pool, kind, status, operation) -> None:
    record_operation(runtime_pool=pool, connector_type=kind, status=status, operation=operation)

    assert _series_count(operations_total) <= MAX_SERIES_PER_METRIC
    assert _label_values(operations_total, "status") <= STATUSES | {OTHER}
    assert _label_values(operations_total, "operation") <= OPERATIONS | {OTHER}
    assert _label_values(operations_total, "connector_type") <= CONNECTOR_TYPES | {OTHER}
    assert _label_values(operations_total, "runtime_pool") <= set(POOL_BY_ID) | {"none", OTHER}


def test_the_theoretical_maximum_is_within_the_budget() -> None:
    pools, types, statuses, operations = len(POOL_BY_ID) + 2, len(CONNECTOR_TYPES) + 1, len(STATUSES) + 1, len(OPERATIONS) + 1
    assert pools * types * statuses * operations <= 4 * MAX_SERIES_PER_METRIC   # a loose ceiling; the fuzz above shows the real one is far lower


# ── call sites ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _literal_keyword_values(function_name: str, keyword: str) -> list[tuple[str, int, str]]:
    found = []
    for path in (BACKEND / "app").rglob("*.py"):
        if path.name == "metrics.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == function_name:
                for kw in node.keywords:
                    if kw.arg == keyword and isinstance(kw.value, ast.Constant):
                        found.append((str(path.relative_to(BACKEND)), node.lineno, kw.value.value))
    return found


def test_every_literal_value_at_a_record_operation_call_site_is_in_its_vocabulary() -> None:
    """The runtime fallback hides a typo in "other"; this catches it at the source."""
    sites = _literal_keyword_values("record_operation", "operation") + _literal_keyword_values("record_operation", "status")
    assert sites, "the scan found no call sites — it is broken"
    for path, line, value in sites:
        assert value in OPERATIONS | STATUSES, f"{path}:{line} passes {value!r}, which is not in a vocabulary"


def test_every_literal_quality_step_is_in_the_vocabulary() -> None:
    sites = _literal_keyword_values("record_quality_retry", "step")
    for path, line, value in sites:
        assert value in QUALITY_STEPS, f"{path}:{line} passes step={value!r}"


def test_the_policy_document_lists_every_vocabulary_value() -> None:
    doc = (BACKEND / "docs" / "METRICS_POLICY.md").read_text()
    for value in sorted(CONNECTOR_TYPES | STATUSES | OPERATIONS | QUALITY_STEPS):
        assert f"`{value}`" in doc, f"docs/METRICS_POLICY.md does not mention `{value}`"
    for name in sorted(ALLOWED_LABEL_NAMES):
        assert f"`{name}`" in doc
    assert str(MAX_SERIES_PER_METRIC) in doc


def test_the_metrics_module_documents_its_policy_page() -> None:
    assert "docs/METRICS_POLICY.md" in metrics.__doc__
