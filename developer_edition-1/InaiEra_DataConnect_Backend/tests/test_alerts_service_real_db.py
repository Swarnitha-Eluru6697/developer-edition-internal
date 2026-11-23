"""AlertService against a REAL, migrated PostgreSQL (DATACONNECT_REAL_DB_TESTS=1): default rules, validation, encrypted webhook URLs, and the
sweep end to end — raise, deliver, never re-send, clear automatically with a follow-up, severity floor and scope, retries, lease, "resolved by
hand stays resolved until a new failure", every source (state, operations, threshold pauses, dead-letter and quality counters), concurrency
of acknowledge / resolve, and pruning."""

import asyncio
from datetime import timedelta

import pytest
from alerts_real_db import (  # noqa: F401 - the fixtures are found by pytest under their own names: factory, hooks, scratch_url
    SLACK,
    TEAMS,
    add_failures,
    add_operation,
    add_pipeline,
    add_threshold_pause,
    delete_pipeline,
    factory_fixture,
    hooks_fixture,
    new_org,
    now,
    scratch_url_fixture,
    set_state,
    skip_without_db,
)
from sqlalchemy import select, text

from app.core.config import settings
from app.core.errors import DataConnectError, ValidationError
from app.models import Alert, AlertObservation, AlertRule
from app.services.alerts.service import AlertService

pytestmark = skip_without_db


async def svc(make, org):
    session = make()
    return session, AlertService(session, org)


async def sweep(make, org, hooks):
    async with make() as s, hooks.client() as c:
        return await AlertService(s, org).sweep(client=c)


async def alerts_of(make, org, **where):
    async with make() as s:
        q = select(Alert).where(Alert.org_id == org)
        for k, v in where.items():
            q = q.where(getattr(Alert, k) == v)
        rows = (await s.scalars(q.order_by(Alert.raised_at))).all()
        for a in rows:
            await s.refresh(a, ["events"])
        return rows


async def channel(make, org, name, url=SLACK, kind="slack", **f):
    async with make() as s:
        ch = await AlertService(s, org).create_channel(actor="tester", name=name, kind=kind, webhook_url=url, min_severity=f.get("min_severity", "low"),
                                                       scope_pipeline_id=f.get("scope"), scope_pipeline_name=None, enabled=True)
        await s.commit()
        return str(ch.id)


async def only_rule(make, org, kind, **f):
    """Switch every default rule off but the one of `kind` (so a test sees exactly one alert per pipeline)."""
    async with make() as s:
        svc_ = AlertService(s, org)
        rules = await svc_.list_rules()
        for r in rules:
            r.enabled = r.kind == kind
            for k, v in f.items():
                if r.kind == kind:
                    setattr(r, k, v)
        await s.commit()


# ── rules and channels ──────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_default_rules_are_created_once_and_a_deleted_one_does_not_come_back(factory):
    org = await new_org(factory)
    async with factory() as s:
        names = [r.name for r in await AlertService(s, org).list_rules()]
        await s.commit()
    assert sorted(names) == sorted(["pipeline-failed", "pipeline-degraded", "deploy-failed", "error-threshold-paused", "high-dlq-errors",
                                    "quality-failures"])
    async with factory() as s:
        svc_ = AlertService(s, org)
        await svc_.delete_rule(next(str(r.id) for r in await svc_.list_rules() if r.name == "pipeline-degraded"))
        await s.commit()
    async with factory() as s:
        assert "pipeline-degraded" not in [r.name for r in await AlertService(s, org).list_rules()]


async def test_rule_numbers_are_checked_per_kind(factory):
    org = await new_org(factory)
    async with factory() as s:
        svc_ = AlertService(s, org)
        for kind, extra, field in (("pipeline_failed", {"threshold": 2}, "threshold"), ("pipeline_failed", {"window_seconds": 3600}, "window_seconds"),
                                   ("dlq_errors", {"window_seconds": 123}, "window_seconds"), ("dlq_errors", {"min_runs": 3}, "min_runs")):
            args = {"name": f"r-{field}-{kind}", "kind": kind, "severity": "high", "threshold": 1, "window_seconds": 0, "min_runs": 1, "scope_pipeline_id": None,
                        "scope_pipeline_name": None, "enabled": True} | extra
            with pytest.raises(ValidationError) as err:
                await svc_.create_rule(actor="t", **args)
            assert err.value.field_errors[0].field == field


