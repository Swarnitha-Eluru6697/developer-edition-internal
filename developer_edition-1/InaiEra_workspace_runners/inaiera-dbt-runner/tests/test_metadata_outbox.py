"""Tests for RUN-DBT-008: lineage/dataset-registration publication used to
be a best-effort inline HTTP call made right after a run finished — if
UCS or Datasets was briefly down, or the process crashed at exactly the
wrong moment, that metadata was simply gone. These tests prove the
replacement: enqueue() persists durably (and atomically with the run's
terminal state), and deliver_pending() actually retries with backoff
instead of giving up, using a real Postgres database (the `run_db`
fixture in conftest.py) since the whole point is verifying real
persisted-row behavior across "attempts."
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from jose import jwt
from sqlalchemy import select

from app.core import metrics
from app.core.config import get_settings
from app.core.service_auth import DATASETS_AUDIENCE, UCS_AUDIENCE
from app.models import MetadataOutboxEvent, Project, Run
from app.services import metadata_outbox
from app.services.run_service import _finalize_run


@pytest.fixture(autouse=True)
def sinks_enabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openlineage_url", "http://ucs.test/api/v1/lineage")
    monkeypatch.setattr(s, "ucs_internal_token", "ucs-secret")
    monkeypatch.setattr(s, "datasets_url", "http://datasets.test")
    monkeypatch.setattr(s, "datasets_internal_token", "datasets-secret")
    monkeypatch.setattr(s, "metadata_outbox_max_attempts", 3)
    metrics.reset()
    return s


async def _async_result(value):
    return value


async def _make_project_and_run(run_db, *, status: str = "running") -> tuple[uuid.UUID, uuid.UUID]:
    async with run_db() as db:
        project = Project(
            org_id=get_settings().org_id, name="analytics", git_url="https://github.com/acme/analytics.git",
            git_ref="main", engine="pg_lake",
        )
        db.add(project)
        await db.flush()
        run = Run(project_id=project.id, trigger="manual", status=status)
        db.add(run)
        await db.commit()
        return run.id, project.id


# ---------------------------------------------------------------------
# enqueue()
# ---------------------------------------------------------------------
async def test_enqueue_persists_a_pending_event(run_db):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="openlineage", payload={"outputs": []},
        )
        await db.commit()

    async with run_db() as db:
        rows = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].attempt_count == 0
    assert rows[0].payload == {"outputs": []}
    assert rows[0].idempotency_key == f"openlineage:{run_id}"


async def test_enqueue_is_idempotent_for_the_same_run_and_event_type(run_db):
    """The second enqueue for the same (event_type, run_id) is a no-op —
    this is what makes it safe for _finalize_run() to be re-entered (e.g.
    raced by the reconciler) without double-queueing the same event."""
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="openlineage", payload={"outputs": ["first"]},
        )
        await db.commit()

    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="openlineage", payload={"outputs": ["second"]},
        )
        await db.commit()

    async with run_db() as db:
        rows = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload == {"outputs": ["first"]}  # the second call changed nothing


# ---------------------------------------------------------------------
# deliver_pending()
# ---------------------------------------------------------------------
async def test_deliver_pending_marks_a_successful_delivery(run_db, monkeypatch):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="dataset_registration", payload={"datasets": [{"name": "stg_orders"}]},
        )
        await db.commit()

    monkeypatch.setattr(
        "app.services.datasets_client.deliver",
        lambda payload, settings, **kw: _async_result({"sent": True, "status": 200}),
    )

    await metadata_outbox.deliver_pending()

    async with run_db() as db:
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.attempt_count == 0


async def test_deliver_pending_retries_with_backoff_on_failure_then_succeeds(run_db, monkeypatch):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="dataset_registration", payload={"datasets": [{"name": "stg_orders"}]},
        )
        await db.commit()

    monkeypatch.setattr(
        "app.services.datasets_client.deliver",
        lambda payload, settings, **kw: _async_result({"sent": False, "reason": "datasets unreachable"}),
    )
    await metadata_outbox.deliver_pending()

    async with run_db() as db:
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
    assert row.status == "pending"  # not lost — still there to retry
    assert row.attempt_count == 1
    assert row.last_error == "datasets unreachable"
    assert row.next_attempt_at > datetime.now(timezone.utc)

    # Make it due again (simulating the backoff interval having elapsed)
    # and have the endpoint succeed this time.
    async with run_db() as db:
        row = await db.get(MetadataOutboxEvent, row.id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    monkeypatch.setattr(
        "app.services.datasets_client.deliver",
        lambda payload, settings, **kw: _async_result({"sent": True, "status": 200}),
    )
    await metadata_outbox.deliver_pending()

    async with run_db() as db:
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
    assert row.status == "delivered"
    assert row.attempt_count == 1  # unchanged by the successful attempt


async def test_deliver_pending_skips_rows_not_yet_due(run_db, monkeypatch):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="dataset_registration", payload={"datasets": [{"name": "stg_orders"}]},
        )
        await db.flush()
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()

    def _must_not_be_called(payload, settings, **kw):
        raise AssertionError("a not-yet-due row must not be delivered")

    monkeypatch.setattr("app.services.datasets_client.deliver", _must_not_be_called)

    await metadata_outbox.deliver_pending()  # must not raise

    async with run_db() as db:
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
    assert row.status == "pending"
    assert row.attempt_count == 0


async def test_deliver_pending_dispatches_openlineage_events_to_the_lineage_emitter(run_db, monkeypatch):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(
            db, run_id=run_id, project_id=project_id,
            event_type="openlineage", payload={"outputs": []},
        )
        await db.commit()

    called = {}

    def fake_deliver(payload, settings, **kw):
        called["payload"] = payload
        called["idempotency_key"] = kw.get("idempotency_key")
        return {"sent": True, "status": 200}

    monkeypatch.setattr("app.executors.openlineage_emitter.deliver", fake_deliver)

    await metadata_outbox.deliver_pending()

    assert called["payload"] == {"outputs": []}
    assert called["idempotency_key"] == f"openlineage:{run_id}"
    async with run_db() as db:
        row = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalar_one()
    assert row.status == "delivered"


# ---------------------------------------------------------------------
# _finalize_run() enqueues atomically with the run's terminal state
# ---------------------------------------------------------------------
async def test_finalize_run_enqueues_both_events_atomically_with_terminal_state(run_db):
    run_id, project_id = await _make_project_and_run(run_db)
    manifest = {
        "nodes": {
            "model.demo.stg_orders": {
                "resource_type": "model", "name": "stg_orders", "relation_name": "public.stg_orders",
                "depends_on": {"nodes": []},
            },
        },
        "sources": {},
    }
    result = {
        "success": True, "errors": [], "nodes": [],
        "manifest": manifest,
        "lineage_event": {"outputs": [{"namespace": "pg", "name": "public.stg_orders", "facets": {}}]},
    }

    ok = await _finalize_run(run_id, project_id, "deadbeef", result)
    assert ok is True

    async with run_db() as db:
        run = await db.get(Run, run_id)
        assert run.status == "succeeded"

        events = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalars().all()
    by_type = {e.event_type: e for e in events}
    assert set(by_type) == {"openlineage", "dataset_registration"}
    assert by_type["openlineage"].payload == result["lineage_event"]
    assert by_type["dataset_registration"].payload == {
        "datasets": [{"name": "stg_orders", "relation_name": "public.stg_orders",
                      "project_id": str(project_id), "run_id": str(run_id)}],
    }
    assert all(e.status == "pending" for e in events)


async def test_finalize_run_does_not_enqueue_dataset_registration_on_a_failed_build(run_db):
    """A failed build still has nothing to register as a dataset, but its
    lineage event (test results) is exactly what the catalog most needs —
    see openlineage_emitter's own docstring — so that one is still queued."""
    run_id, project_id = await _make_project_and_run(run_db)
    result = {
        "success": False, "errors": ["Compilation Error"], "nodes": [],
        "manifest": {"nodes": {}, "sources": {}},
        "lineage_event": {"outputs": [{"namespace": "pg", "name": "public.broken", "facets": {}}]},
    }

    await _finalize_run(run_id, project_id, None, result)

    async with run_db() as db:
        events = (await db.execute(
            select(MetadataOutboxEvent).where(MetadataOutboxEvent.run_id == run_id)
        )).scalars().all()
    assert {e.event_type for e in events} == {"openlineage"}


