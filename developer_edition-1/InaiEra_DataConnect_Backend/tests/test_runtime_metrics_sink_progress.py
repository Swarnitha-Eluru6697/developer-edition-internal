"""
runtime_metrics._sink_progress: a sink's backlog is end offset − committed,
but never counts records retention has already deleted. A sink that was
paused before committing anything, on a topic whose records have since aged
out, used to show the whole topic's end offset (14,080) as lag forever.
"""

import kafka
from kafka.structs import TopicPartition

from app.services import runtime_metrics

TOPIC = "raw-crm-src-accounts-batch-v1"


class _Admin:
    def __init__(self, start: dict[int, int], end: dict[int, int]) -> None:
        self._start, self._end = start, end

    def partitions_for_topic(self, topic):
        return set(self._end) if topic == TOPIC else None

    def end_offsets(self, tps):
        return {tp: self._end[tp.partition] for tp in tps}

    def beginning_offsets(self, tps):
        return {tp: self._start[tp.partition] for tp in tps}


def _group(committed: dict[int, int | None]):
    class _Group:
        def __init__(self, **kwargs) -> None:
            pass

        def assign(self, tps) -> None:
            pass

        def committed(self, tp: TopicPartition):
            return committed.get(tp.partition)

        def close(self) -> None:
            pass

    return _Group


def _progress(monkeypatch, *, start, end, committed) -> dict:
    monkeypatch.setattr(kafka, "KafkaConsumer", _group(committed))
    sinks = [{"name": "sink-a", "topics": [TOPIC]}]
    return runtime_metrics._sink_progress("kafka:9092", sinks, _Admin(start, end))["sink-a"]


def test_no_commit_on_a_topic_retention_has_emptied_is_zero_lag(monkeypatch) -> None:
    got = _progress(monkeypatch, start={0: 2230, 1: 2500}, end={0: 2230, 1: 2500}, committed={})
    assert got["lag"] == 0
    assert got["consumed"] == 0


def test_no_commit_counts_only_the_records_still_in_the_log(monkeypatch) -> None:
    got = _progress(monkeypatch, start={0: 100, 1: 0}, end={0: 150, 1: 40}, committed={})
    assert got["lag"] == 50 + 40


def test_a_commit_older_than_the_log_start_counts_from_the_log_start(monkeypatch) -> None:
    got = _progress(monkeypatch, start={0: 900}, end={0: 1000}, committed={0: 200})
    assert got["lag"] == 100
    assert got["consumed"] == 200


def test_a_normal_commit_is_end_minus_committed(monkeypatch) -> None:
    got = _progress(monkeypatch, start={0: 0, 1: 10}, end={0: 500, 1: 300}, committed={0: 480, 1: 300})
    assert got["lag"] == 20
    assert got["consumed"] == 780
