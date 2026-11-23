"""
GAP-08 qualification: the two failure/race scenarios the ticket asks to be explicitly tested, plus the holes found
auditing them.

  A. Process A owns a pipeline -> A dies -> lease expires -> B claims it -> B reconciles.
  B. A reconciliation is running + the user edits the pipeline -> new generation -> the old reconciliation's result
     MUST NOT overwrite the new desired state.

The hand-rolled tick doubles from tests/test_runtime_reconciler_convergence.py are reused (see its docstring for why
_reconcile_row can't run on FakePostgres alone); ownership handoff across ticks uses FakePostgres, which mirrors the
claim/renew/fence queries but not true cross-transaction atomicity (a sandbox limit shared with BUG-02's claim query).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.runtime_reconciler as reconciler_module
from app.core.config import settings
from app.services.pipeline_service import pipeline_service
from app.services.runtime_reconciler import RuntimeReconciler
from tests.test_runtime_reconciler_convergence import (
    _FakeRepo,
    _FakeSession,
    _install_fakes,
    _row,
)


@pytest.fixture(autouse=True)
def _no_plugin_preflight(monkeypatch):
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)


class _ScriptedRepo(_FakeRepo):
    """fence() answers from a script of (live_generation, still_owner) so a test can change the world mid-tick: the
    Nth call returns the Nth entry (the last repeats)."""

    def __init__(self, script):
        super().__init__(session=None)
        self._script = list(script)
        self.fence_calls = 0

    async def fence(self, row_id, worker_id, *, lock=False):
        index = min(self.fence_calls, len(self._script) - 1)
        self.fence_calls += 1
        return self._script[index]


async def _tick(monkeypatch, *, script, deploy, resolved_hash="hash-new", stored_hash="hash-old", exists=True):
    """One _reconcile_row where the pipeline's resolved config differs from the stored hash (so a deploy is planned)."""
    SENT_TO_CONNECT.clear()
    calls, deployed = _install_fakes(
        monkeypatch, status_sequence=["RUNNING"], resolved_config_hash=resolved_hash
    )
    monkeypatch.setattr(reconciler_module, "deploy_if_current", deploy)
    row = _row(desired_state="running", desired_generation=1, config_hash=stored_hash)
    repo = _ScriptedRepo(script)
    reconciler = RuntimeReconciler(worker_id="process-a")
    result = await reconciler._reconcile_row(
        _FakeSession(live_desired_generation=1), repo, client=None, existing={"conn-1"} if exists else set(), row=row,
    )
    return calls, deployed, row, result


SENT_TO_CONNECT: list[str] = []


async def _applied(client, connector_name, config, *, still_current):
    SENT_TO_CONNECT.append(connector_name)
    return {"config": config}


async def _superseded(client, connector_name, config, *, still_current):
    return None  # what deploy_if_current returns when still_current() turned false while it was retrying


# ── B. A reconciliation racing a user edit ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_deploy_superseded_mid_flight_is_not_recorded_as_the_applied_config(monkeypatch) -> None:
    """The regression behind the audit: deploy_if_current returns None when the tick was superseded, which used to
    look like success — the new hash was then stored as applied, the next tick saw "no change", and the config was
    never deployed."""
    _, _, row, _ = await _tick(monkeypatch, script=[(1, True)], deploy=_superseded)

    assert row.config_hash == "hash-old"  # not recorded -> the next tick still sees a change and deploys
    assert row.observed_generation == 0


@pytest.mark.asyncio
async def test_the_next_tick_then_deploys_the_config_the_superseded_tick_never_applied(monkeypatch) -> None:
    calls, _, row, _ = await _tick(monkeypatch, script=[(1, True)], deploy=_superseded)
    assert calls["deploy"] == []

    calls2, deployed2 = _install_fakes(monkeypatch, status_sequence=["RUNNING"], resolved_config_hash="hash-new")
    reconciler = RuntimeReconciler(worker_id="process-a")
    await reconciler._reconcile_row(
        _FakeSession(1), _ScriptedRepo([(1, True)]), client=None, existing={"conn-1"}, row=row,
    )

    assert calls2["deploy"] == ["conn-1"]
    assert row.config_hash == "hash-new"


@pytest.mark.asyncio
async def test_an_edit_recorded_after_the_deploy_leaves_the_stored_hash_and_generation_alone(monkeypatch) -> None:
    """The deploy went through, but the user's edit (generation 2) was recorded before this tick wrote its result: the
    result must not claim convergence with, or overwrite the hash of, the newer intent."""
    # fence #1 is the pre-action freshness check (still generation 1); fence #2 is the final write fence (generation 2).
    _, _, row, _ = await _tick(monkeypatch, script=[(1, True), (2, True)], deploy=_applied)

    assert row.config_hash == "hash-old"
    assert row.observed_generation == 0