async def test_finalize_run_skips_enqueue_for_disabled_sinks(run_db, monkeypatch, sinks_enabled):
    """Rows for an unconfigured sink must not pile up forever."""
    monkeypatch.setattr(sinks_enabled, "ucs_internal_token", None)
    monkeypatch.setattr(sinks_enabled, "datasets_internal_token", None)
    run_id, project_id = await _make_project_and_run(run_db)
    manifest = {"nodes": {"model.d.m": {"resource_type": "model", "name": "m", "relation_name": "p.m",
                                        "depends_on": {"nodes": []}}}, "sources": {}}
    await _finalize_run(run_id, project_id, None, {
        "success": True, "errors": [], "nodes": [], "manifest": manifest,
        "lineage_event": {"outputs": [{"namespace": "pg", "name": "p.m", "facets": {}}]},
    })
    async with run_db() as db:
        rows = (await db.execute(select(MetadataOutboxEvent))).scalars().all()
        assert rows == []
        assert (await db.get(Run, run_id)).status == "succeeded"


async def test_pending_row_for_a_since_disabled_sink_is_marked_skipped(run_db, monkeypatch, sinks_enabled):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(db, run_id=run_id, project_id=project_id,
                                      event_type="dataset_registration", payload={"datasets": []})
        await db.commit()
    monkeypatch.setattr(sinks_enabled, "datasets_internal_token", None)
    monkeypatch.setattr("app.services.datasets_client.deliver",
                        lambda *a, **kw: pytest.fail("must not deliver to a disabled sink"))
    await metadata_outbox.deliver_pending()
    async with run_db() as db:
        row = (await db.execute(select(MetadataOutboxEvent))).scalar_one()
    assert row.status == "skipped"
    assert metrics.get("metadata_outbox_skipped_total", event_type="dataset_registration") == 1