async def test_names_are_unique_per_organization_whatever_the_case(factory):
    org, other = await new_org(factory), await new_org(factory)
    rule = {"kind": "dlq_errors", "severity": "high", "threshold": 5, "window_seconds": 900, "min_runs": 1, "scope_pipeline_id": None, "scope_pipeline_name": None,
                "enabled": True}
    async with factory() as s:
        await AlertService(s, org).create_rule(actor="t", name="Loud DLQ", **rule)
        await s.commit()
    async with factory() as s:
        with pytest.raises(DataConnectError) as err:
            await AlertService(s, org).create_rule(actor="t", name="loud dlq", **rule)
        assert (err.value.status_code, err.value.code, err.value.field_errors[0].field) == (409, "ALERT_RULE_NAME_TAKEN", "name")
    async with factory() as s:
        await AlertService(s, other).create_rule(actor="t", name="Loud DLQ", **rule)            # another organization may use the name
        await s.commit()
    await channel(factory, org, "Ops")
    with pytest.raises(DataConnectError) as err:
        await channel(factory, org, "OPS")
    assert err.value.code == "ALERT_CHANNEL_NAME_TAKEN"


async def test_a_webhook_url_is_stored_encrypted_and_only_its_hint_is_readable(factory):
    org = await new_org(factory)
    cid = await channel(factory, org, "slack-ops")
    async with factory() as s:
        dump = (await s.execute(text("SELECT row_to_json(c)::text FROM dc_alert_channels c WHERE id = :i"), {"i": cid})).scalar()
        secret = (await s.execute(text("SELECT envelope::text FROM dc_alert_channel_secrets WHERE channel_id = :i"), {"i": cid})).scalar()
        anywhere = (await s.execute(text("SELECT count(*) FROM dc_alert_channels c, dc_alert_channel_secrets x "
                                         "WHERE row_to_json(c)::text LIKE '%XXXXsecretXXXX%' OR x.envelope::text LIKE '%XXXXsecretXXXX%'"))).scalar()
        ch = await AlertService(s, org).get_channel(cid)
        assert await AlertService(s, org)._url_of(ch) == SLACK                     # the sweep can still read it back
    assert "hooks.slack.com/…/XXXX" in dump
    assert "secret" not in dump
    assert '"ct"' in secret
    assert anywhere == 0


async def test_a_bad_webhook_url_is_refused_on_the_url_field(factory):
    org = await new_org(factory)
    with pytest.raises(ValidationError) as err:
        await channel(factory, org, "x", url="https://evil.example.com/services/x")
    assert err.value.field_errors[0].field == "webhook_url"
    assert "hooks.slack.com" in err.value.field_errors[0].message


async def test_changing_the_type_needs_a_new_url_and_a_blank_url_keeps_the_old_one(factory):
    org = await new_org(factory)
    cid = await channel(factory, org, "c1")
    async with factory() as s:
        with pytest.raises(ValidationError):
            await AlertService(s, org).update_channel(cid, {"kind": "teams"})
    async with factory() as s:
        svc_ = AlertService(s, org)
        await svc_.update_channel(cid, {"name": "renamed", "webhook_url": ""})
        await s.commit()
        assert await svc_._url_of(await svc_.get_channel(cid)) == SLACK
        await svc_.update_channel(cid, {"kind": "teams", "webhook_url": TEAMS})
        await s.commit()
        assert await svc_._url_of(await svc_.get_channel(cid)) == TEAMS


