"""
GAP-09: "The reconciler should always converge to the latest desired generation" — exercised end to end.

tests/test_runtime_reconciler_convergence.py checks each named race one tick at a time, on hand-set generations. This
file drives the real pieces together instead: lifecycle calls go through `record_desired` (which assigns the
generation), pipeline and transform-policy edits go through the same generation bump the services use, and the real
`reconcile_once` loop (claim, renew, fence, per-row commit) runs against a small STATEFUL stand-in for Kafka Connect
that actually holds connectors, their config and their state. Each test then asserts where the system ends up, not
which calls were made.

The stand-in is deliberately simple: PUT config on an existing connector keeps its paused/running state (as Connect
does), pause/resume/restart/delete change state, and any call raises while the worker is "unavailable".
"""

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

import app.services.runtime_reconciler as reconciler_module
from app.services import pipeline_service as pipeline_service_module
from app.services.runtime_reconciler import RuntimeReconciler
from app.services.transform_service import transform_service

ORG = "org_1"
CONNECTOR = "conn-1"


@dataclass
class World:
    """Kafka Connect plus the pipeline's current spec."""

    connectors: dict[str, dict[str, str]] = field(default_factory=dict)  # name -> {"hash", "state"}
    available: bool = True
    fail_next_delete: int = 0
    spec_hash: str = "h1"
    pipeline_exists: bool = True
    pipeline_id: str = "pipe-1"
    log: list[tuple[str, str]] = field(default_factory=list)
    during_deploy: Any = None  # callable run after the deploy's freshness check passed, before it is applied

    def check(self) -> None:
        if not self.available:
            raise RuntimeError("Kafka Connect unavailable")


def _status(state: str) -> dict:
    return {"connector": {"state": state}, "tasks": [{"state": state}]}


@pytest.fixture
def world(fake_postgres, monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)

    async def get_pipeline(org_id, pipeline_id):
        if not w.pipeline_exists:
            return None
        return {
            "id": w.pipeline_id, "org_id": ORG, "domain": "sales", "source_system": "pg", "entity": "orders",
            "version": "v1", "status": "running", "error_handling_policy": "DLQ_AND_CONTINUE",
        }

    async def set_status(org_id, pipeline_id, status):
        return {}

    async def hard_delete(org_id, pipeline_id):
        w.pipeline_exists = False

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", set_status)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "hard_delete", hard_delete)

    async def resolve_config(pipeline):
        return {"marker": w.spec_hash}, w.spec_hash

    async def list_connectors(client):
        w.check()
        return list(w.connectors)

    async def get_status(client, name):
        w.check()
        return _status(w.connectors[name]["state"])

    async def deploy(client, name, config, *, still_current):
        if not await still_current():
            return None
        w.check()
        if w.during_deploy:
            hook, w.during_deploy = w.during_deploy, None
            await hook()  # a user edit landing while this deploy is in flight
        existing = w.connectors.get(name)
        w.connectors[name] = {"hash": config["marker"], "state": existing["state"] if existing else "RUNNING"}
        w.log.append(("deploy", config["marker"]))
        return {}

    async def pause(client, name):
        w.check()
        w.connectors[name]["state"] = "PAUSED"
        w.log.append(("pause", name))

    async def resume(client, name):
        w.check()
        w.connectors[name]["state"] = "RUNNING"
        w.log.append(("resume", name))

    async def restart(client, name):
        w.check()
        w.connectors[name]["state"] = "RUNNING"
        w.log.append(("restart", name))

    async def delete(client, name):
        w.check()
        if w.fail_next_delete:
            w.fail_next_delete -= 1
            raise RuntimeError("Kafka Connect dropped the delete request")
        w.connectors.pop(name, None)
        w.log.append(("delete", name))

    for attr, fn in {
        "_resolve_config": resolve_config, "list_connectors_retryable": list_connectors,
        "get_status_retryable": get_status, "deploy_if_current": deploy, "pause_if_needed": pause,
        "resume_if_needed": resume, "restart_once": restart, "delete_converged": delete,
    }.items():
        monkeypatch.setattr(reconciler_module, attr, fn)
    return w