@pytest.mark.asyncio
async def test_a_current_applied_deploy_still_records_its_hash_and_converges(monkeypatch) -> None:
    """Regression control: nothing raced, so the original behaviour is unchanged."""
    _, _, row, _ = await _tick(monkeypatch, script=[(1, True)], deploy=_applied)

    assert row.config_hash == "hash-new"
    assert row.observed_generation == 1


@pytest.mark.asyncio
async def test_a_pipeline_edit_bumps_the_generation_so_an_in_flight_tick_sees_it_as_stale(fake_postgres) -> None:
    """A pipeline edit changes neither desired_state nor (through record_desired) the stored hash, so it used to leave
    the generation untouched and a reconcile that resolved the old spec looked perfectly current."""
    fake_postgres.create(
        "pipeline_runtime_states", {"pipeline_id": "pipe-1", "org_id": "org-1", "desired_generation": 4},
    )
    row = next(iter(fake_postgres.pipeline_runtime_states.values()))

    await RuntimeReconciler().bump_generation("pipe-1")

    assert row.desired_generation == 5


@pytest.mark.asyncio
async def test_updating_a_pipeline_bumps_its_runtime_generation(fake_postgres, monkeypatch) -> None:
    from app.schemas.pipeline import PipelineUpdate
    from tests.test_pipeline_update import _create_pipeline, _seed_org

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    template, connection = _seed_org(fake_postgres, "org_gap08")
    created = await _create_pipeline("org_gap08", template, connection)
    fake_postgres.create(
        "pipeline_runtime_states",
        {"pipeline_id": created["id"], "org_id": "org_gap08", "desired_generation": 3},
    )
    state = next(iter(fake_postgres.pipeline_runtime_states.values()))

    await pipeline_service.update_pipeline("org_gap08", created["id"], PipelineUpdate(description="edited"))

    assert state.desired_generation == 4


@pytest.mark.asyncio
async def test_two_simultaneous_first_writes_retry_instead_of_failing(monkeypatch) -> None:
    """Two requests creating the first runtime row for one pipeline race on its unique key; the loser retries and
    takes the update path."""
    attempts = []

    async def _once(self, *args):
        attempts.append(1)
        if len(attempts) == 1:
            raise IntegrityError("insert", {}, Exception("duplicate pipeline_id"))

    monkeypatch.setattr(RuntimeReconciler, "_record_desired_once", _once)

    await RuntimeReconciler().record_desired("org-1", "pipe-1", "conn-1", "deployed", "h")

    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_record_desired_reads_the_row_under_a_lock(fake_postgres, monkeypatch) -> None:
    """Two simultaneous edits must queue (each deriving its generation from the other's committed result) rather than
    both reading N and both writing N+1."""
    seen = []
    make_repo = reconciler_module.PipelineRuntimeStateRepository  # FakePostgres installs a factory here

    def _spying_repo(session):
        repo = make_repo(session)
        real_get = repo.get_by_pipeline_id

        async def _spy(pipeline_id, *, for_update=False):
            seen.append(for_update)
            return await real_get(pipeline_id, for_update=for_update)

        repo.get_by_pipeline_id = _spy
        return repo

    monkeypatch.setattr(reconciler_module, "PipelineRuntimeStateRepository", _spying_repo)

    await RuntimeReconciler().record_desired("org-1", "pipe-1", "conn-1", "deployed", "h1")
    await RuntimeReconciler().record_desired("org-1", "pipe-1", "conn-1", "deployed", "h2")

    assert seen == [True, True]
    state = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert state.desired_generation == 2  # created at 1, then the changed hash bumped it once


# ── A. Ownership handoff and a tick that outlives its lease ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tick_that_lost_its_lease_before_acting_does_nothing(monkeypatch) -> None:
    _, _, row, result = await _tick(monkeypatch, script=[(1, False)], deploy=_applied)

    assert SENT_TO_CONNECT == []  # not a single call reached Connect
    assert result["skipped"] == "lease_lost"
    assert row.last_reconciled_at is None  # nothing written
    assert row.config_hash == "hash-old"


@pytest.mark.asyncio
async def test_a_tick_that_loses_its_lease_during_the_work_discards_its_result(monkeypatch) -> None:
    """A is mid-deploy when B takes the row over: A's finished result must not be written over B's."""
    # pre-action check passes (still owner); the final fence says another process owns it now.
    _, _, row, result = await _tick(monkeypatch, script=[(1, True), (1, False)], deploy=_applied)

    assert SENT_TO_CONNECT == ["conn-1"]  # the work was done, but its result is thrown away
    assert result["skipped"] == "lease_lost"
    assert row.last_reconciled_at is None
    assert row.observed_generation == 0
    assert row.config_hash == "hash-old"


