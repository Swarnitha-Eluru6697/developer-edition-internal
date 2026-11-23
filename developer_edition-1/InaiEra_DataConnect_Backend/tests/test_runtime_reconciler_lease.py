"""
GAP-08, scenario A: "Process A owns pipeline -> A dies -> lease expires ->
B claims pipeline -> B reconciles." Nothing coordinated concurrent
`runtime_reconciler.run_forever()` processes before this fix — every
process reconciled every PipelineRuntimeState row, every tick, with no
per-row ownership at all (app/services/runtime_ownership.py, despite its
name, is a tenant-scoped connector/topic-name attribution helper, unrelated
to this). `RuntimeReconciler._claim_rows` / `PipelineRuntimeStateRepository.
claim_batch` (app/repositories/dataconnect.py) is the fix: an atomic
claim/lease, same shape as BUG-02's operation_runner._claim_next.

Exercised here against `_FakePipelineRuntimeStateRepository.claim_batch`
(tests/conftest.py) — a plain-Python mirror of the real SQL's 3-clause
eligibility (unleased, already ours, or lease lapsed), not the real
Postgres statement (`FOR UPDATE SKIP LOCKED`, true cross-transaction
atomicity) — this sandbox has no live Postgres to exercise that against,
same acknowledged limitation as BUG-02's own claim query, which has no
dedicated test either. What's verified here is the ownership-handoff
DECISION LOGIC: who gets claimed, when, and by whom.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_a_fresh_row_is_claimed_by_whichever_process_ticks_first(fake_postgres) -> None:
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")

    claimed = await process_a._claim_rows()

    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert claimed == [row.id]
    assert row.leased_by == "process-a"
    assert row.lease_expires_at is not None


@pytest.mark.asyncio
async def test_a_second_process_cannot_claim_a_row_whose_lease_is_still_valid(fake_postgres) -> None:
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")
    process_b = RuntimeReconciler(worker_id="process-b")

    await process_a._claim_rows()
    claimed_by_b = await process_b._claim_rows()

    assert claimed_by_b == []
    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert row.leased_by == "process-a"  # ownership unchanged


@pytest.mark.asyncio
async def test_process_a_keeps_renewing_its_own_lease_every_tick(fake_postgres) -> None:
    """A live process re-claiming its own rows every tick must not be
    treated as a handoff — it's the same process extending its own
    ownership, not losing and re-winning it."""
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")

    first = await process_a._claim_rows()
    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    lease_after_first_tick = row.lease_expires_at

    second = await process_a._claim_rows()
    lease_after_second_tick = row.lease_expires_at

    assert first == second  # same row, claimed again
    assert row.leased_by == "process-a"
    assert lease_after_second_tick > lease_after_first_tick  # renewed forward, not left stale


@pytest.mark.asyncio
async def test_process_b_claims_after_process_as_lease_expires(fake_postgres, monkeypatch) -> None:
    """The ticket's own diagram: Process A owns pipeline -> A dies -> lease
    expires -> B claims pipeline -> B reconciles."""
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")
    process_b = RuntimeReconciler(worker_id="process-b")

    await process_a._claim_rows()
    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert row.leased_by == "process-a"

    # A dies — it never ticks again, so its lease just sits there and lapses.
    # Simulate time passing past runtime_reconciler_lease_seconds by
    # backdating the lease directly, the same observable end state a real
    # clock advancing would produce.
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    claimed_by_b = await process_b._claim_rows()

    assert claimed_by_b == [row.id]
    assert row.leased_by == "process-b"  # ownership transferred


@pytest.mark.asyncio
async def test_two_processes_split_ownership_of_multiple_pipelines(fake_postgres) -> None:
    """Not just handoff-after-death — two live processes ticking at the
    same time must never both claim (and therefore both act on) the SAME
    row; each row is claimed by exactly one of them."""
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-2", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")
    process_b = RuntimeReconciler(worker_id="process-b")

    claimed_by_a = set(await process_a._claim_rows())
    claimed_by_b = set(await process_b._claim_rows())

    all_ids = {row.id for row in fake_postgres.pipeline_runtime_states.values()}
    assert claimed_by_a == all_ids  # A ticked first, got everything unleased
    assert claimed_by_b == set()    # nothing left for B — no double ownership
    assert not (claimed_by_a & claimed_by_b)


@pytest.mark.asyncio
async def test_lease_duration_uses_the_configured_setting(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(settings, "runtime_reconciler_lease_seconds", 45.0)
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1"})
    process_a = RuntimeReconciler(worker_id="process-a")

    before = datetime.now(UTC)
    await process_a._claim_rows()
    after = datetime.now(UTC)

    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert before + timedelta(seconds=44) < row.lease_expires_at < after + timedelta(seconds=46)


def test_two_reconciler_instances_get_distinct_worker_ids_by_default() -> None:
    """No two RuntimeReconciler instances constructed without an explicit
    worker_id (i.e. two real, independent processes) should ever collide —
    that would make _claim_rows treat them as the same owner."""
    a = RuntimeReconciler()
    b = RuntimeReconciler()
    assert a._worker_id != b._worker_id