@pytest.fixture
def reconciler() -> RuntimeReconciler:
    return RuntimeReconciler(client_factory=lambda: None, worker_id="process-a")


# ── what a lifecycle call or an edit does, as the services do it ──────────────────────────────────────────────────


async def deploy(r: RuntimeReconciler, w: World) -> None:
    await r.record_desired(ORG, w.pipeline_id, CONNECTOR, "deployed", w.spec_hash)


async def pause(r: RuntimeReconciler, w: World) -> None:
    await r.record_desired(ORG, w.pipeline_id, CONNECTOR, "paused")


async def resume(r: RuntimeReconciler, w: World) -> None:
    await r.record_desired(ORG, w.pipeline_id, CONNECTOR, "running")


async def delete(r: RuntimeReconciler, w: World) -> None:
    await r.record_desired(ORG, w.pipeline_id, CONNECTOR, "deleted")


async def edit_config(r: RuntimeReconciler, w: World, new_hash: str) -> None:
    """PipelineService.update_pipeline: the stored spec changes, then the desired generation is bumped."""
    w.spec_hash = new_hash
    await r.bump_generation(w.pipeline_id)


def state_row(fake_postgres, pipeline_id: str | None = None):
    """The runtime-state row for `pipeline_id` (or the only/first one)."""
    rows = fake_postgres.pipeline_runtime_states.values()
    return next((r for r in rows if pipeline_id is None or r.pipeline_id == pipeline_id), None)


_OBSERVED_FOR = {"deployed": "running", "running": "running", "paused": "paused"}


async def converge(
    r: RuntimeReconciler, fake_postgres, *, max_ticks: int = 8, pipeline_id: str | None = None,
) -> int:
    """Tick until the row is genuinely settled on its latest intent — generations equal, no error, AND the observed
    state is the one the desired state asks for (observed_generation alone can lag a drift after an earlier
    convergence) — or the row is gone. Returns the ticks used."""
    for tick in range(1, max_ticks + 1):
        await r.reconcile_once()
        row = state_row(fake_postgres, pipeline_id)
        if row is None:
            return tick
        if (
            row.observed_generation == row.desired_generation
            and row.last_runtime_error is None
            and row.observed_state == _OBSERVED_FOR.get(row.desired_state)
        ):
            return tick
    raise AssertionError(f"did not converge in {max_ticks} ticks: {vars(state_row(fake_postgres, pipeline_id))}")


# ── the five named races ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_then_immediate_pause_ends_paused_on_the_latest_generation(world, reconciler, fake_postgres) -> None:
    await deploy(reconciler, world)
    await pause(reconciler, world)  # both recorded before any tick ran

    await converge(reconciler, fake_postgres)

    assert world.connectors[CONNECTOR] == {"hash": "h1", "state": "PAUSED"}
    row = state_row(fake_postgres)
    assert row.desired_generation == 2
    assert row.observed_generation == 2
    assert [name for name, _ in world.log].count("deploy") == 1


@pytest.mark.asyncio
async def test_pause_then_immediate_resume_never_touches_the_running_connector(world, reconciler, fake_postgres) -> None:
    await deploy(reconciler, world)
    await converge(reconciler, fake_postgres)
    world.log.clear()

    await pause(reconciler, world)
    await resume(reconciler, world)
    await converge(reconciler, fake_postgres)

    assert world.log == []  # the intermediate "paused" intent was superseded before it was ever acted on
    assert world.connectors[CONNECTOR]["state"] == "RUNNING"
    row = state_row(fake_postgres)
    assert row.desired_generation == 3
    assert row.observed_generation == 3


