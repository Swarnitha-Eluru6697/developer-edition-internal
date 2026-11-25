"""NEW-06: reading recent records off a connector DLQ topic honours ONE limit for the whole topic, not one per partition.

Before, the reader took the newest `limit` records of EACH partition and its stop condition was `limit * partitions`, so `limit=200` on a 20-partition
topic could return 4000 records, in whatever order the partitions happened to be polled. Now at most `limit` records come back in total, newest first,
in a deterministic order (timestamp, then partition, then offset).

A fake Kafka consumer stands in for the broker so partition counts and timestamps can be exact. (The same check against a real broker is in
test_connector_dlq_reader_real_kafka.py.)
"""
import random
import time
from types import SimpleNamespace

import pytest

import app.services.connector_dlq_reader as reader

NOW_MS = int(time.time() * 1000)
BASE = NOW_MS - 120_000                           # all records are inside a 1-hour window
WINDOW = 3600


class FakeConsumer:
    """Just enough of kafka-python's KafkaConsumer for the reader: a topic of partitions, each a list of (timestamp_ms, headers) in offset order."""

    instances: list["FakeConsumer"] = []
    layout: dict[int, list[tuple[int, dict]]] = {}
    poll_order_seed: int | None = None
    poll_fails = False

    def __init__(self, **kwargs):
        self.positions: dict = {}
        self.delivered = 0
        self.closed = False
        FakeConsumer.instances.append(self)

    def partitions_for_topic(self, topic):
        return set(self.layout) or None

    def assign(self, tps):
        self.assigned = list(tps)

    def offsets_for_times(self, query):
        out = {}
        for tp, start_ms in query.items():
            records = self.layout.get(tp.partition, [])
            first = next((o for o, (ts, _h) in enumerate(records) if ts >= start_ms), None)
            out[tp] = SimpleNamespace(offset=first, timestamp=start_ms) if first is not None else None
        return out

    def end_offsets(self, tps):
        return {tp: len(self.layout.get(tp.partition, [])) for tp in tps}

    def seek(self, tp, offset):
        self.positions[tp] = offset

    def poll(self, timeout_ms, max_records):
        if FakeConsumer.poll_fails:
            raise RuntimeError("broker went away")
        order = list(self.positions)
        if FakeConsumer.poll_order_seed is not None:
            random.Random(FakeConsumer.poll_order_seed).shuffle(order)
        batch, budget = {}, max_records
        for tp in order:
            records = self.layout.get(tp.partition, [])
            start = self.positions[tp]
            take = records[start:start + budget]
            if take:
                batch[tp] = [SimpleNamespace(offset=start + i, timestamp=ts, headers=[(k, v.encode()) for k, v in h.items()])
                             for i, (ts, h) in enumerate(take)]
                self.positions[tp] = start + len(take)
                self.delivered += len(take)
                budget -= len(take)
            if budget <= 0:
                break
        return batch

    def close(self):
        self.closed = True


@pytest.fixture
def broker(monkeypatch):
    FakeConsumer.instances, FakeConsumer.layout, FakeConsumer.poll_order_seed, FakeConsumer.poll_fails = [], {}, None, False
    monkeypatch.setattr(reader, "KafkaConsumer", FakeConsumer)
    return FakeConsumer


def _fill(broker, partitions: int, per_partition: int, *, stamp=None) -> None:
    """`stamp(partition, offset)` -> timestamp. Default: a unique, increasing timestamp across the whole topic."""
    stamp = stamp or (lambda p, o: BASE + o * 1000 + p)
    broker.layout = {p: [(stamp(p, o), {"id": f"{p}:{o}"}) for o in range(per_partition)] for p in range(partitions)}


def _ids(result) -> list[str]:
    return [r["headers"]["id"] for r in result]


def _read(topic="t", window=WINDOW, limit=200):
    return reader._read_recent_sync(topic, window, limit)


# ── The review's check ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_limit_of_200_on_20_partitions_returns_no_more_than_200(broker) -> None:
    _fill(broker, partitions=20, per_partition=300)               # 6000 records; the old cap would have allowed 200 x 20 = 4000

    result = _read(limit=200)

    assert len(result) == 200


def test_what_comes_back_is_the_200_newest_records_of_the_whole_topic(broker) -> None:
    _fill(broker, partitions=20, per_partition=300)
    everything = [(ts, p, o) for p, records in broker.layout.items() for o, (ts, _h) in enumerate(records)]
    newest_200 = sorted(everything, key=lambda e: (-e[0], e[1], -e[2]))[:200]

    result = _read(limit=200)

    assert [(r["timestamp_ms"], r["partition"], r["offset"]) for r in result] == newest_200


def test_the_work_is_bounded_too_not_just_the_answer(broker) -> None:
    """At most `limit` candidates per partition are ever read — never a partition's whole history."""
    _fill(broker, partitions=20, per_partition=2000)              # 40,000 records on the topic

    _read(limit=200)

    assert broker.instances[0].delivered <= 200 * 20


@pytest.mark.parametrize("partitions", [1, 3, 20, 64])
@pytest.mark.parametrize("limit", [1, 10, 200])
def test_the_total_never_exceeds_the_limit_whatever_the_partition_count(broker, partitions, limit) -> None:
    _fill(broker, partitions=partitions, per_partition=250)

    assert len(_read(limit=limit)) == min(limit, partitions * 250)


# ── Order ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_newest_first(broker) -> None:
    _fill(broker, partitions=5, per_partition=50)

    stamps = [r["timestamp_ms"] for r in _read(limit=100)]

    assert stamps == sorted(stamps, reverse=True)