@pytest.mark.asyncio
async def test_a_lapsed_but_untaken_lease_is_still_ours_to_act_on(monkeypatch) -> None:
    """Ownership is who holds the lease, not whether its clock ran out: if nobody took the row over, acting is safe."""
    _, _, row, result = await _tick(monkeypatch, script=[(1, True)], deploy=_applied)

    assert "skipped" not in result
    assert row.last_reconciled_at is not None
    assert row.config_hash == "hash-new"


def _patch_connect(monkeypatch, handled: list[str]):
    async def _list(client):
        return []

    monkeypatch.setattr(reconciler_module, "list_connectors_retryable", _list)

    async def _reconcile_row(self, session, repo, client, existing, row):
        handled.append((self._worker_id, row.pipeline_id))
        return {"pipeline_id": row.pipeline_id}

    monkeypatch.setattr(RuntimeReconciler, "_reconcile_row", _reconcile_row)


def _state(fake_postgres, pipeline_id):
    fake_postgres.create("pipeline_runtime_states", {"pipeline_id": pipeline_id, "org_id": "org-1"})
    return next(r for r in fake_postgres.pipeline_runtime_states.values() if r.pipeline_id == pipeline_id)


@pytest.mark.asyncio
async def test_process_a_dies_the_lease_lapses_and_process_b_reconciles_the_pipeline(fake_postgres, monkeypatch) -> None:
    """The ticket's diagram end to end: A owns -> A dies -> lease expires -> B claims -> B reconciles."""
    handled: list = []
    _patch_connect(monkeypatch, handled)
    row = _state(fake_postgres, "pipe-1")
    process_a = RuntimeReconciler(client_factory=lambda: None, worker_id="process-a")
    process_b = RuntimeReconciler(client_factory=lambda: None, worker_id="process-b")

    await process_a.reconcile_once()
    assert handled == [("process-a", "pipe-1")]

    # While A's lease is valid, B must not touch the pipeline.
    await process_b.reconcile_once()
    assert handled == [("process-a", "pipe-1")]

    # A dies: no more ticks, its lease lapses.
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await process_b.reconcile_once()

    assert handled == [("process-a", "pipe-1"), ("process-b", "pipe-1")]
    assert row.leased_by == "process-b"


@pytest.mark.asyncio
async def test_a_row_taken_over_mid_batch_is_skipped_by_the_slow_process(fake_postgres, monkeypatch) -> None:
    """A claims three rows at the start of its tick but works them one at a time. If B takes the third over while A is
    busy with the first two, A must leave it alone."""
    handled: list = []
    _patch_connect(monkeypatch, handled)
    rows = [_state(fake_postgres, f"pipe-{i}") for i in (1, 2, 3)]
    process_a = RuntimeReconciler(client_factory=lambda: None, worker_id="process-a")

    async def _slow(self, session, repo, client, existing, row):
        handled.append(row.pipeline_id)
        if row.pipeline_id == "pipe-1":  # while A works the first row, A's lease on the last row lapses and B takes it
            rows[2].leased_by = "process-b"
        return {"pipeline_id": row.pipeline_id}

    monkeypatch.setattr(RuntimeReconciler, "_reconcile_row", _slow)

    await process_a.reconcile_once()

    assert "pipe-1" in handled
    assert "pipe-2" in handled
    assert "pipe-3" not in handled
    assert rows[2].leased_by == "process-b"


@pytest.mark.asyncio
async def test_each_row_is_given_a_fresh_lease_just_before_it_is_worked(fake_postgres, monkeypatch) -> None:
    handled: list = []
    _patch_connect(monkeypatch, handled)
    row = _state(fake_postgres, "pipe-1")
    process_a = RuntimeReconciler(client_factory=lambda: None, worker_id="process-a")
    await process_a._claim_rows()
    short = datetime.now(UTC) + timedelta(seconds=1)
    row.lease_expires_at = short

    assert await process_a._renew_lease(row.id) is True
    assert row.lease_expires_at > short + timedelta(seconds=settings.runtime_reconciler_lease_seconds / 2)


@pytest.mark.asyncio
async def test_renewing_a_lease_another_process_took_fails(fake_postgres) -> None:
    row = _state(fake_postgres, "pipe-1")
    row.leased_by = "process-b"

    assert await RuntimeReconciler(worker_id="process-a")._renew_lease(row.id) is False