@pytest.mark.asyncio
async def test_deploy_then_edit_config_ends_on_the_edited_config(world, reconciler, fake_postgres) -> None:
    await deploy(reconciler, world)
    await converge(reconciler, fake_postgres)

    await edit_config(reconciler, world, "h2")
    await converge(reconciler, fake_postgres)

    assert world.connectors[CONNECTOR] == {"hash": "h2", "state": "RUNNING"}
    row = state_row(fake_postgres)
    assert row.config_hash == "h2"
    assert row.observed_generation == row.desired_generation == 2


@pytest.mark.asyncio
async def test_an_edit_landing_while_a_deploy_is_in_flight_is_applied_by_the_next_tick(
    world, reconciler, fake_postgres,
) -> None:
    """The tick has already decided to send h1 when the user's edit (h2) is recorded. That h1 reaches Connect, but the
    tick must not claim convergence — the next tick sends h2."""
    await deploy(reconciler, world)

    async def _edit_lands():
        await edit_config(reconciler, world, "h2")

    world.during_deploy = _edit_lands

    await reconciler.reconcile_once()

    row = state_row(fake_postgres)
    assert world.connectors[CONNECTOR]["hash"] == "h1"  # the in-flight deploy went out with the old spec
    assert row.observed_generation != row.desired_generation  # ...and the tick did not claim convergence

    await converge(reconciler, fake_postgres)

    assert world.connectors[CONNECTOR]["hash"] == "h2"
    assert row.observed_generation == row.desired_generation


@pytest.mark.asyncio
async def test_restart_then_edit_transform_ends_on_the_new_transform_and_marks_only_its_users(
    world, reconciler, fake_postgres,
) -> None:
    """Editing a transform policy goes through the real TransformPolicyService: it must bump the generation of every
    pipeline that applies the policy (and only those), and the connector must end on the edited transforms."""
    policy = fake_postgres.create("transform_policies", {"org_id": ORG, "name": "mask", "transforms": []})
    mine = fake_postgres.create("pipelines", {"org_id": ORG, "transform_policy_id": policy["id"]})
    other = fake_postgres.create("pipelines", {"org_id": ORG, "transform_policy_id": "some-other-policy"})
    world.pipeline_id = mine["id"]
    fake_postgres.create(
        "pipeline_runtime_states",
        {"org_id": ORG, "pipeline_id": other["id"], "connector_name": "other", "desired_state": "running",
         "desired_generation": 1, "leased_by": "someone-else"},
    )
    await deploy(reconciler, world)
    await converge(reconciler, fake_postgres, pipeline_id=mine["id"])
    world.connectors[CONNECTOR]["state"] = "FAILED"  # the connector was restarted because it had failed...
    await resume(reconciler, world)  # ...which records "running" again (no generation change)
    before = {r.pipeline_id: r.desired_generation for r in fake_postgres.pipeline_runtime_states.values()}

    world.spec_hash = "h-transform-v2"  # what the edited policy does to the generated config
    await transform_service.update_policy(ORG, policy["id"], {"transforms": [{"type": "mask"}]})
    await converge(reconciler, fake_postgres, pipeline_id=mine["id"])

    after = {r.pipeline_id: r.desired_generation for r in fake_postgres.pipeline_runtime_states.values()}
    assert after[mine["id"]] == before[mine["id"]] + 1
    assert after[other["id"]] == before[other["id"]]  # a pipeline on a different policy is not marked
    assert world.connectors[CONNECTOR] == {"hash": "h-transform-v2", "state": "RUNNING"}


@pytest.mark.asyncio
async def test_delete_while_connect_is_down_waits_and_completes_when_it_returns(world, reconciler, fake_postgres) -> None:
    await deploy(reconciler, world)
    await converge(reconciler, fake_postgres)

    await delete(reconciler, world)
    world.available = False
    for _ in range(3):
        await reconciler.reconcile_once()  # the whole tick gives up at the connector listing

    row = state_row(fake_postgres)
    assert row is not None
    assert row.desired_state == "deleted"  # the intent is still on record
    assert world.pipeline_exists  # nothing was half-deleted
    assert CONNECTOR in world.connectors

    world.available = True
    await converge(reconciler, fake_postgres)

    assert CONNECTOR not in world.connectors
    assert not world.pipeline_exists
    assert state_row(fake_postgres) is None


