"""
FE-BUG-25: Deploy/Pause/Resume/Restart must not let a double-click (or two
browser tabs) create two Operations for the same pipeline. The actual guard
is PipelineService.claim_lifecycle_transition — an atomic conditional UPDATE
run at submission time (app/api/routes/pipelines.py's
_submit_lifecycle_operation), before any Operation row is created — see that
method's own docstring for why a read-then-write inside deploy()/_lifecycle()
themselves (which only persisted the transitional status AFTER a live Kafka
Connect round-trip) wasn't enough.
"""

import asyncio

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient


async def _noop(*args, **kwargs) -> None:
    return None

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}},
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict, **overrides) -> dict:
    payload = PipelineCreate(
        name=overrides.pop("name", "Orders CDC"), template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders",
        mode=overrides.pop("mode", "cdc"), version="v1", **overrides,
    )
    return await pipeline_service.create_pipeline(org_id, payload)


# ── PipelineService.claim_lifecycle_transition — the core guard ───────────

@pytest.mark.asyncio
async def test_claim_succeeds_from_a_valid_source_status_and_sets_both_fields(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_claim_1")
    created = await _create_pipeline("org_claim_1", template, connection)

    claimed = await pipeline_service.claim_lifecycle_transition("org_claim_1", created["id"], "deploying", "op-1")

    assert claimed["status"] == "deploying"
    assert claimed["active_operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_a_second_claim_while_the_first_is_still_active_is_rejected(fake_postgres) -> None:
    """The idempotency guard itself: once claimed, the pipeline's status is
    no longer in the allowed source set for the SAME transition, so a
    second claim — however soon after the first — finds nothing to claim."""
    template, connection = _seed_org(fake_postgres, "org_claim_2")
    created = await _create_pipeline("org_claim_2", template, connection)

    await pipeline_service.claim_lifecycle_transition("org_claim_2", created["id"], "deploying", "op-1")

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.claim_lifecycle_transition("org_claim_2", created["id"], "deploying", "op-2")
    assert exc_info.value.status_code == 409

    # The FIRST claim's operation id is still the one on record — a
    # rejected second claim must never overwrite it.
    current = await pipeline_service.get_or_404("org_claim_2", created["id"])
    assert current["active_operation_id"] == "op-1"


@pytest.mark.asyncio
async def test_claim_is_rejected_for_a_pipeline_in_a_terminal_or_unrelated_status(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_claim_3")
    created = await _create_pipeline("org_claim_3", template, connection)
    fake_postgres.pipelines[created["id"]].status = "deployed"

    # "resuming" is only reachable from "paused" — not "deployed".
    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.claim_lifecycle_transition("org_claim_3", created["id"], "resuming", "op-1")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_deploy_on_a_paused_pipeline_says_resume_not_operation_in_progress(fake_postgres) -> None:
    """Deploy on a paused pipeline used to say "it may already have one in
    progress (target status: 'deploying')" although nothing was running —
    paused just isn't a status Deploy can start from."""
    template, connection = _seed_org(fake_postgres, "org_claim_5")
    created = await _create_pipeline("org_claim_5", template, connection)
    fake_postgres.pipelines[created["id"]].status = "paused"

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.claim_lifecycle_transition("org_claim_5", created["id"], "deploying", "op-1")
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert "Cannot deploy this pipeline while it is 'paused'" in detail
    assert "Resume" in detail
    assert "in progress" not in detail


@pytest.mark.asyncio
async def test_a_claim_during_a_real_operation_still_says_in_progress(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_claim_6")
    created = await _create_pipeline("org_claim_6", template, connection)
    await pipeline_service.claim_lifecycle_transition("org_claim_6", created["id"], "deploying", "op-1")

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.claim_lifecycle_transition("org_claim_6", created["id"], "pausing", "op-2")
    assert "already has one in progress (status: 'deploying')" in exc_info.value.detail


@pytest.mark.parametrize(("current", "claim", "expected"), [
    ("draft", "resuming", "Cannot resume this pipeline while it is 'draft'. Only a paused pipeline can be resumed."),
    ("paused", "pausing", "Cannot pause this pipeline while it is 'paused'."),
    ("running", "deployed", "Cannot deploy this pipeline while it is 'running'."),
    ("restarting", "restarting", "already has one in progress (status: 'restarting')"),
])
def test_claim_conflict_detail_names_the_action_and_status(current, claim, expected) -> None:
    from app.services.pipeline_service import _claim_conflict_detail
    assert expected in _claim_conflict_detail(current, claim)


@pytest.mark.asyncio
async def test_claiming_a_different_pipeline_is_unaffected_by_another_ones_claim(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_claim_4")
    first = await _create_pipeline("org_claim_4", template, connection, name="A")
    second = await _create_pipeline("org_claim_4", template, connection, name="B")

    await pipeline_service.claim_lifecycle_transition("org_claim_4", first["id"], "deploying", "op-1")
    claimed_second = await pipeline_service.claim_lifecycle_transition("org_claim_4", second["id"], "deploying", "op-2")

    assert claimed_second["status"] == "deploying"
    assert claimed_second["active_operation_id"] == "op-2"


@pytest.mark.asyncio
async def test_active_operation_id_is_cleared_once_status_settles_to_a_stable_value(fake_postgres) -> None:
    """The other half of the contract: active_operation_id must not linger
    forever once the operation genuinely finishes — _persist_pipeline_fields
    clears it automatically whenever `status` moves to anything outside
    TRANSITIONAL_STATUSES, without deploy()/_lifecycle()/the reconciler's
    set_status needing to remember to do it themselves."""
    template, connection = _seed_org(fake_postgres, "org_claim_5")
    created = await _create_pipeline("org_claim_5", template, connection)
    await pipeline_service.claim_lifecycle_transition("org_claim_5", created["id"], "deploying", "op-1")

    updated = await pipeline_service.set_status("org_claim_5", created["id"], "running")

    assert updated["status"] == "running"
    assert updated["active_operation_id"] is None


@pytest.mark.asyncio
async def test_active_operation_id_survives_a_redundant_same_status_write(fake_postgres) -> None:
    """deploy()/_lifecycle() themselves still redundantly re-persist the
    transitional status at the end of their own run (for generated_config,
    in deploy()'s case) — that write must not accidentally clear the very
    field it just set."""
    template, connection = _seed_org(fake_postgres, "org_claim_6")
    created = await _create_pipeline("org_claim_6", template, connection)
    await pipeline_service.claim_lifecycle_transition("org_claim_6", created["id"], "deploying", "op-1")

    updated = await pipeline_service._persist_pipeline_fields("org_claim_6", created["id"], {"status": "deploying"})

    assert updated["active_operation_id"] == "op-1"


# ── status_to_claim_for / _assert_transition's narrow allow_self ──────────

@pytest.mark.asyncio
async def test_deploy_succeeds_when_the_pipeline_was_already_claimed_for_it(fake_postgres, monkeypatch) -> None:
    """The other end of the fix: once claimed, deploy() itself must not
    reject its own (now redundant) _assert_transition check just because
    the pipeline is already sitting at the status it's "transitioning" to."""
    template, connection = _seed_org(fake_postgres, "org_claim_7")
    created = await _create_pipeline("org_claim_7", template, connection)
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop)
    monkeypatch.setattr("app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop)

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    async def _fake_deploy_connector(self, connector_name, config):
        return {"config": config}

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _fake_deploy_connector)

    await pipeline_service.claim_lifecycle_transition("org_claim_7", created["id"], "deploying", "op-1")
    result = await pipeline_service.deploy("org_claim_7", created["id"])

    assert result["status"] == "deploying"


# ── Route level — proves the submission layer rejects a duplicate before ──
# ── ever creating a second Operation ───────────────────────────────────────

def test_a_second_deploy_submitted_before_the_first_resolves_is_rejected_over_http(fake_postgres, monkeypatch) -> None:
    # settings.runtime_deploy_enabled is off by default in tests — the claim
    # sets the FINAL status directly in that mode (deploy() itself has no
    # async operation to model then, see status_to_claim_for), which is
    # still exactly the case this test needs: the claim, not a live Kafka
    # Connect round-trip, is what's under test here.
    template, connection = _seed_org(fake_postgres, "org_claim_http_1")
    created = asyncio.run(_create_pipeline("org_claim_http_1", template, connection))

    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop)
    org_header = {"X-Tenant-Id": "org_claim_http_1"}

    first = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers=org_header)
    assert first.status_code == 202
    first_operation_id = first.json()["id"]

    second = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers=org_header)
    assert second.status_code == 409

    # Confirm via the pipeline itself: still pointing at the FIRST
    # operation, never overwritten or duplicated.
    fetched = client.get(f"/api/v1/dataconnect/pipelines/{created['id']}", headers=org_header)
    assert fetched.json()["status"] == "deployed"
    assert fetched.json()["active_operation_id"] == first_operation_id


def test_pause_resume_restart_are_each_rejected_on_a_duplicate_submission_over_http(fake_postgres, monkeypatch) -> None:
    """Same guard, exercised for the other three actions the ticket names —
    not just deploy. Runs with runtime_deploy_enabled on so each claim
    targets the transitional status (pausing/resuming/restarting), the
    normal production path — the queued Operation itself is never actually
    executed in this test (no worker is running), so between each pair of
    calls the row is reset by hand to what a completed operation would have
    left behind, purely to set up the NEXT action's precondition."""
    template, connection = _seed_org(fake_postgres, "org_claim_http_2")
    created = asyncio.run(_create_pipeline("org_claim_http_2", template, connection))
    fake_postgres.pipelines[created["id"]].status = "running"
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)

    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop)
    org_header = {"X-Tenant-Id": "org_claim_http_2"}

    first_pause = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/pause", headers=org_header)
    assert first_pause.status_code == 202
    second_pause = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/pause", headers=org_header)
    assert second_pause.status_code == 409

    fake_postgres.pipelines[created["id"]].status = "paused"
    fake_postgres.pipelines[created["id"]].active_operation_id = None
    first_resume = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/resume", headers=org_header)
    assert first_resume.status_code == 202
    second_resume = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/resume", headers=org_header)
    assert second_resume.status_code == 409

    fake_postgres.pipelines[created["id"]].status = "running"
    fake_postgres.pipelines[created["id"]].active_operation_id = None
    first_restart = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/restart", headers=org_header)
    assert first_restart.status_code == 202
    second_restart = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/restart", headers=org_header)
    assert second_restart.status_code == 409


