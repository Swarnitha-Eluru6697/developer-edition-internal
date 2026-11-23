"""A card posted through a webhook cannot be edited, so when an alert is acknowledged, resolved or clears by itself the channels that
RECEIVED it get a short follow-up message. Real, migrated Postgres (INTEGRATION_DATABASE_URL); the receiver plays Slack / Teams."""
from __future__ import annotations

import pytest
from alerts_support import SLACK, TEAMS, db, receiver, secrets, world  # noqa: F401  (fixtures)
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Alert, AlertEvent
from app.services.alerts import webhooks
from app.services.alerts.service import AlertService

pytestmark = pytest.mark.integration

FLOW = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"       # a Teams Workflows webhook


async def channel(svc, name, kind="slack", url=SLACK, **kw):
    ch = await svc.create_channel(actor="admin", name=name, kind=kind, webhook_url=url, **{"min_severity": "low", "enabled": True, **kw})
    await svc.db.commit()
    return ch


async def raised(svc, world, receiver, *channels):
    world.run("failed", detail="boom")
    await svc.sweep(client=receiver.client)
    return (await svc.db.scalars(select(Alert))).one()


def heading(body: dict) -> str:
    return body["attachments"][0]["blocks"][0]["text"]["text"]


async def test_acknowledging_tells_every_channel_that_received_the_alert(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "#ops")
    await channel(svc, "Teams ops", "teams", FLOW)
    a = await raised(svc, world, receiver)
    before = len(receiver.posts)
    await svc.acknowledge(a.id, actor="padmin", note=None, client=receiver.client)
    new = receiver.posts[before:]
    assert len(new) == 2
    slack = next(b for u, b in new if "hooks.slack.com" in u)
    assert heading(slack) == "*👀 job-run-failed: acknowledged*"
    assert slack["attachments"][0]["color"] == "#2563EB"
    facts = {f["text"].split("\n")[0].strip("*"): f["text"].split("\n")[1] for f in slack["attachments"][0]["blocks"][1]["fields"]}
    assert facts == {"Job": "customers", "Rule": "job-run-failed", "State": "acknowledged", "By": "padmin"}
    assert all(b["type"] != "actions" for b in slack["attachments"][0]["blocks"])              # no buttons on a status update
    teams = next(b for u, b in new if "logic.azure.com" in u)["attachments"][0]["content"]
    assert teams["body"][0]["text"] == "👀 job-run-failed: acknowledged" and teams["body"][0]["color"] == "Accent"
    assert "actions" not in teams


async def test_resolving_says_so_in_green(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "Teams ops", "teams", FLOW)
    a = await raised(svc, world, receiver)
    before = len(receiver.posts)
    await svc.resolve(a.id, actor="Teams channel Teams ops", note="fixed upstream", client=receiver.client)
    [(_, body)] = receiver.posts[before:]
    card = body["attachments"][0]["content"]
    assert card["body"][0]["text"] == "✅ job-run-failed: resolved" and card["body"][0]["color"] == "Good"
    assert "was resolved by Teams channel Teams ops. Note: fixed upstream" in card["body"][2]["text"]