def test_records_with_the_same_timestamp_come_out_in_one_fixed_order(broker) -> None:
    """A batch that fails together shares a millisecond. Which of them survive the limit must not depend on polling order."""
    _fill(broker, partitions=20, per_partition=40, stamp=lambda p, o: BASE)       # every one of 800 records has the SAME timestamp

    runs = []
    for seed in (None, 1, 2, 3, 4):
        broker.poll_order_seed = seed
        runs.append(_ids(_read(limit=200)))

    assert all(run == runs[0] for run in runs)                  # the same 200 records, in the same order, every time
    assert runs[0][0] == "0:39"                                 # lowest partition first, newest offset first within it
    assert runs[0][:40] == [f"0:{o}" for o in range(39, -1, -1)]
    assert runs[0][40] == "1:39"


def test_a_smaller_limit_is_a_prefix_of_a_larger_one(broker) -> None:
    """Because the order is total: asking for fewer gives exactly the top of what asking for more gives."""
    _fill(broker, partitions=20, per_partition=100, stamp=lambda p, o: BASE + (o // 5) * 1000)    # lots of ties

    assert _ids(_read(limit=50)) == _ids(_read(limit=200))[:50]
    assert _ids(_read(limit=1)) == _ids(_read(limit=7))[:1]


def test_the_newest_records_may_all_sit_on_one_partition(broker) -> None:
    """There is no per-partition share: one hot partition can supply the whole answer."""
    _fill(broker, partitions=10, per_partition=500, stamp=lambda p, o: BASE + o if p != 3 else BASE + 100_000 + o)

    result = _read(limit=200)

    assert {r["partition"] for r in result} == {3}
    assert len(result) == 200


def test_the_answer_can_come_from_many_partitions_at_once(broker) -> None:
    _fill(broker, partitions=20, per_partition=100)

    assert len({r["partition"] for r in _read(limit=200)}) > 1


def test_candidates_are_ordered_by_timestamp_not_by_offset(broker) -> None:
    """Kafka's CreateTime can be out of order within a partition; the result is ordered by timestamp, not by log position."""
    broker.layout = {0: [(BASE + 5000, {"id": "a"}), (BASE + 9000, {"id": "b"}), (BASE + 1000, {"id": "c"})]}

    assert _ids(_read(limit=3)) == ["b", "a", "c"]               # c has the highest offset but the oldest timestamp


def test_the_candidates_are_the_newest_records_by_log_position_so_the_read_stays_bounded(broker) -> None:
    """The documented trade-off: with limit=2 only the last two offsets of a partition are candidates, so a record further back is never read."""
    broker.layout = {0: [(BASE + 5000, {"id": "a"}), (BASE + 9000, {"id": "b"}), (BASE + 1000, {"id": "c"})]}

    assert _ids(_read(limit=2)) == ["b", "c"]


# ── Edges ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_fewer_records_than_the_limit_returns_them_all(broker) -> None:
    _fill(broker, partitions=4, per_partition=3)

    assert len(_read(limit=200)) == 12


@pytest.mark.parametrize("limit", [0, -1, -200])
def test_a_limit_of_zero_or_less_returns_nothing_and_opens_no_connection(broker, limit) -> None:
    _fill(broker, partitions=2, per_partition=5)

    assert _read(limit=limit) == []
    assert broker.instances == []


def test_a_huge_limit_is_capped(broker) -> None:
    _fill(broker, partitions=4, per_partition=2000)

    assert len(_read(limit=10**9)) == reader._MAX_LIMIT


def test_a_topic_that_does_not_exist_returns_nothing(broker) -> None:
    assert _read() == []
    assert broker.instances[0].closed is True


def test_records_outside_the_window_are_not_returned(broker) -> None:
    broker.layout = {0: [(NOW_MS - 7200_000 + o, {"id": f"old{o}"}) for o in range(5)] + [(BASE + o, {"id": f"new{o}"}) for o in range(5)]}

    assert set(_ids(_read(window=3600, limit=200))) == {f"new{o}" for o in range(5)}


def test_an_empty_window_returns_nothing(broker) -> None:
    broker.layout = {0: [(NOW_MS - 7200_000, {"id": "old"})]}

    assert _read(window=3600) == []


@pytest.mark.parametrize("missing", [-1, None])
def test_a_record_with_no_timestamp_sorts_last_and_does_not_break_the_read(broker, missing) -> None:
    """Kafka reports -1 for "no timestamp"; it must rank below every real one, not crash the comparison."""
    broker.layout = {0: [(BASE, {"id": "first"}), (missing, {"id": "none"}), (BASE + 5, {"id": "last"})]}

    assert _ids(_read(limit=10)) == ["last", "first", "none"]


def test_each_record_says_where_it_came_from_and_its_headers_are_decoded(broker) -> None:
    broker.layout = {2: [(BASE, {"__connect.errors.connector.name": "c1"})]}

    (record,) = _read()

    assert record == {"timestamp_ms": BASE, "partition": 2, "offset": 0, "headers": {"__connect.errors.connector.name": "c1"}}


def test_the_connection_is_closed_even_when_the_read_fails(broker) -> None:
    _fill(broker, partitions=2, per_partition=5)
    broker.poll_fails = True

    with pytest.raises(RuntimeError):
        _read()

    assert broker.instances[0].closed is True


@pytest.mark.asyncio
async def test_the_async_entry_point_honours_the_same_limit(broker) -> None:
    _fill(broker, partitions=20, per_partition=300)

    result = await reader.read_recent_connector_errors("t", WINDOW, limit=200)

    assert len(result) == 200
