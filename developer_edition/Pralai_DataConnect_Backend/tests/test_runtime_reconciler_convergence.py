"""
GAP-09: explicit convergence tests for 5 named desired/observed generation
races, on top of GAP-08's staleness guard and the pre-existing config_hash
convergence mechanism. "The reconciler should always converge to the
latest desired generation."

Two convergence mechanisms are in play, confirmed by reading the real call
sites (not assumed):
  - desired_generation: bumped by record_desired on a desired_state or
    config-hash change (deploy/pause/resume/delete), and — since GAP-08/09 —
    by RuntimeReconciler.bump_generation on a pipeline edit
    (PipelineService.update_pipeline) and on a transform-policy edit (once for
    every pipeline that applies it). Not bumped by restart on an
    already-"running" pipeline: desired_state doesn't change. The bump is what
    lets a tick already in flight see itself as stale.
  - config_hash (recomputed fresh from the live pipeline/transform_policy/
    connection every single tick by _resolve_config, independent of
    desired_generation) — what actually makes the next tick redeploy an edit.
Scenarios 1/2 are desired_generation races; 3/4 are config_hash races (4
starting from a restart); 5 is a durability/idempotency race against a
failing Kafka Connect call. These check ONE tick on hand-set generations; the
end-to-end version (real record_desired, real bumps, reconcile_once against
a stateful Connect stand-in, and randomised sequences) is
tests/test_runtime_convergence_sequences.py.

Builds the same kind of hand-rolled fakes as
tests/test_runtime_reconciler_generation_race.py (GAP-08) — _reconcile_row
still isn't exercised by any FakePostgres-backed fixture, for the same
reason noted there (its generic _FakeSession.scalar() always returns None,
which can't represent "the live desired_generation is still N").
"""

from types import SimpleNamespace

import pytest

import app.services.runtime_reconciler as reconciler_module
from app.services import pipeline_service as pipeline_service_module


@pytest.fixture(autouse=True)
def _no_plugin_preflight(monkeypatch):
    """These tests are about desired/observed GENERATION and config-hash convergence, driven by a hand-rolled fake
    Connect client that has no plugin catalog. Plugin preflight in the reconciler has its own tests
    (tests/test_reconciler_preflight.py)."""
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)


PIPELINE = {
    "id": "pipe-1", "org_id": "org-1", "domain": "sales", "source_system": "pg",
    "entity": "orders", "version": "v1", "status": "running",
    "error_handling_policy": "DLQ_AND_CONTINUE",
}


class _FakeSession:
    def __init__(self, live_desired_generation: int):
        self.live_desired_generation = live_desired_generation

    async def scalar(self, *args, **kwargs):
        return self.live_desired_generation

    async def flush(self) -> None:
        pass


class _FakeRepo:
    def __init__(self, session=None):
        self.session = session
        self.deleted: list = []

    async def delete(self, row) -> None:
        self.deleted.append(row)

    async def fence(self, row_id, worker_id, *, lock=False):
        # GAP-08: (live desired_generation, still the lease holder) — the session double carries the "live" value.
        return self.session.live_desired_generation, True


def _row(desired_state: str, desired_generation: int, *, observed_generation: int = 0, config_hash: str = "hash-1"):
    return SimpleNamespace(
        id="row-1", org_id="org-1", pipeline_id="pipe-1", connector_name="conn-1",
        desired_state=desired_state, desired_generation=desired_generation,
        config_hash=config_hash, observed_state="unknown", observed_generation=observed_generation,
        last_reconciled_at=None, last_runtime_error=None,
    )


async def _status(state: str | None) -> dict | None:
    if state is None:
        return None
    return {"connector": {"state": state}, "tasks": [{"state": state}]}