# ── Regression: a failed operation must release its claim ─────────────────
#
# Found live: claim_lifecycle_transition moves a pipeline onto a
# transitional status BEFORE deploy() ever runs. If deploy() then fails
# (here: a real case hit in practice — deploy_preflight rejecting an
# incompatible Kafka Connect worker version), nothing reverted the
# pipeline off "deploying" — it was stuck there permanently, and its own
# duplicate-submission guard then rejected every subsequent retry as "an
# operation already in progress" even though that operation had already
# failed. app/api/routes/pipelines.py's _lifecycle_work wrapper (calling
# PipelineService.release_claim_on_failure on any exception) is the fix.

@pytest.mark.asyncio
async def test_release_claim_on_failure_moves_a_stuck_pipeline_to_failed(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_release_1")
    created = await _create_pipeline("org_release_1", template, connection)
    await pipeline_service.claim_lifecycle_transition("org_release_1", created["id"], "deploying", "op-stuck-1")

    await pipeline_service.release_claim_on_failure("org_release_1", created["id"])

    recovered = await pipeline_service.get_or_404("org_release_1", created["id"])
    assert recovered["status"] == "failed"
    assert recovered["active_operation_id"] is None


@pytest.mark.asyncio
async def test_release_claim_on_failure_is_a_noop_once_status_already_moved_on(fake_postgres) -> None:
    """A stale retry of an already-superseded operation must never clobber
    a legitimate, more-recent write."""
    template, connection = _seed_org(fake_postgres, "org_release_2")
    created = await _create_pipeline("org_release_2", template, connection)
    await pipeline_service.claim_lifecycle_transition("org_release_2", created["id"], "deploying", "op-stuck-2")
    await pipeline_service.set_status("org_release_2", created["id"], "running")

    await pipeline_service.release_claim_on_failure("org_release_2", created["id"])

    unchanged = await pipeline_service.get_or_404("org_release_2", created["id"])
    assert unchanged["status"] == "running"


def test_a_failed_deploy_lands_on_failed_not_stuck_deploying_and_can_be_retried_over_http(
    fake_postgres, monkeypatch,
) -> None:
    """End-to-end reproduction of the exact scenario found live: deploy the
    same way the route does (submit -> claim -> run the registered work
    callable, the same one operation_runner's worker would eventually
    claim), force the underlying deploy() call to fail, and confirm the
    pipeline is left "failed" (not "deploying") and immediately
    re-deployable — not permanently rejected by its own idempotency guard."""
    from app.api.routes.pipelines import _lifecycle_work

    template, connection = _seed_org(fake_postgres, "org_release_3")
    created = asyncio.run(_create_pipeline("org_release_3", template, connection))
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop)
    # The live scenario this reproduces only exists when there's an actual
    # async operation to model — with this off, deploy() never touches
    # anything that could fail this way, and the claim already lands on the
    # final status directly (nothing transitional to get stuck on).
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    org_header = {"X-Tenant-Id": "org_release_3"}

    submit = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers=org_header)
    assert submit.status_code == 202

    async def _boom(org_id, pipeline_id):
        raise RuntimeError("Kafka Connect worker version is not in this template's qualified versions")

    monkeypatch.setattr(pipeline_service, "deploy", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(_lifecycle_work("deploy", "org_release_3", created["id"])())

    stuck_check = client.get(f"/api/v1/dataconnect/pipelines/{created['id']}", headers=org_header)
    assert stuck_check.json()["status"] == "failed"
    assert stuck_check.json()["active_operation_id"] is None

    retry = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers=org_header)
    assert retry.status_code == 202