# ── the sweep ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_a_failed_pipeline_raises_one_alert_that_is_sent_once_and_cleared_when_it_runs_again(factory, hooks):
    org = await new_org(factory)
    await channel(factory, org, "slack-ops")
    pid = await add_pipeline(factory, org, "orders-cdc")
    await set_state(factory, org, pid, "running")
    assert (await sweep(factory, org, hooks)).raised == 0
    assert hooks.posts == []

    await set_state(factory, org, pid, "failed", error="Kafka Connect reports the connector FAILED: ConnectException: Could not connect to db:5432")
    summary = await sweep(factory, org, hooks)
    assert (summary.raised, summary.delivered) == (1, 1)
    [a] = await alerts_of(factory, org)
    assert (a.rule_name, a.state, a.delivery, a.pipeline_name) == ("pipeline-failed", "open", "sent", "orders-cdc")
    assert a.summary == "Pipeline 'orders-cdc' failed: Kafka Connect reports the connector FAILED: ConnectException: Could not connect to db:5432"
    assert [e.action for e in a.events] == ["raised", "sent to slack-ops"]
    assert len(hooks.posts) == 1
    assert "Could not connect to db:5432" in hooks.texts()[0]
    assert "orders-cdc" in hooks.texts()[0]

    await sweep(factory, org, hooks)                                                    # still failed: quiet
    assert len(hooks.posts) == 1
    assert len(await alerts_of(factory, org)) == 1

    await set_state(factory, org, pid, "running")
    assert (await sweep(factory, org, hooks)).cleared == 1
    [a] = await alerts_of(factory, org)
    assert a.state == "resolved"
    assert a.events[-1].action == "cleared automatically"
    assert len(hooks.posts) == 2
    assert "cleared automatically" in hooks.texts()[1]


async def test_a_failed_connector_the_self_heal_keeps_restarting_raises_once_and_stays_open(factory, hooks):
    """Seen live: every reconciler tick restarts the failed task, and the tick after reads it as "unknown" (Connect has no state for it while it
    restarts), keeping the same reason. That must not clear the alert, raise it again, or page the channel again."""
    org = await new_org(factory)
    await channel(factory, org, "ops")
    pid = await add_pipeline(factory, org, "final-check")
    reason = "Kafka Connect reports the connector FAILED: password authentication failed for user \"fin_user\""
    for state in ("failed", "unknown", "failed", "unknown", "unknown", "failed"):
        await set_state(factory, org, pid, state, error=reason)
        await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert (a.state, a.delivery) == ("open", "sent")
    assert [e.action for e in a.events] == ["raised", "sent to ops"]
    assert len(hooks.posts) == 1