def _install_fakes(monkeypatch, *, status_sequence: list[str | None], resolved_config_hash: str = "hash-1"):
    """Fakes every I/O dependency _reconcile_row touches. `status_sequence`
    feeds get_status_retryable one value per call in order (repeating the
    last once exhausted; use None for "connector doesn't exist" style
    unreachable responses only where the caller already skips the call).
    Returns a dict of call-tracking lists plus the fake deploy's captured
    config, so a test can assert on exactly what would have been sent to
    Kafka Connect."""
    calls: dict[str, list] = {"delete": [], "pause": [], "resume": [], "restart": [], "deploy": []}
    remaining = list(status_sequence)
    deployed_configs: list[dict] = []

    async def _fake_get_pipeline(org_id, pipeline_id):
        return dict(PIPELINE)

    async def _fake_set_status(org_id, pipeline_id, status):
        pass

    async def _fake_hard_delete(org_id, pipeline_id):
        calls.setdefault("hard_delete", []).append(pipeline_id)

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _fake_get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", _fake_set_status)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "hard_delete", _fake_hard_delete)

    async def _fake_resolve_config(pipeline):
        return {"connector.class": "x", "config_hash_marker": resolved_config_hash}, resolved_config_hash

    monkeypatch.setattr(reconciler_module, "_resolve_config", _fake_resolve_config)

    async def _fake_get_status_retryable(client, connector_name):
        state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return await _status(state)

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _fake_get_status_retryable)

    async def _fake_delete_converged(client, connector_name):
        calls["delete"].append(connector_name)

    async def _fake_pause_if_needed(client, connector_name):
        calls["pause"].append(connector_name)

    async def _fake_resume_if_needed(client, connector_name):
        calls["resume"].append(connector_name)

    async def _fake_restart_once(client, connector_name):
        calls["restart"].append(connector_name)

    async def _fake_deploy_if_current(client, connector_name, config, *, still_current):
        if await still_current():
            calls["deploy"].append(connector_name)
            deployed_configs.append(config)
            return {"config": config}
        return None

    monkeypatch.setattr(reconciler_module, "delete_converged", _fake_delete_converged)
    monkeypatch.setattr(reconciler_module, "pause_if_needed", _fake_pause_if_needed)
    monkeypatch.setattr(reconciler_module, "resume_if_needed", _fake_resume_if_needed)
    monkeypatch.setattr(reconciler_module, "restart_once", _fake_restart_once)
    monkeypatch.setattr(reconciler_module, "deploy_if_current", _fake_deploy_if_current)

    return calls, deployed_configs


# ── 1. Deploy → immediate Pause ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_then_immediate_pause_converges_to_paused_at_the_latest_generation(monkeypatch) -> None:
    """Two record_desired calls land before any tick ever runs: deploy
    (gen 1) then pause (gen 2). The connector was never actually created,
    so plan_actions must deploy first (Kafka Connect has no "create
    paused" option) and only pause on a later tick — but it must end up
    converged on desired_generation 2, the LATEST intent, never getting
    stuck believing gen 1 (deploy) was the end state."""
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
    row = _row(desired_state="paused", desired_generation=2)  # already the latest by the time any tick runs
    session = _FakeSession(live_desired_generation=2)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    # Tick 1: connector doesn't exist yet -> deploys first (comment in
    # plan_actions: "apply the config first; state actions follow on a
    # later tick").
    result_1 = await reconciler._reconcile_row(session, repo, client=None, existing=set(), row=row)
    assert calls["deploy"] == ["conn-1"]
    assert calls["pause"] == []
    assert row.observed_generation == 0  # not converged yet — desired is "paused", observed came back "running"
    assert result_1["observed_state"] == "running"

    # Tick 2: connector now exists (as it would in `existing` on the
    # reconciler's next real pass); Connect still reports it running at the
    # start of the tick (nothing paused it yet) -> plan_actions issues the
    # pause -> the post-action re-check reports it actually paused.
    calls2, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING", "PAUSED"])
    result_2 = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)
    assert calls2["pause"] == ["conn-1"]
    assert row.observed_generation == 2  # converged to the LATEST generation, not the deploy's gen 1
    assert result_2["observed_state"] == "paused"


