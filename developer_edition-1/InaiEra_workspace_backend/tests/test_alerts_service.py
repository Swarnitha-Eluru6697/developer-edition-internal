"""Alerts against a real, migrated Postgres: the sweep end to end (raise, send, keep quiet, clear), the lifecycle and the CRUD rules.

Integration tests: they run when INTEGRATION_DATABASE_URL points at a migrated database (they empty the alert tables first)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from alerts_support import SLACK, TEAMS, db, receiver, secrets, world  # noqa: F401  (fixtures)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import WorkspaceError
from app.models import Alert, AlertObservation, AlertRule
from app.services.alerts.kinds import DEFAULT_RULES
from app.services.alerts.service import AlertService, now_utc

pytestmark = pytest.mark.integration


async def add_channel(svc, name="#oncall", kind="slack", url=SLACK, **kw):
    ch = await svc.create_channel(actor="admin", name=name, kind=kind, webhook_url=url, **{"min_severity": "low", "enabled": True, **kw})
    await svc.db.commit()
    return ch


async def alerts_of(db, **where):
    return list((await db.scalars(select(Alert).filter_by(**where).order_by(Alert.raised_at))).all())


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_the_default_rules_are_created_once_and_do_not_come_back_after_a_delete(db):
    svc = AlertService(db)
    rules = await svc.list_rules()
    assert sorted(r.name for r in rules) == sorted(d.name for d in DEFAULT_RULES)
    for r in rules:
        await svc.delete_rule(r.id)
    await db.commit()
    assert await svc.list_rules() == []


async def test_rule_numbers_are_validated_per_kind(db):
    svc = AlertService(db)
    for bad in ({"kind": "failure_rate", "threshold": 150}, {"kind": "run_failed", "window_seconds": 1234},
                {"kind": "flink_job_failed", "window_seconds": 3600}, {"kind": "run_failed", "min_runs": 5}):
        with pytest.raises(WorkspaceError) as e:
            fields = dict(severity="low", threshold=1, window_seconds=0, min_runs=1, scope_job_key=None, scope_job_name=None, enabled=True)
            await svc.create_rule(actor="a", name=f"r-{uuid.uuid4().hex[:6]}", **{**fields, **bad})
        assert e.value.status_code == 400 and e.value.detail["code"] == "VALIDATION_FAILED"


async def test_rule_and_channel_names_are_unique_ignoring_case(db, secrets):
    svc = AlertService(db)
    base = dict(severity="low", threshold=1, window_seconds=0, min_runs=1, scope_job_key=None, scope_job_name=None, enabled=True)
    await svc.create_rule(actor="a", name="My-Rule", kind="run_failed", **base)
    with pytest.raises(WorkspaceError) as e:
        await svc.create_rule(actor="a", name="my-rule", kind="run_failed", **base)
    assert e.value.status_code == 409 and e.value.detail["code"] == "ALERT_RULE_NAME_TAKEN"
    await add_channel(svc, name="#Ops")
    with pytest.raises(WorkspaceError) as e:
        await add_channel(svc, name="#ops")
    assert e.value.status_code == 409 and e.value.detail["code"] == "ALERT_CHANNEL_NAME_TAKEN"


async def test_changing_a_rule_to_a_kind_without_a_window_resets_the_stale_fields(db):
    svc = AlertService(db)
    rule = await svc.create_rule(actor="a", name="r", kind="failure_rate", severity="low", threshold=50, window_seconds=3600, min_runs=5,
                                 scope_job_key=None, scope_job_name=None, enabled=True)
    rule = await svc.update_rule(rule.id, {"kind": "flink_job_failed"})
    assert (rule.kind, rule.window_seconds, rule.min_runs, rule.threshold) == ("flink_job_failed", 0, 1, 1)


# ── channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_channels_webhook_url_lives_only_in_the_secrets_backend(db, secrets):
    svc = AlertService(db)
    ch = await add_channel(svc)
    assert secrets.data[ch.secret_ref] == {"url": SLACK}
    assert ch.url_hint == "hooks.slack.com/…/WXYZ" and SLACK not in str(ch.__dict__)
    row = (await db.execute(text("SELECT * FROM alert_channels"))).mappings().one()
    assert SLACK not in str(dict(row))


async def test_a_refused_url_writes_no_row_and_no_secret(db, secrets):
    svc = AlertService(db)
    with pytest.raises(WorkspaceError) as e:
        await add_channel(svc, url="https://evil.example.com/hook")
    assert e.value.status_code == 400 and e.value.detail["details"]["field"] == "webhook_url"
    assert secrets.data == {} and await svc.list_channels() == []


async def test_editing_a_channel_keeps_the_url_unless_a_new_one_is_given_and_a_kind_change_needs_one(db, secrets):
    svc = AlertService(db)
    ch = await add_channel(svc)
    await svc.update_channel(ch.id, {"min_severity": "high", "webhook_url": ""})
    assert secrets.data[ch.secret_ref]["url"] == SLACK and ch.min_severity == "high"
    with pytest.raises(WorkspaceError):
        await svc.update_channel(ch.id, {"kind": "teams"})
    await svc.update_channel(ch.id, {"kind": "teams", "webhook_url": TEAMS})
    assert secrets.data[ch.secret_ref]["url"] == TEAMS and ch.kind == "teams" and ch.url_hint.startswith("contoso.webhook.office.com")


async def test_deleting_a_channel_removes_its_secret(db, secrets):
    svc = AlertService(db)
    ch = await add_channel(svc)
    await svc.delete_channel(ch.id)
    await db.commit()
    assert secrets.data == {} and await svc.list_channels() == []


async def test_a_test_message_records_how_the_last_attempt_went(db, secrets, receiver):
    svc = AlertService(db)
    ch = await add_channel(svc)
    ok, detail, _ = await svc.test_channel(ch.id, actor="admin", client=receiver.client)
    assert ok and detail == "delivered" and ch.last_ok is True and ch.last_attempt_at is not None
    url, body = receiver.posts[-1]
    assert url == SLACK and "Test alert" in body["text"]
    receiver.status, receiver.text = 404, "no_service"
    ok, detail, _ = await svc.test_channel(ch.id, actor="admin", client=receiver.client)
    assert not ok and detail == "404 no_service" and ch.last_ok is False and ch.last_detail == "404 no_service"


# ── the sweep ────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_failure_is_raised_but_waits_unsent_while_no_channel_exists(db, secrets, world):
    svc = AlertService(db)
    world.run("failed", detail="Compilation Error")
    res = await svc.sweep(client=None)
    assert res.ran and res.raised == 1 and res.detail == "ok"
    [a] = await alerts_of(db)
    assert (a.state, a.delivery, a.rule_name, a.severity, a.job_name) == ("open", "no_channel", "job-run-failed", "high", "customers")
    assert a.summary == "dbt project 'customers' failed: Compilation Error"
    counts = await svc.counts()
    assert counts == {"open": 0, "acknowledged": 0, "resolved": 0, "undelivered": 1}      # not listed under Open until it is sent
    alerts, total = await svc.list_alerts("open", start=None, end=None, limit=10, offset=0)
    assert alerts == [] and total == 0


async def test_adding_a_channel_sends_the_waiting_alert_once_and_never_again(db, secrets, world, receiver):
    svc = AlertService(db)
    world.run("failed", detail="boom")
    await svc.sweep()
    ch = await add_channel(svc)
    res = await svc.sweep(client=receiver.client)
    assert res.delivered == 1 and len(receiver.posts) == 1
    url, body = receiver.posts[0]
    assert url == SLACK and body["text"].startswith("[HIGH] dbt project 'customers': job-run-failed")
    [a] = await alerts_of(db)
    assert a.delivery == "sent" and a.sent_at is not None and a.delivery_attempts == 1
    assert [e.action for e in (await svc.get_alert(a.id)).events] == ["raised", "sent to #oncall"]
    assert (await svc.counts())["open"] == 1
    await svc.sweep(client=receiver.client)
    await svc.sweep(client=receiver.client)
    assert len(receiver.posts) == 1 and len(await alerts_of(db)) == 1                        # still failing: nobody is told again
    assert ch.last_ok is True


async def test_the_same_failure_seen_by_every_sweep_is_one_alert(db, secrets, world):
    svc = AlertService(db)
    world.run("failed")
    for _ in range(4):
        await svc.sweep()
    assert len(await alerts_of(db)) == 1
    assert await db.scalar(select(func.count()).select_from(AlertObservation)) == 1


async def test_a_success_afterwards_clears_the_alert_by_itself_and_a_new_failure_raises_a_new_one(db, secrets, world, receiver):
    svc = AlertService(db)
    await add_channel(svc)
    world.run("failed", 10)
    await svc.sweep(client=receiver.client)
    world.run("succeeded", 5)
    res = await svc.sweep(client=receiver.client)
    assert res.cleared == 1
    [a] = await alerts_of(db)
    assert a.state == "resolved" and a.resolved_at is not None
    assert (await svc.get_alert(a.id)).events[-1].action == "cleared automatically"
    world.run("failed", 1)
    await svc.sweep(client=receiver.client)
    assert [x.state for x in await alerts_of(db)] == ["resolved", "open"]
    assert len(receiver.posts) == 3                                  # the alert, the "cleared automatically" follow-up, the new alert


async def test_acknowledging_keeps_it_quiet_and_resolving_then_a_new_failure_alerts_again(db, secrets, world, receiver):
    svc = AlertService(db)
    await add_channel(svc)
    world.run("failed", 10)
    await svc.sweep(client=receiver.client)
    [a] = await alerts_of(db)
    await svc.acknowledge(a.id, actor="padmin", note="looking", client=receiver.client)
    await db.commit()
    await svc.sweep(client=receiver.client)
    assert (await alerts_of(db))[0].state == "acknowledged" and len(receiver.posts) == 2       # the alert + the "acknowledged" follow-up; still failing, not sent again
    detail = await svc.resolve(a.id, actor="padmin", note=None, client=receiver.client)
    assert [e.action for e in detail.events] == ["raised", "sent to #oncall", "acknowledged", "resolved"]
    assert detail.events[2].note == "looking" and detail.state == "resolved"
    await db.commit()
    world.run("failed", 1)
    await svc.sweep(client=receiver.client)
    assert len(await alerts_of(db)) == 2 and len(receiver.posts) == 4                          # alert, acknowledged, resolved, then the new failure alerts


async def test_acknowledge_and_resolve_are_idempotent_and_a_resolved_alert_cannot_be_acknowledged(db, secrets, world):
    svc = AlertService(db)
    world.run("failed")
    await svc.sweep()
    [a] = await alerts_of(db)
    await svc.acknowledge(a.id, actor="x", note=None)
    await svc.acknowledge(a.id, actor="x", note=None)
    assert [e.action for e in (await svc.get_alert(a.id)).events].count("acknowledged") == 1
    await svc.resolve(a.id, actor="x", note=None)
    await svc.resolve(a.id, actor="x", note=None)
    assert [e.action for e in (await svc.get_alert(a.id)).events].count("resolved") == 1
    with pytest.raises(WorkspaceError) as e:
        await svc.acknowledge(a.id, actor="x", note=None)
    assert e.value.status_code == 409 and e.value.detail["code"] == "ALERT_ALREADY_RESOLVED"
    with pytest.raises(WorkspaceError) as e:
        await svc.get_alert(uuid.uuid4())
    assert e.value.status_code == 404


async def test_a_channels_severity_floor_and_job_scope_decide_what_it_receives(db, secrets, world, receiver):
    svc = AlertService(db)
    await svc.create_rule(actor="a", name="low-rule", kind="run_failed", severity="low", threshold=1, window_seconds=3600, min_runs=1,
                          scope_job_key="j2", scope_job_name="other", enabled=True)
    await add_channel(svc, name="#high-only", min_severity="high", url=SLACK)
    await add_channel(svc, name="#j1-only", kind="teams", url=TEAMS, scope_job_key="j1", scope_job_name="customers")
    world.run("failed", job="j1", name="customers")           # high (default rule): both channels? no: #high-only yes, #j1-only yes
    world.run("failed", job="j2", name="other")               # high by default rule, plus the low rule: #high-only only (j1-only is scoped)
    await svc.sweep(client=receiver.client)
    posted = sorted((u.split("/")[2], b["text"] if "text" in b else b["summary"]) for u, b in receiver.posts)
    assert [h for h, _ in posted].count("hooks.slack.com") == 2           # j1's and j2's high alerts reach #high-only
    teams_posts = [b for u, b in receiver.posts if "office.com" in u]
    assert len(teams_posts) == 1 and "customers" in teams_posts[0]["summary"]                 # #j1-only got only j1's
    low = [a for a in await alerts_of(db) if a.rule_name == "low-rule"]
    assert len(low) == 1 and low[0].delivery == "no_channel"                                # severity low: below #high-only's floor, wrong scope


async def test_a_failing_webhook_is_retried_a_few_times_then_left_failed_and_recovers_when_it_works_again(db, secrets, world, receiver, monkeypatch):
    svc = AlertService(db)
    monkeypatch.setattr(get_settings(), "alerts_delivery_max_attempts", 3)
    ch = await add_channel(svc)
    receiver.status, receiver.text = 404, "no_service"
    world.run("failed")
    for _ in range(5):
        await svc.sweep(client=receiver.client)
    [a] = await alerts_of(db)
    assert a.delivery == "failed" and a.delivery_attempts == 3 and len(receiver.posts) == 3     # stops at the limit
    events = (await svc.get_alert(a.id)).events
    assert events[1].action == "delivery to #oncall failed" and events[1].note == "404 no_service"
    assert ch.last_ok is False and ch.last_detail == "404 no_service"
    assert (await svc.counts())["undelivered"] == 1
    # an admin fixes the channel and presses "Run rules now": the failed alert gets another chance only while attempts remain
    a.delivery_attempts = 0
    await db.commit()
    receiver.status, receiver.text = 200, "ok"
    await svc.sweep(client=receiver.client)
    assert (await alerts_of(db))[0].delivery == "sent" and ch.last_ok is True


async def test_a_disabled_rule_raises_nothing_and_leaves_its_open_alert_alone(db, secrets, world):
    svc = AlertService(db)
    world.run("failed")
    await svc.sweep()
    rule = (await db.scalars(select(AlertRule).where(AlertRule.name == "job-run-failed"))).one()
    await svc.update_rule(rule.id, {"enabled": False})
    await db.commit()
    world.run("succeeded", 0)                                                                    # the condition has stopped, but the rule is off
    await svc.sweep()
    [a] = await alerts_of(db)
    assert a.state == "open"                                                                    # not closed behind anyone's back
    world.run("failed", 0, ref="another")
    await svc.sweep()
    assert len(await alerts_of(db)) == 1


async def test_a_source_that_is_down_is_reported_and_raises_nothing_by_itself(db, secrets, world):
    svc = AlertService(db)
    world.run("failed")
    await svc.sweep()
    world.down = {"dbt-runner", "flink"}
    res = await svc.sweep()
    assert res.ran and "dbt Runner could not be reached" in res.detail and "FlinkSQL runner" in res.detail
    assert [a.state for a in await alerts_of(db)] == ["open"]                                   # the open alert is still open
    state = await svc._state()
    assert state.last_sweep_ok is False and "could not be reached" in state.last_sweep_detail


async def test_the_sweep_lease_lets_only_one_sweep_run_at_a_time_and_expires(db, secrets, world):
    svc = AlertService(db)
    assert await svc.take_lease(now_utc()) is True
    assert await svc.take_lease(now_utc()) is False
    res = await svc.sweep()
    assert res.ran is False and res.detail == "A sweep is already running."
    assert await svc.take_lease(now_utc() + timedelta(minutes=5)) is True                      # a dead sweeper's lease runs out
    await svc.release_lease(now_utc(), True, "ok")
    assert (await svc.sweep()).ran is True


async def test_observations_older_than_the_retention_are_forgotten(db, secrets, world, monkeypatch):
    svc = AlertService(db)
    monkeypatch.setattr(get_settings(), "alerts_retention_days", 1)
    world.run("succeeded", minutes_ago=3 * 24 * 60, ref="old")
    world.run("succeeded", minutes_ago=5, ref="new")
    await svc.sweep()
    refs = set((await db.scalars(select(AlertObservation.ref_id))).all())
    assert refs == {"new"}


async def test_flink_and_dagster_readings_raise_their_own_alerts(db, secrets, world):
    from app.services.alerts.engine import Obs
    svc = AlertService(db)
    world.flink.append(Obs("f1", "orders-stream", "flink", "flink_state", "failed", world.now - timedelta(minutes=2), "f1:failed:x", None, "bad sink"))
    world.dagster.append(Obs("j9", "revenue", "dbt", "blocked", "blocked", world.now - timedelta(minutes=1), "d1", None,
                             "Not started: upstream project 'customers' failed."))
    await svc.sweep()
    got = {a.rule_name: a for a in await alerts_of(db)}
    assert got["flink-job-failed"].summary == "Flink job 'orders-stream' is in the failed state: bad sink"
    assert got["upstream-blocked"].summary.startswith("A run of dbt project 'revenue' was refused: Not started: upstream project 'customers'")


async def test_a_quality_failure_raises_the_quality_alert_and_not_the_generic_one(db, secrets, world):
    svc = AlertService(db)
    world.run("failed", detail="Got 4 results, configured to fail if != 0", failure_class="quality")
    await svc.sweep()
    assert [a.rule_name for a in await alerts_of(db)] == ["quality-check-failed"]


async def test_breaching_tells_what_is_open_and_what_the_next_sweep_would_raise(db, secrets, world):
    svc = AlertService(db)
    world.run("failed", job="a", name="a")
    await svc.sweep()
    # a rule created after the sweep also breaches for the same job, and has not been raised yet
    await svc.create_rule(actor="a", name="all-failing", kind="failure_rate", severity="low", threshold=100, window_seconds=0, min_runs=1,
                          scope_job_key=None, scope_job_name=None, enabled=True)
    got = {(b.rule_name, b.job_key, state) for b, state in await svc.breaching()}
    assert got == {("job-run-failed", "a", "already_open"), ("all-failing", "a", "would_raise")}
    assert len(await alerts_of(db)) == 1                                                         # asking raised nothing


async def test_listing_pages_and_filters_by_date(db, secrets, world, receiver):
    svc = AlertService(db)
    await add_channel(svc)
    for i in range(7):
        world.run("failed", job=f"j{i}", name=f"job{i}")
    await svc.sweep(client=receiver.client)
    first, total = await svc.list_alerts("open", start=None, end=None, limit=5, offset=0)
    rest, _ = await svc.list_alerts("open", start=None, end=None, limit=5, offset=5)
    assert total == 7 and len(first) == 5 and len(rest) == 2 and not {a.id for a in first} & {a.id for a in rest}
    none, n = await svc.list_alerts("open", start=now_utc() + timedelta(days=1), end=None, limit=5, offset=0)
    assert none == [] and n == 0


async def test_two_live_alerts_for_one_rule_and_job_cannot_exist(db, secrets, world):
    svc = AlertService(db)
    world.run("failed")
    await svc.sweep()
    [a] = await alerts_of(db)
    db.add(Alert(org_id=a.org_id, rule_id=a.rule_id, rule_name=a.rule_name, kind=a.kind, severity=a.severity, job_key=a.job_key,
                 job_name=a.job_name, job_kind=a.job_kind, summary="dup", fingerprint=a.fingerprint, state="open", delivery="pending",
                 raised_at=now_utc()))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
