"""NEW-04: a connector dead-letter topic can be shared by several pipelines, and each failure must be filed under the pipeline whose connector wrote
it — read from the `__connect.errors.connector.name` header Kafka Connect stamps on every DLQ record — not under "the first pipeline that uses the
topic".

(The same behaviour against a real PostgreSQL, the manager and the monitoring screen is in test_dlq_shared_topic_real_db.py.)
"""
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.dlq_attribution import (
    CONNECTOR_NAME_HEADER,
    DlqAttribution,
    dlq_topic_for,
    is_dlq_sink,
    pipelines_by_dlq_topic,
)
from app.services.dlq_failure_counter import count_batch_by_pipeline

A = {"id": "pipe-a", "connector_name": "sales--crm--orders--batch--v1"}
B = {"id": "pipe-b", "connector_name": "sales--crm--orders--stream--v1"}
OLDEST = datetime.now(UTC) - timedelta(days=1)


def _record(connector: str | None, *, category: str = "serialization", minutes_ago: int = 1, offset: int = 0):
    ser = category == "serialization"
    headers = [
        ("__connect.errors.stage", b"VALUE_CONVERTER" if ser else b"TASK_PUT"),
        ("__connect.errors.exception.class.name", b"org.apache.kafka.connect.errors.DataException" if ser else b"java.sql.SQLException"),
        ("__connect.errors.exception.message", b"Converting byte[] to Kafka Connect data failed due to serialization error" if ser else b"connection reset"),
    ]
    if connector is not None:
        headers.append((CONNECTOR_NAME_HEADER, connector.encode()))
    return SimpleNamespace(topic="t", partition=0, offset=offset, timestamp=int((time.time() - minutes_ago * 60) * 1000), headers=headers)


# ── Whose is a record ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_record_is_attributed_by_the_connector_name_on_it() -> None:
    shared = DlqAttribution([A, B])

    assert shared.pipeline_id_for({CONNECTOR_NAME_HEADER: A["connector_name"]}) == "pipe-a"
    assert shared.pipeline_id_for({CONNECTOR_NAME_HEADER: B["connector_name"]}) == "pipe-b"


def test_a_connector_that_is_not_one_of_the_topics_pipelines_belongs_to_nobody() -> None:
    """A deleted pipeline's leftovers, or another connector entirely: never filed under whichever pipeline remains."""
    assert DlqAttribution([A, B]).pipeline_id_for({CONNECTOR_NAME_HEADER: "gone--x--y--batch--v1"}) is None
    assert DlqAttribution([A]).pipeline_id_for({CONNECTOR_NAME_HEADER: "gone--x--y--batch--v1"}) is None


def test_a_record_with_no_connector_header_on_a_shared_topic_belongs_to_nobody() -> None:
    """It could be either pipeline's. Guessing — the first one — is exactly the bug."""
    assert DlqAttribution([A, B]).pipeline_id_for({}) is None
    assert DlqAttribution([A, B]).pipeline_id_for({CONNECTOR_NAME_HEADER: "  "}) is None


def test_a_record_with_no_header_on_a_topic_with_one_pipeline_is_that_pipelines() -> None:
    """It can only be theirs (for instance the context headers were switched off), so it is still counted."""
    assert DlqAttribution([A]).pipeline_id_for({}) == "pipe-a"


def test_a_topic_nobody_writes_attributes_nothing() -> None:
    assert DlqAttribution([]).pipeline_id_for({}) is None
    assert DlqAttribution([]).pipeline_id_for({CONNECTOR_NAME_HEADER: A["connector_name"]}) is None


def test_the_connector_name_is_matched_exactly_not_by_prefix_or_case() -> None:
    shared = DlqAttribution([A])

    for look_alike in (A["connector_name"] + "-2", A["connector_name"][:-1], A["connector_name"].upper(), " " + A["connector_name"] + "x"):
        assert shared.pipeline_id_for({CONNECTOR_NAME_HEADER: look_alike}) is None, look_alike


# ── Which pipelines share a topic ──────────────────────────────────────────────────────────────────────────────────────

def _pipeline(pid: str, mode: str, **over) -> dict:
    return {
        "id": pid, "org_id": "org_1", "name": f"pipeline {pid}", "connector_name": f"sales--crm--orders--{mode}--v1",
        "direction": "SINK", "error_handling_policy": "DLQ_AND_CONTINUE", "domain": "sales", "source_system": "crm", "entity": "orders",
        "version": "v1", "mode": mode, **over,
    }


