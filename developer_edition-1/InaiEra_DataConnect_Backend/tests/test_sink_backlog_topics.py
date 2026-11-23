"""A sink's backlog is its CONNECTOR's consumer-group lag, on the topic the connector really reads. An ENFORCE sink's connector reads the
pipeline's prepared topic; measured against the input topic in its settings (which only the quality worker reads), two complete quality sinks
showed 141,001 records of lag each on 2026-11-02 (the Dashboard's "Consumer lag" 282.0k) while their groups had committed everything.

And for an ENFORCE sink, records its quality worker has not read yet are backlog too: they reach the table only after the worker passes them
on (that day, up to ~140,000 records for 8 minutes that no lag figure showed)."""

import pytest

from app.services import runtime_metrics
from app.services.runtime_metrics import connector_input_topics
from app.services.runtime_metrics import (
    sink_backlogs as real_sink_backlogs,  # conftest replaces the module attribute for every test
)

RAW = "raw-shop-pg-orders-batch-v1"
# An ENFORCE sink's connector reads its internal hand-off topic (quality_mode.SINK_HANDOFF_PREFIX).
PREPARED = "_dataconnect.gate.shop-pg-orders_copy-stream-v1"


def _sink(mode, **extra):
    return {"id": "s1", "org_id": "org_1001", "direction": "SINK", "connector_name": f"conn-{mode}", "zone": "raw", "domain": "shop",
            "source_system": "pg", "entity": "orders_copy", "mode": "stream", "version": "v1", "quality_mode": mode,
            "quality_policy_id": "q1" if mode == "ENFORCE" else None, "connector_config": {"topics": RAW}, **extra}


def test_an_enforce_sinks_connector_reads_the_prepared_topic():
    assert connector_input_topics(_sink("ENFORCE")) == [PREPARED]


def test_an_observe_sink_reads_its_configured_topics():
    assert connector_input_topics(_sink("OBSERVE")) == [RAW]
    assert connector_input_topics(_sink("OBSERVE", connector_config={"topics": f"{RAW}, raw-x-y-z-batch-v1"})) == [
        RAW, "raw-x-y-z-batch-v1"]


@pytest.mark.asyncio
async def test_the_backlog_is_measured_on_the_topic_each_connector_reads(monkeypatch):
    seen = {}

    async def _owned(org_id):
        return {"conn-ENFORCE": _sink("ENFORCE"), "conn-OBSERVE": _sink("OBSERVE"),
                "src": {"direction": "SOURCE", "connector_name": "src", "topic_name": RAW, "connector_config": {}}}

    def _blocking(bootstrap, sinks):
        seen.update({s["name"]: s["topics"] for s in sinks})
        return {s["name"]: 0 for s in sinks}

    monkeypatch.setattr(runtime_metrics, "pipeline_by_connector_name", _owned)
    monkeypatch.setattr(runtime_metrics, "_sink_backlogs_blocking", _blocking)
    runtime_metrics._BACKLOG_CACHE.clear()
    await real_sink_backlogs("org_1001")
    assert seen == {"conn-ENFORCE": [PREPARED], "conn-ENFORCE#quality": [RAW], "conn-OBSERVE": [RAW]}


@pytest.mark.asyncio
async def test_an_enforce_sinks_quality_stage_is_read_with_the_quality_workers_group(monkeypatch):
    entries = []

    async def _owned(org_id):
        return {"conn-ENFORCE": _sink("ENFORCE"), "conn-OBSERVE": _sink("OBSERVE", quality_policy_id="q1")}   # OBSERVE with a policy: no stage

    def _blocking(bootstrap, sinks):
        entries.extend(sinks)
        return {}

    monkeypatch.setattr(runtime_metrics, "pipeline_by_connector_name", _owned)
    monkeypatch.setattr(runtime_metrics, "_sink_backlogs_blocking", _blocking)
    runtime_metrics._BACKLOG_CACHE.clear()
    await real_sink_backlogs("org_1001")
    assert {e["name"]: e.get("group") for e in entries} == {
        "conn-ENFORCE": None, "conn-ENFORCE#quality": "inai-quality-s1", "conn-OBSERVE": None}


class _FakeGroupConsumer:
    """kafka.KafkaConsumer for one consumer group: what that group has committed per partition."""
    commits: dict = {}
    groups: list = []

    def __init__(self, *, group_id=None, **kwargs):
        self.group_id = group_id
        if group_id:
            _FakeGroupConsumer.groups.append(group_id)

    def partitions_for_topic(self, topic):
        return {0, 1}

    def end_offsets(self, tps):
        return dict.fromkeys(tps, 100)

    def beginning_offsets(self, tps):
        return dict.fromkeys(tps, 0)

    def assign(self, tps):
        pass

    def committed(self, tp):
        return _FakeGroupConsumer.commits.get((self.group_id, tp.topic, tp.partition))

    def close(self):
        pass


def test_the_sinks_lag_is_its_own_plus_its_quality_workers(monkeypatch):
    import kafka

    _FakeGroupConsumer.groups = []
    _FakeGroupConsumer.commits = {
        ("connect-conn-ENFORCE", PREPARED, 0): 100, ("connect-conn-ENFORCE", PREPARED, 1): 90,          # the connector: 10 left
        ("inai-quality-s1", RAW, 0): 40, ("inai-quality-s1", RAW, 1): 100,            # the worker: 60 left
        ("connect-conn-OBSERVE", RAW, 0): 100, ("connect-conn-OBSERVE", RAW, 1): 95,                    # 5 left
    }
    monkeypatch.setattr(kafka, "KafkaConsumer", _FakeGroupConsumer)
    backlogs = runtime_metrics._sink_backlogs_blocking("kafka:9092", [
        {"name": "conn-ENFORCE", "topics": [PREPARED]},
        {"name": "conn-ENFORCE#quality", "topics": [RAW], "group": "inai-quality-s1"},
        {"name": "conn-OBSERVE", "topics": [RAW]},
    ])
    assert backlogs == {"conn-ENFORCE": 70, "conn-OBSERVE": 5}
    assert "inai-quality-s1" in _FakeGroupConsumer.groups


def test_a_quality_worker_that_has_read_nothing_yet_counts_its_whole_input(monkeypatch):
    import kafka

    _FakeGroupConsumer.commits = {("connect-conn-ENFORCE", PREPARED, 0): 100, ("connect-conn-ENFORCE", PREPARED, 1): 100}
    monkeypatch.setattr(kafka, "KafkaConsumer", _FakeGroupConsumer)
    assert runtime_metrics._sink_backlogs_blocking("kafka:9092", [
        {"name": "conn-ENFORCE", "topics": [PREPARED]},
        {"name": "conn-ENFORCE#quality", "topics": [RAW], "group": "inai-quality-s1"},
    ]) == {"conn-ENFORCE": 200}


def test_the_worker_and_the_backlog_use_the_same_group_name():
    import inspect

    from app.services import quality_stream_worker
    from app.services.quality_mode import quality_consumer_group

    assert quality_consumer_group({"org_id": "org_1001", "id": "s1"}) == "inai-quality-s1"
    assert "group_id=quality_consumer_group(" in inspect.getsource(quality_stream_worker.QualityStreamWorker._run)
