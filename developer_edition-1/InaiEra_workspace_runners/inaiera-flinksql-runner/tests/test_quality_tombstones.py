"""FL-006: a Kafka tombstone (null value) is classified explicitly and durably before its offset moves.

It used to be skipped with `continue`: a batch of only tombstones wrote nothing and committed nothing (so a restart re-read them), and in a
mixed batch the tombstones' offsets were committed along with the others without any record that they had been seen.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app.models import QualityResultOutbox
from app.services import quality_consumer as qc
from tests.quality_records import window_result
from tests.test_quality_consumer_durability import _TP, _fake_settings, _Msg

TOPIC = "dq.quality.results.v2"


async def _run(poll_results, write_outbox, settings=None):
    calls: list = []
    fake = MagicMock()
    fake.poll.side_effect = lambda **kw: (poll_results.pop(0) if poll_results else {})
    fake.commit.side_effect = lambda: calls.append("commit")

    async def _write(entries):
        calls.append(("write", list(entries)))
        await write_outbox(entries)

    async def _drain(client, s):
        return (0, 0)

    async def _no_sweep(s):
        return 0

    with patch.object(qc, "_make_consumer", return_value=fake), patch.object(qc, "_write_outbox", side_effect=_write), \
            patch.object(qc, "_drain_outbox", side_effect=_drain), patch.object(qc, "_sweep_stale", side_effect=_no_sweep), \
            patch.object(qc, "get_settings", return_value=settings or _fake_settings()):
        task = asyncio.create_task(qc.quality_consumer_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return calls


async def _noop(entries):
    return None


def test_a_tombstone_becomes_an_explicit_ignored_entry():
    e = qc.tombstone_entry(TOPIC, 2, 17)
    assert e.kind == qc.KIND_DEAD and e.status == "skipped" and e.key == f"tombstone:{TOPIC}-2-17"
    assert e.payload["classification"] == "tombstone" and e.payload["offset"] == 17 and e.payload["record"] is None


async def test_a_tombstone_only_batch_is_durably_classified_then_committed():
    calls = await _run([{_TP(TOPIC, 0): [_Msg(None, 5), _Msg(None, 6)]}], _noop)
    writes = [c for c in calls if isinstance(c, tuple)]
    assert len(writes) == 1 and [e.key for e in writes[0][1]] == [f"tombstone:{TOPIC}-0-5", f"tombstone:{TOPIC}-0-6"]
    assert "commit" in calls and calls.index(writes[0]) < calls.index("commit")      # the offset only moves after the write


async def test_a_mixed_batch_classifies_every_record_before_the_commit():
    msgs = [_Msg(window_result(), 1), _Msg(None, 2), _Msg({"rule_name": "old-shape"}, 3)]
    calls = await _run([{_TP(TOPIC, 0): msgs}], _noop)
    (write,) = [c for c in calls if isinstance(c, tuple)]
    kinds = [(e.kind, e.status) for e in write[1]]
    assert kinds == [("quality_run", "pending"), ("dead_letter", "skipped"), ("dead_letter", "rejected")]    # one entry per record, in order
    assert calls.index(write) < calls.index("commit")


async def test_the_offset_is_not_committed_when_the_tombstone_cannot_be_written():
    async def _fail(entries):
        raise RuntimeError("db down")
    calls = await _run([{_TP(TOPIC, 0): [_Msg(None, 9)]}], _fail)
    assert "commit" not in calls


async def test_a_replayed_tombstone_collapses_onto_one_row(pg_db):
    await qc._write_outbox([qc.tombstone_entry(TOPIC, 0, 5)])
    await qc._write_outbox([qc.tombstone_entry(TOPIC, 0, 5)])                  # the batch is re-read after a restart
    async with pg_db() as db:
        rows = list((await db.execute(select(QualityResultOutbox))).scalars())
    assert len(rows) == 1 and rows[0].kind == "dead_letter" and rows[0].status == "skipped"
    assert rows[0].payload["classification"] == "tombstone" and "tombstone" in rows[0].last_error


async def test_a_tombstone_row_is_never_delivered_to_ucs(pg_db):
    from tests.test_quality_outbox_pg import _settings, _Ucs
    await qc._write_outbox([qc.tombstone_entry(TOPIC, 0, 5)])
    ucs = _Ucs()
    await qc._drain_outbox(ucs, _settings(ucs_quality_runs_enabled=True))
    assert ucs.posts == []