async def test_resolved_by_hand_stays_resolved_through_restart_blips(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed", error="boom")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    async with factory() as s, hooks.client() as c:
        await AlertService(s, org).resolve(str(a.id), actor="ann", note=None, client=c)
        await s.commit()
    for state in ("unknown", "failed", "unknown", "failed"):
        await set_state(factory, org, pid, state, error="boom")
        await sweep(factory, org, hooks)
    assert [x.state for x in await alerts_of(factory, org)] == ["resolved"]             # the same failure throughout: not back


async def test_a_failure_from_long_ago_stays_open_while_only_unknown_readings_follow(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed", error="boom")
    await sweep(factory, org, hooks)
    await set_state(factory, org, pid, "unknown", error="boom")
    await sweep(factory, org, hooks)
    async with factory() as s:                                                          # the failure was seen 60 days ago, the unknown 59
        await s.execute(text("UPDATE dc_alert_observations SET occurred_at = occurred_at - interval '60 days' "
                             "WHERE org_id = :o AND outcome = 'failed'"), {"o": org})
        await s.execute(text("UPDATE dc_alert_observations SET occurred_at = occurred_at - interval '59 days' "
                             "WHERE org_id = :o AND outcome = 'unknown'"), {"o": org})
        await s.commit()
    await sweep(factory, org, hooks)                                                    # loads (and pruning keeps) the last KNOWN state too
    async with factory() as s:
        left = sorted((await s.execute(select(AlertObservation.outcome).where(AlertObservation.org_id == org))).scalars().all())
    assert left == ["failed", "unknown"]
    assert [a.state for a in await alerts_of(factory, org)] == ["open"]


async def test_a_stalled_quality_worker_shows_as_degraded(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "pg-quality", status="degraded")
    await set_state(factory, org, pid, "running", error="the quality check has been stuck on one record for 6 minutes")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.rule_name == "pipeline-degraded"
    assert "stuck on one record" in a.summary


async def test_paused_and_deleted_pipelines_raise_nothing_and_a_deleted_ones_alert_clears(factory, hooks):
    org = await new_org(factory)
    paused = await add_pipeline(factory, org, "paused-one", status="paused")
    await set_state(factory, org, paused, "paused")
    gone = await add_pipeline(factory, org, "to-delete")
    await set_state(factory, org, gone, "failed")
    await sweep(factory, org, hooks)
    assert [a.pipeline_name for a in await alerts_of(factory, org)] == ["to-delete"]
    await delete_pipeline(factory, gone)
    assert (await sweep(factory, org, hooks)).cleared == 1


async def test_an_alert_with_no_matching_channel_waits_and_is_sent_once_a_channel_exists(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "orders")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.delivery == "no_channel"
    assert hooks.posts == []
    async with factory() as s:
        assert (await AlertService(s, org).counts()) == {"open": 0, "acknowledged": 0, "resolved": 0, "undelivered": 1}
    await channel(factory, org, "late")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.delivery == "sent"
    assert len(hooks.posts) == 1
    async with factory() as s:
        assert (await AlertService(s, org).counts())["open"] == 1


async def test_severity_floor_and_pipeline_scope_pick_the_channels(factory, hooks):
    org = await new_org(factory)
    a_id = await add_pipeline(factory, org, "a")
    b_id = await add_pipeline(factory, org, "b")
    await channel(factory, org, "high-only", url=SLACK, min_severity="high")
    await channel(factory, org, "only-b", url=TEAMS, kind="teams", scope=b_id)
    await set_state(factory, org, a_id, "degraded")                                     # medium
    await set_state(factory, org, b_id, "failed")                                       # high
    await sweep(factory, org, hooks)
    sent = {a.pipeline_name: sorted(e.action for e in a.events if e.action.startswith("sent")) for a in await alerts_of(factory, org)}
    assert sent == {"a": [], "b": ["sent to high-only", "sent to only-b"]}


async def test_a_refusing_channel_is_retried_a_few_times_then_left(factory, hooks, monkeypatch):
    monkeypatch.setattr(settings, "alerts_delivery_max_attempts", 2)
    org = await new_org(factory)
    await channel(factory, org, "broken")
    hooks.fail.add(SLACK)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    for _ in range(4):
        await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert (a.delivery, a.delivery_attempts, len(hooks.posts)) == ("failed", 2, 2)
    assert a.events[-1].action == "delivery to broken failed"
    assert a.events[-1].note == "500 boom"


async def test_a_disabled_rule_leaves_its_open_alert_alone(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    await only_rule(factory, org, "dlq_errors")                                         # pipeline-failed switched off
    await set_state(factory, org, pid, "running")
    await sweep(factory, org, hooks)
    assert [a.state for a in await alerts_of(factory, org)] == ["open"]


async def test_resolved_by_hand_while_still_failing_is_not_raised_again_until_a_new_failure(factory, hooks):
    org = await new_org(factory)
    await channel(factory, org, "ops")
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed", error="first")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    async with factory() as s, hooks.client() as c:
        await AlertService(s, org).resolve(str(a.id), actor="ann", note=None, client=c)
        await s.commit()
    await sweep(factory, org, hooks)
    await sweep(factory, org, hooks)
    assert [x.state for x in await alerts_of(factory, org)] == ["resolved"]             # same failure: not back
    await set_state(factory, org, pid, "running")
    await sweep(factory, org, hooks)
    await set_state(factory, org, pid, "failed", error="second")
    await sweep(factory, org, hooks)
    alerts = await alerts_of(factory, org)
    assert [x.state for x in alerts] == ["resolved", "open"]
    assert alerts[1].summary.endswith("second")


async def test_only_one_sweep_runs_at_a_time(factory, hooks):
    org = await new_org(factory)
    async with factory() as s:
        assert await AlertService(s, org).take_lease(now()) is True
    summary = await sweep(factory, org, hooks)
    assert summary.ran is False
    assert summary.detail == "A sweep is already running."


async def test_a_failed_deploy_raises_and_a_later_successful_one_clears(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await add_operation(factory, org, pid, "failed", minutes_ago=5, message="Kafka Connect rejected the connector configuration.")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.rule_name == "deploy-failed"
    assert a.summary == ("A deploy of pipeline 'x' failed: Kafka Connect rejected the connector "
                                                            "configuration. [KAFKA_CONNECT_CONFIG_INVALID]")
    await add_operation(factory, org, pid, "succeeded", minutes_ago=1)
    assert (await sweep(factory, org, hooks)).cleared == 1


async def test_an_error_threshold_pause_raises(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x", status="paused")
    await set_state(factory, org, pid, "paused")
    await add_threshold_pause(factory, org, pid)
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.rule_name == "error-threshold-paused"
    assert "250 dead-letter records in 5 minutes, limit 100" in a.summary


@pytest.mark.parametrize(("kind", "rule_name"), [("ingestion", "high-dlq-errors"), ("validation", "quality-failures")])
async def test_dead_letter_and_quality_counts_raise_past_the_threshold(factory, hooks, kind, rule_name):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await add_failures(factory, org, pid, kind, 60, minutes_ago=3)
    if kind == "validation":
        await add_failures(factory, org, pid, kind, 500, minutes_ago=3, key="price_range")    # per-rule keys are not records: not counted
    await sweep(factory, org, hooks)
    assert await alerts_of(factory, org) == []                                            # 60 < 100
    await add_failures(factory, org, pid, kind, 50, minutes_ago=1)
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.rule_name == rule_name
    assert a.summary.startswith("110 records")


async def test_two_simultaneous_resolves_make_one_change_and_one_follow_up(factory, hooks):
    org = await new_org(factory)
    await channel(factory, org, "ops")
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    posts_before = len(hooks.posts)

    async def resolve():
        async with factory() as s, hooks.client() as c:
            await AlertService(s, org).resolve(str(a.id), actor="ann", note=None, client=c)
            await s.commit()
    await asyncio.gather(resolve(), resolve())
    [a] = await alerts_of(factory, org)
    assert [e.action for e in a.events].count("resolved") == 1
    assert len(hooks.posts) == posts_before + 1


async def test_acknowledge_then_resolve_tells_the_channel_twice_and_resolving_twice_changes_nothing(factory, hooks):
    org = await new_org(factory)
    await channel(factory, org, "ops")
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    async with factory() as s, hooks.client() as c:
        svc_ = AlertService(s, org)
        await svc_.acknowledge(str(a.id), actor="ann", note="looking", client=c)
        await svc_.resolve(str(a.id), actor="ann", note=None, client=c)
        await svc_.resolve(str(a.id), actor="bob", note=None, client=c)
        await s.commit()
        with pytest.raises(DataConnectError) as err:
            await svc_.acknowledge(str(a.id), actor="bob", note=None, client=c)
        assert err.value.code == "ALERT_ALREADY_RESOLVED"
    texts = hooks.texts()
    assert len(texts) == 3
    assert "acknowledged by ann" in texts[1]
    assert "resolved by ann" in texts[2]


async def test_pruning_keeps_a_pipelines_newest_state(factory, hooks, monkeypatch):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    async with factory() as s:                                                          # make everything look 60 days old
        await s.execute(text("UPDATE dc_alert_observations SET occurred_at = occurred_at - interval '60 days' WHERE org_id = :o"), {"o": org})
        await s.execute(text("INSERT INTO dc_alert_observations (id, org_id, source, ref_id, pipeline_id, pipeline_name, direction, category, outcome, "
                             "occurred_at) VALUES (gen_random_uuid(), :o, 'operation', 'old-op|pipeline_deploy', :p, 'x', 'SOURCE', 'operation', "
                             "'failed', now() - interval '60 days')"), {"o": org, "p": pid})
        await s.commit()
    await sweep(factory, org, hooks)
    async with factory() as s:
        left = (await s.execute(select(AlertObservation.category).where(AlertObservation.org_id == org))).scalars().all()
    assert left == ["state"]                                                            # the old operation went; the state the rules use stayed
    assert [a.state for a in await alerts_of(factory, org)] == ["open"]                 # and the pipeline is still seen as failed


async def test_a_rule_scoped_to_one_pipeline_only_looks_at_it(factory, hooks):
    org = await new_org(factory)
    a_id = await add_pipeline(factory, org, "a")
    b_id = await add_pipeline(factory, org, "b")
    await set_state(factory, org, a_id, "failed")
    await set_state(factory, org, b_id, "failed")
    await only_rule(factory, org, "pipeline_failed", scope_pipeline_id=b_id)
    await sweep(factory, org, hooks)
    assert [a.pipeline_name for a in await alerts_of(factory, org)] == ["b"]


async def test_the_sweeper_sweeps_every_organization(factory, hooks):
    from app.services.alerts.sweeper import AlertSweeper

    orgs = [await new_org(factory) for _ in range(2)]
    for org in orgs:
        pid = await add_pipeline(factory, org, "x")
        await set_state(factory, org, pid, "failed")
    await AlertSweeper().run_once()
    for org in orgs:
        assert len(await alerts_of(factory, org)) == 1


async def test_a_rules_alerts_keep_its_name_after_the_rule_is_deleted(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed")
    await sweep(factory, org, hooks)
    async with factory() as s:
        rid = (await s.scalars(select(AlertRule.id).where(AlertRule.org_id == org, AlertRule.name == "pipeline-failed"))).one()
        await AlertService(s, org).delete_rule(str(rid))
        await s.commit()
    [a] = await alerts_of(factory, org)
    assert a.rule_id is None
    assert a.rule_name == "pipeline-failed"
    await sweep(factory, org, hooks)                                                    # a deleted rule is not evaluated: nothing changes
    assert [x.state for x in await alerts_of(factory, org)] == ["open"]
    assert timedelta(0) <= now() - a.raised_at < timedelta(minutes=5)


async def test_a_renamed_pipeline_is_reported_by_its_new_name(factory, hooks):
    org = await new_org(factory)
    pid = await add_pipeline(factory, org, "old-name")
    await set_state(factory, org, pid, "failed")
    async with factory() as s:
        await s.execute(text("UPDATE dc_pipelines SET name = 'new-name' WHERE id = :p"), {"p": pid})
        await s.commit()
    await sweep(factory, org, hooks)
    [a] = await alerts_of(factory, org)
    assert a.pipeline_name == "new-name"
    assert "'new-name'" in a.summary


async def test_follow_ups_go_only_to_the_channels_that_received_the_alert(factory, hooks):
    org = await new_org(factory)
    await channel(factory, org, "everything", url=SLACK)
    await channel(factory, org, "high-only", url=TEAMS, kind="teams", min_severity="high")      # filtered out: the alert is medium
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "degraded")
    await sweep(factory, org, hooks)
    await channel(factory, org, "added-later", url="https://hooks.slack.com/services/T9/B9/later")
    [a] = await alerts_of(factory, org)
    async with factory() as s, hooks.client() as c:
        await AlertService(s, org).acknowledge(str(a.id), actor="ann", note=None, client=c)
        await s.commit()
    assert [u for u, _ in hooks.posts] == [SLACK, SLACK]          # the alert, then its follow-up: never the filtered or the later channel