async def test_delivery_dead_letters_after_max_attempts(run_db, monkeypatch):
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(db, run_id=run_id, project_id=project_id,
                                      event_type="dataset_registration", payload={"datasets": []})
        await db.commit()
    monkeypatch.setattr("app.services.datasets_client.deliver",
                        lambda payload, settings, **kw: _async_result({"sent": False, "reason": "503"}))

    for _ in range(5):
        await metadata_outbox.deliver_pending()
        async with run_db() as db:
            row = (await db.execute(select(MetadataOutboxEvent))).scalar_one()
            if row.status == "pending":
                row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()

    async with run_db() as db:
        row = (await db.execute(select(MetadataOutboxEvent))).scalar_one()
    assert row.status == "dead_letter"
    assert row.attempt_count == 3  # max_attempts, then no further retries
    assert row.last_error == "503"
    assert metrics.get("metadata_outbox_dead_letter_total", event_type="dataset_registration") == 1


@respx.mock
async def test_real_delivery_sends_idempotency_key_and_scoped_tokens(run_db):
    """Both real HTTP clients send the outbox row's idempotency key as an
    Idempotency-Key header, plus a short-lived scoped bearer token."""
    run_id, project_id = await _make_project_and_run(run_db)
    async with run_db() as db:
        await metadata_outbox.enqueue(db, run_id=run_id, project_id=project_id,
                                      event_type="openlineage",
                                      payload={"outputs": [], "inputs": []})
        await metadata_outbox.enqueue(db, run_id=run_id, project_id=project_id,
                                      event_type="dataset_registration", payload={"datasets": [{"name": "m"}]})
        await db.commit()
    ucs = respx.post("http://ucs.test/api/v1/lineage/events").mock(return_value=httpx.Response(200))
    ds = respx.post("http://datasets.test/v1/datasets/register-batch").mock(return_value=httpx.Response(201))

    await metadata_outbox.deliver_pending()

    ucs_req = ucs.calls.last.request
    ds_req = ds.calls.last.request
    assert ucs_req.headers["Idempotency-Key"] == f"openlineage:{run_id}"
    assert ds_req.headers["Idempotency-Key"] == f"dataset_registration:{run_id}"
    ucs_claims = jwt.decode(ucs_req.headers["Authorization"].removeprefix("Bearer "), "ucs-secret",
                            algorithms=["HS256"], audience=UCS_AUDIENCE)
    ds_claims = jwt.decode(ds_req.headers["Authorization"].removeprefix("Bearer "), "datasets-secret",
                           algorithms=["HS256"], audience=DATASETS_AUDIENCE)
    assert ucs_claims["scope"] == "ucs.lineage.write"
    assert ds_claims["scope"] == "datasets.register.write"
    assert ds_claims["exp"] - ds_claims["iat"] <= 300
    async with run_db() as db:
        rows = (await db.execute(select(MetadataOutboxEvent))).scalars().all()
    assert {r.status for r in rows} == {"delivered"}