async def test_a_note_that_only_says_it_was_a_button_click_is_not_repeated(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    await svc.acknowledge(a.id, actor="Slack channel #ops", note="via the button in the channel message", client=receiver.client)
    summary = receiver.posts[-1][1]["attachments"][0]["blocks"][2]["text"]["text"]
    assert summary == "job-run-failed on customers was acknowledged by Slack channel #ops." and "Note" not in summary


async def test_an_alert_that_clears_by_itself_is_announced_as_cleared_automatically(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "#ops")
    await raised(svc, world, receiver)
    before = len(receiver.posts)
    world.run("succeeded", 0)
    res = await svc.sweep(client=receiver.client)
    assert res.cleared == 1
    [(_, body)] = receiver.posts[before:]
    assert heading(body) == "*✅ job-run-failed: cleared automatically*"
    assert body["attachments"][0]["color"] == "#16A34A"
    assert "was cleared automatically." in body["attachments"][0]["blocks"][2]["text"]["text"]


async def test_only_channels_that_received_the_alert_hear_about_it(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)                                    # sent to #ops only
    await channel(svc, "added-later", "teams", FLOW)                          # did not exist when it was sent
    await channel(svc, "other-job", "teams", TEAMS, scope_job_key="zzz", scope_job_name="not this job")
    before = len(receiver.posts)
    await svc.acknowledge(a.id, actor="padmin", note=None, client=receiver.client)
    assert [u for u, _ in receiver.posts[before:]] == [SLACK]


async def test_nothing_is_posted_twice_for_one_change_and_nothing_for_a_refused_one(db, secrets, world, receiver):
    from app.core.errors import WorkspaceError
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    await svc.acknowledge(a.id, actor="x", note=None, client=receiver.client)
    n = len(receiver.posts)
    await svc.acknowledge(a.id, actor="x", note=None, client=receiver.client)                  # already acknowledged: no change
    assert len(receiver.posts) == n
    await svc.resolve(a.id, actor="x", note=None, client=receiver.client)
    n = len(receiver.posts)
    await svc.resolve(a.id, actor="x", note=None, client=receiver.client)                      # already resolved
    with pytest.raises(WorkspaceError):
        await svc.acknowledge(a.id, actor="x", note=None, client=receiver.client)              # refused: resolved
    assert len(receiver.posts) == n


async def test_an_alert_no_channel_ever_received_resolves_quietly(db, secrets, world, receiver):
    svc = AlertService(db)
    world.run("failed")
    await svc.sweep()                                                         # no channel exists
    [a] = (await db.scalars(select(Alert))).all()
    await channel(svc, "#late")
    await svc.resolve(a.id, actor="x", note=None, client=receiver.client)
    assert receiver.posts == []


async def test_a_channel_that_cannot_be_reached_never_blocks_the_action_and_is_noted_in_the_history(db, secrets, world, receiver):
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    receiver.status, receiver.text = 500, "boom"
    done = await svc.acknowledge(a.id, actor="padmin", note=None, client=receiver.client)
    assert done.state == "acknowledged"
    events = (await db.scalars(select(AlertEvent).where(AlertEvent.alert_id == a.id).order_by(AlertEvent.at))).all()
    assert events[-1].action == "status update to #ops failed" and events[-1].note == "500 boom"


async def test_a_disabled_channel_is_not_told(db, secrets, world, receiver):
    svc = AlertService(db)
    ch = await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    await svc.update_channel(ch.id, {"enabled": False})
    await db.commit()
    before = len(receiver.posts)
    await svc.acknowledge(a.id, actor="x", note=None, client=receiver.client)
    assert len(receiver.posts) == before


async def test_status_updates_can_be_switched_off(db, secrets, world, receiver, monkeypatch):
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    monkeypatch.setattr(get_settings(), "alerts_status_updates", False)
    before = len(receiver.posts)
    await svc.acknowledge(a.id, actor="x", note=None, client=receiver.client)
    assert len(receiver.posts) == before


def test_the_status_message_is_coloured_by_what_happened_not_by_severity():
    m = webhooks.Message("t", "high", "s", (), heading="rule: resolved", tone="resolved")
    assert m.head == "✅ rule: resolved" and m.hex == "#16A34A"
    assert webhooks.Message("t", "high", "s", (), tone="ack").hex == "#2563EB"
    assert webhooks.Message("t", "high", "s", ()).hex == "#DC2626" and webhooks.Message("t", "high", "s", ()).head == "🚨 t"
    classic = webhooks.body_for("teams", TEAMS, m)
    assert classic["themeColor"] == "16A34A" and classic["title"] == "✅ rule: resolved"


async def test_two_clicks_at_the_same_moment_make_one_change_and_one_follow_up(db, secrets, world, receiver):
    """A double click on the confirm button, or the same link opened twice: both read "open". The database lets exactly one win."""
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from alerts_support import URL
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    before = len(receiver.posts)
    engine = create_async_engine(URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def click(who):
        async with maker() as session:
            done = await AlertService(session).acknowledge(a.id, actor=who, note=None, client=receiver.client)
            await session.commit()
            return done.state
    states = await asyncio.gather(click("first"), click("second"))
    await engine.dispose()
    assert states == ["acknowledged", "acknowledged"]
    events = (await db.scalars(select(AlertEvent).where(AlertEvent.alert_id == a.id, AlertEvent.action == "acknowledged"))).all()
    assert len(events) == 1
    assert len(receiver.posts) - before == 1                                        # one follow-up, not two


async def test_two_resolves_at_the_same_moment_make_one_change(db, secrets, world, receiver):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from alerts_support import URL
    svc = AlertService(db)
    await channel(svc, "#ops")
    a = await raised(svc, world, receiver)
    before = len(receiver.posts)
    engine = create_async_engine(URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def click(who):
        async with maker() as session:
            await AlertService(session).resolve(a.id, actor=who, note=None, client=receiver.client)
            await session.commit()
    await asyncio.gather(click("first"), click("second"))
    await engine.dispose()
    events = (await db.scalars(select(AlertEvent).where(AlertEvent.alert_id == a.id, AlertEvent.action == "resolved"))).all()
    assert len(events) == 1 and len(receiver.posts) - before == 1