# ── 2. Pause → immediate Resume ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_then_immediate_resume_skips_the_intermediate_state_entirely(monkeypatch) -> None:
    """Pause (gen 2) then resume (gen 3) both land before any tick — on an
    already-running, already-converged pipeline (gen 1). No pause was ever
    actually issued to Kafka Connect (it's still running the whole time),
    so the reconciler must NOT call pause_if_needed at all: it only ever
    sees the LATEST desired_state ("running") against the observed state
    ("running"), recognizes it's already converged, and jumps straight to
    stamping observed_generation at the latest generation (3) — never
    passing through 2."""
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
    row = _row(desired_state="running", desired_generation=3, observed_generation=1)
    session = _FakeSession(live_desired_generation=3)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["pause"] == []
    assert calls["resume"] == []
    assert row.observed_generation == 3  # jumped straight to the latest, skipping gen 2 entirely
    assert result["observed_state"] == "running"


# ── 3. Deploy → edit config ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_then_edit_config_redeploys_with_the_edited_config(monkeypatch) -> None:
    """A config/connector_config edit (PipelineService.update_pipeline)
    never calls record_desired — desired_generation stays put. Drift is
    caught purely by _resolve_config's fresh config_hash every tick. This
    proves that mechanism converges to the EDITED config, not the one that
    was live when desired_generation was last bumped."""
    calls, deployed_configs = _install_fakes(
        monkeypatch, status_sequence=["RUNNING"], resolved_config_hash="hash-after-edit"
    )
    # Already deployed & converged with the OLD config hash; desired_generation
    # never moved even though the pipeline's config was since edited.
    row = _row(desired_state="running", desired_generation=1, observed_generation=1, config_hash="hash-before-edit")
    session = _FakeSession(live_desired_generation=1)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["deploy"] == ["conn-1"]
    assert deployed_configs[0]["config_hash_marker"] == "hash-after-edit"  # the edited config, not the stale one
    assert row.config_hash == "hash-after-edit"
    assert result["desired_state"] == "running"


# ── 4. Restart → edit transform ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_then_edit_transform_still_redeploys_with_the_new_transform(monkeypatch) -> None:
    """A restart on an already-"running" pipeline doesn't bump
    desired_generation at all (desired_state doesn't change, and restart
    passes no config_hash to record_desired) — so this specifically proves
    convergence to an edited TransformPolicy does NOT depend on any
    generation bump ever happening: it's caught purely by the same
    config_hash mechanism as scenario 3, exercised here from the
    "just restarted" starting state rather than "just deployed"."""
    calls, deployed_configs = _install_fakes(
        monkeypatch, status_sequence=["RUNNING"], resolved_config_hash="hash-after-transform-edit"
    )
    row = _row(
        desired_state="running", desired_generation=1, observed_generation=1,
        config_hash="hash-before-transform-edit",
    )
    session = _FakeSession(live_desired_generation=1)  # restart never bumped this
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["deploy"] == ["conn-1"]
    assert deployed_configs[0]["config_hash_marker"] == "hash-after-transform-edit"
    assert row.config_hash == "hash-after-transform-edit"
    assert result["desired_state"] == "running"


# ── 5. Delete → Connect unavailable ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_survives_connect_unavailable_and_completes_once_it_recovers(monkeypatch) -> None:
    """record_desired("deleted", ...) is written durably before Connect is
    ever contacted (PipelineService.delete_pipeline). If Connect is
    unreachable, delete_converged's retries are exhausted and it re-raises
    — that must leave the pipeline and its PipelineRuntimeState row
    completely untouched (never half-deleted), ready for a later tick to
    finish the job once Connect is reachable again."""
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
    row = _row(desired_state="deleted", desired_generation=2)
    session = _FakeSession(live_desired_generation=2)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    async def _failing_delete_converged(client, connector_name):
        raise RuntimeError("Kafka Connect unreachable")

    monkeypatch.setattr(reconciler_module, "delete_converged", _failing_delete_converged)

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert result["error"] is not None
    assert repo.deleted == []  # never removed — the delete intent survives for the next tick

    # Connect recovers — the connector is still physically there (tick 1's
    # delete never actually reached it), but this time delete_converged
    # succeeds, and the reconciler special-cases "just deleted" straight to
    # observed_state="absent" without a further status round-trip.
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
    result_2 = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["delete"] == ["conn-1"]
    assert result_2["error"] is None
    assert result_2["observed_state"] == "absent"
    assert repo.deleted == [row]