@pytest.mark.asyncio
async def test_a_delete_call_that_fails_leaves_the_error_visible_and_is_retried(world, reconciler, fake_postgres) -> None:
    await deploy(reconciler, world)
    await converge(reconciler, fake_postgres)

    await delete(reconciler, world)
    world.fail_next_delete = 2
    await reconciler.reconcile_once()

    row = state_row(fake_postgres)
    assert "OPERATION_FAILED" in row.last_runtime_error  # the stored text is the SafeError's...
    assert "dropped the delete" not in row.last_runtime_error  # ...not the raw exception
    assert world.pipeline_exists
    assert CONNECTOR in world.connectors

    await reconciler.reconcile_once()
    await converge(reconciler, fake_postgres)

    assert CONNECTOR not in world.connectors
    assert not world.pipeline_exists
    assert state_row(fake_postgres) is None


@pytest.mark.asyncio
async def test_connect_losing_the_connector_is_recreated_in_the_state_the_latest_intent_asks_for(
    world, reconciler, fake_postgres,
) -> None:
    await deploy(reconciler, world)
    await pause(reconciler, world)
    await converge(reconciler, fake_postgres)
    world.connectors.clear()  # the worker lost it (restart with an empty config topic)

    await converge(reconciler, fake_postgres)

    assert world.connectors[CONNECTOR] == {"hash": "h1", "state": "PAUSED"}


# ── the property: any sequence of lifecycle calls and edits ends on the latest intent ────────────────────────────


OPERATIONS = ("pause", "resume", "edit", "tick")


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(40))
async def test_any_sequence_of_calls_and_edits_converges_to_the_last_intent(
    seed, world, reconciler, fake_postgres,
) -> None:
    rng = random.Random(seed)
    await deploy(reconciler, world)
    desired = "running"
    edits = 0

    for _ in range(rng.randint(3, 12)):
        operation = rng.choice(OPERATIONS)
        if operation == "pause":
            await pause(reconciler, world)
            desired = "paused"
        elif operation == "resume":
            await resume(reconciler, world)
            desired = "running"
        elif operation == "edit":
            edits += 1
            await edit_config(reconciler, world, f"h-edit-{edits}")
        else:
            await reconciler.reconcile_once()  # a tick lands somewhere in the middle of the sequence

    await converge(reconciler, fake_postgres)

    connector = world.connectors[CONNECTOR]
    assert connector["hash"] == world.spec_hash, f"seed {seed}: stuck on a superseded config"
    assert connector["state"] == ("PAUSED" if desired == "paused" else "RUNNING"), f"seed {seed}: wrong final state"
    row = state_row(fake_postgres)
    assert row.observed_generation == row.desired_generation, f"seed {seed}: generation not converged"


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(20))
async def test_a_sequence_ending_in_delete_removes_everything(seed, world, reconciler, fake_postgres) -> None:
    rng = random.Random(1000 + seed)
    await deploy(reconciler, world)
    for _ in range(rng.randint(1, 8)):
        operation = rng.choice(OPERATIONS)
        if operation == "pause":
            await pause(reconciler, world)
        elif operation == "resume":
            await resume(reconciler, world)
        elif operation == "edit":
            await edit_config(reconciler, world, f"h-{rng.randint(0, 999)}")
        else:
            await reconciler.reconcile_once()
    await delete(reconciler, world)

    await converge(reconciler, fake_postgres)

    assert CONNECTOR not in world.connectors
    assert not world.pipeline_exists
    assert state_row(fake_postgres) is None
