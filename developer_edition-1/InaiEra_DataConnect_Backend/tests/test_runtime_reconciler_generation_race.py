"""
GAP-08, scenario B: "A reconciliation running + user edits pipeline -> new
generation -> old reconciliation result MUST NOT overwrite new desired
state." `deploy_if_current` already re-checked desired_generation before
applying a config (from GAP-04-era work); this fix extends the same
guarantee to delete/pause/resume/restart, which previously had no
staleness check at all — see the "GAP-08" section of
app/services/runtime_reconciler.py's module docstring.

`_reconcile_row` was never exercised end-to-end by any existing test (only
its pure helpers — derive_observed_state, plan_actions, is_converged — were
tested), so this file builds hand-rolled fakes for every dependency it
touches rather than extending FakePostgres, whose generic `_FakeSession`
always returns None from `scalar()` and so can't represent "the live
desired_generation is still N" vs "it's now N+1" the way this scenario
needs.
"""

from types import SimpleNamespace

import pytest

import app.services.runtime_reconciler as reconciler_module
from app.services import pipeline_service as pipeline_service_module

PIPELINE = {
    "id": "pipe-1", "org_id": "org-1", "domain": "sales", "source_system": "pg",
    "entity": "orders", "version": "v1", "status": "running",
    "error_handling_policy": "DLQ_AND_CONTINUE",
    # No template_id: skips the metrics-only connector_type lookup at the
    # end of _reconcile_row, which needs its own catalog_service fake this
    # test has no other reason to set up.
}


class _FakeSession:
    """Only what _reconcile_row calls on `session`: scalar() (the live
    desired_generation re-check `_still_current` does) and flush()."""

    def __init__(self, live_desired_generation: int):
        self.live_desired_generation = live_desired_generation
        self.flushed = False

    async def scalar(self, *args, **kwargs):
        return self.live_desired_generation

    async def flush(self) -> None:
        self.flushed = True


class _FakeRepo:
    def __init__(self, session=None):
        self.session = session
        self.deleted: list = []

    async def delete(self, row) -> None:
        self.deleted.append(row)

    async def fence(self, row_id, worker_id, *, lock=False):
        # GAP-08: (live desired_generation, still the lease holder) — the session double carries the "live" value.
        return self.session.live_desired_generation, True


def _row(desired_state: str, desired_generation: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id="row-1", org_id="org-1", pipeline_id="pipe-1", connector_name="conn-1",
        desired_state=desired_state, desired_generation=desired_generation,
        config_hash="hash-1", observed_state="unknown", observed_generation=0,
        last_reconciled_at=None, last_runtime_error=None,
    )


async def _status(state: str) -> dict:
    return {"connector": {"state": state}, "tasks": [{"state": state}]}


def _install_common_fakes(monkeypatch, *, connect_status_sequence: list[str]) -> dict[str, list]:
    """Fakes every I/O dependency _reconcile_row touches so only the
    action-executing calls (delete/deploy/pause/resume/restart) need to be
    asserted on. Returns a dict of call-tracking lists, one per action.

    `connect_status_sequence` feeds get_status_retryable one value per
    call, in order (repeating the last one once exhausted) — _reconcile_row
    calls it once to observe the CURRENT state before planning actions, and
    again afterward to verify what actually changed, never trusting an
    action call's own response as truth."""
    calls: dict[str, list] = {"delete": [], "pause": [], "resume": [], "restart": [], "deploy": []}
    remaining = list(connect_status_sequence)

    async def _fake_get_pipeline(org_id, pipeline_id):
        return dict(PIPELINE)

    async def _fake_set_status(org_id, pipeline_id, status):
        calls.setdefault("set_status", []).append(status)

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _fake_get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", _fake_set_status)

    async def _fake_resolve_config(pipeline):
        return {"connector.class": "x"}, "hash-1"  # matches row's config_hash -> config_changed False

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
            return {"config": config}
        return None

    monkeypatch.setattr(reconciler_module, "delete_converged", _fake_delete_converged)
    monkeypatch.setattr(reconciler_module, "pause_if_needed", _fake_pause_if_needed)
    monkeypatch.setattr(reconciler_module, "resume_if_needed", _fake_resume_if_needed)
    monkeypatch.setattr(reconciler_module, "restart_once", _fake_restart_once)
    monkeypatch.setattr(reconciler_module, "deploy_if_current", _fake_deploy_if_current)

    return calls


@pytest.mark.asyncio
async def test_stale_generation_skips_pause_and_does_not_stamp_observed_generation(monkeypatch) -> None:
    """A concurrent PATCH (simulated: the DB's live desired_generation is
    already ahead of what this tick planned `actions` against) must make
    _reconcile_row skip acting entirely — it must not pause a connector
    based on intent a newer request has already superseded, and must not
    claim convergence (observed_generation) with the stale generation it
    never actually re-verified."""
    calls = _install_common_fakes(monkeypatch, connect_status_sequence=["RUNNING"])  # desired paused, observed running -> plan_actions wants {"pause": True}
    row = _row(desired_state="paused", desired_generation=5)
    session = _FakeSession(live_desired_generation=6)  # a PATCH already bumped it past what this tick read
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["pause"] == []
    assert row.observed_generation == 0  # untouched — never stamped as converged with the stale generation
    assert result["desired_state"] == "paused"


@pytest.mark.asyncio
async def test_stale_generation_skips_resume(monkeypatch) -> None:
    calls = _install_common_fakes(monkeypatch, connect_status_sequence=["PAUSED"])  # desired running, observed paused -> {"resume": True}
    row = _row(desired_state="running", desired_generation=5)
    session = _FakeSession(live_desired_generation=6)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["resume"] == []
    assert row.observed_generation == 0
    assert result["desired_state"] == "running"


@pytest.mark.asyncio
async def test_stale_generation_skips_restart(monkeypatch) -> None:
    calls = _install_common_fakes(monkeypatch, connect_status_sequence=["FAILED"])  # observed failed -> {"restart": True} (self-heal)
    row = _row(desired_state="running", desired_generation=5)
    session = _FakeSession(live_desired_generation=6)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["restart"] == []
    assert row.observed_generation == 0
    assert result["desired_state"] == "running"


@pytest.mark.asyncio
async def test_stale_generation_skips_delete(monkeypatch) -> None:
    calls = _install_common_fakes(monkeypatch, connect_status_sequence=["RUNNING"])
    row = _row(desired_state="deleted", desired_generation=5)
    session = _FakeSession(live_desired_generation=6)
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["delete"] == []
    assert repo.deleted == []


@pytest.mark.asyncio
async def test_non_stale_generation_still_executes_pause_as_before(monkeypatch) -> None:
    """Regression control: when nothing raced this tick (the live
    desired_generation still matches what was read), the pre-existing
    behavior — act, then stamp observed_generation once converged — must be
    completely unchanged."""
    calls = _install_common_fakes(monkeypatch, connect_status_sequence=["RUNNING", "PAUSED"])
    row = _row(desired_state="paused", desired_generation=5)
    session = _FakeSession(live_desired_generation=5)  # unchanged — matches row.desired_generation
    repo = _FakeRepo(session)
    reconciler = reconciler_module.RuntimeReconciler()

    result = await reconciler._reconcile_row(session, repo, client=None, existing={"conn-1"}, row=row)

    assert calls["pause"] == ["conn-1"]
    assert row.observed_generation == 5  # converged, correctly stamped
    assert result["observed_state"] == "paused"