def test_two_sinks_that_differ_only_in_mode_share_one_dlq_topic_and_both_are_listed() -> None:
    topics = pipelines_by_dlq_topic([_pipeline("a", "batch"), _pipeline("b", "stream")])

    assert list(topics) == ["quarantine-sales-crm-orders-connector-v1"]
    assert [p["id"] for p in next(iter(topics.values()))] == ["a", "b"]           # in the order given: the first is not the only one


def test_sinks_with_different_entities_do_not_share() -> None:
    topics = pipelines_by_dlq_topic([_pipeline("a", "batch"), _pipeline("b", "batch", entity="invoices")])

    assert len(topics) == 2


@pytest.mark.parametrize("over", [
    {"direction": "SOURCE"},                                  # Kafka Connect gives a source no dead-letter queue
    {"error_handling_policy": "FAIL_FAST"},
    {"error_handling_policy": "SKIP_AND_LOG"},
    {"connector_name": None},                                 # never deployed
    {"connector_name": ""},
    {"domain": "Bad Domain"},                                 # does not form a topic name
])
def test_only_deployed_dead_letter_sinks_with_a_valid_name_count(over) -> None:
    assert pipelines_by_dlq_topic([_pipeline("a", "batch", **over)]) == {}


def test_a_threshold_sink_writes_a_dlq_too_and_direction_is_case_insensitive() -> None:
    assert is_dlq_sink(_pipeline("a", "batch", direction="sink", error_handling_policy="THRESHOLD"))
    assert dlq_topic_for(_pipeline("a", "batch")) == "quarantine-sales-crm-orders-connector-v1"
    assert dlq_topic_for({"org_id": "org_1"}) is None


# ── Counting ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def _totals(per_pipeline) -> dict[str, int]:
    return {pid: sum(counts.values()) for pid, counts in per_pipeline.items()}


def test_injected_failures_for_two_pipelines_sharing_a_dlq_are_counted_separately() -> None:
    """The review's check: failures from two pipelines on one DLQ."""
    records = (
        [_record(A["connector_name"], offset=i) for i in range(3)]
        + [_record(B["connector_name"], offset=10 + i) for i in range(2)]
    )

    per_pipeline, unattributed = count_batch_by_pipeline(records, oldest=OLDEST, attribution=DlqAttribution([A, B]))

    assert _totals(per_pipeline) == {"pipe-a": 3, "pipe-b": 2}
    assert unattributed == 0


def test_the_causes_are_kept_per_pipeline() -> None:
    records = [
        _record(A["connector_name"], category="serialization"), _record(A["connector_name"], category="serialization"),
        _record(B["connector_name"], category="connector"),
    ]

    per_pipeline, _ = count_batch_by_pipeline(records, oldest=OLDEST, attribution=DlqAttribution([A, B]))

    by_category = {pid: {category: n for (_bucket, category), n in counts.items()} for pid, counts in per_pipeline.items()}
    assert by_category == {"pipe-a": {"serialization": 2}, "pipe-b": {"connector": 1}}


def test_records_that_cannot_be_attributed_are_counted_for_no_pipeline_and_reported() -> None:
    records = [
        _record(A["connector_name"]),
        _record(None),                                          # shared topic, no header
        _record("gone--x--y--batch--v1"),                # a connector that is not either of ours
        _record(B["connector_name"]),
    ]

    per_pipeline, unattributed = count_batch_by_pipeline(records, oldest=OLDEST, attribution=DlqAttribution([A, B]))

    assert _totals(per_pipeline) == {"pipe-a": 1, "pipe-b": 1}
    assert unattributed == 2


def test_records_older_than_the_retention_window_are_neither_counted_nor_called_unattributed() -> None:
    old = _record(A["connector_name"], minutes_ago=60 * 24 * 10)
    old_no_header = _record(None, minutes_ago=60 * 24 * 10)

    per_pipeline, unattributed = count_batch_by_pipeline([old, old_no_header], oldest=OLDEST, attribution=DlqAttribution([A, B]))

    assert per_pipeline == {}
    assert unattributed == 0


def test_a_single_pipeline_topic_counts_header_less_records_for_it_but_not_another_connectors() -> None:
    records = [_record(None), _record(A["connector_name"]), _record("other--x--y--batch--v1")]

    per_pipeline, unattributed = count_batch_by_pipeline(records, oldest=OLDEST, attribution=DlqAttribution([A]))

    assert _totals(per_pipeline) == {"pipe-a": 2}
    assert unattributed == 1
