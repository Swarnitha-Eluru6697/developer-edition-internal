"""The Acknowledge / Resolve buttons in the channel message, end to end on a real, migrated Postgres: the buttons are in the message only
when links are set up, each is signed for its alert and channel, a GET only shows a confirmation page (scanners / previews must change
nothing), the button on that page does the action as "<Teams|Slack> channel <name>", and forged / expired / unknown links are refused.

Integration tests: INTEGRATION_DATABASE_URL (they empty the alert tables first)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from alerts_support import SLACK, TEAMS, FakeSecrets, World, clean_sync
from conftest import _headers

from app.core.config import get_settings
from app.services.alerts import links, sources, webhooks
from app.services.alerts.engine import Obs

pytestmark = pytest.mark.integration

ADMIN = _headers(user_name="padmin", permissions=["workspace:access:view", "workspace:alerts:view", "workspace:alerts:edit", "workspace:alerts:manage"])
BASE = "https://ws.example.com"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    clean_sync()
    s = get_settings()
    monkeypatch.setattr(s, "alerts_public_url", BASE)
    monkeypatch.setattr(s, "alerts_action_secret", "integration-secret-" * 3)
    fake = FakeSecrets()
    from app.services.alerts import service
    monkeypatch.setattr(service, "get_secrets_backend", lambda: fake)
    world = World(datetime.now(timezone.utc))
    sent: list[tuple[str, webhooks.Message]] = []

    async def runner(known, **kw):
        return sources.SourceResult("dbt-runner", True, "", list(world.runs)), {o.job_key: (o.job_name, o.job_kind) for o in world.runs}

    async def none(**kw):
        return sources.SourceResult("flink", True, "", [])

    async def dagster(known, names, **kw):
        return sources.SourceResult("dagster", True, "", [])

    async def fake_send(kind, url, message, *, client=None):
        sent.append((kind, message))
        return webhooks.SendResult(True, "delivered")

    async def public(host):
        return None
    monkeypatch.setattr(sources, "collect_runner", runner)
    monkeypatch.setattr(sources, "collect_flink", none)
    monkeypatch.setattr(sources, "collect_dagster", dagster)
    monkeypatch.setattr(webhooks, "send", fake_send)
    monkeypatch.setattr(webhooks, "assert_host_allowed", public)
    return type("Env", (), {"world": world, "sent": sent})()


def raise_alert(client, env, detail="boom", channels=(("teams-ops", "teams", TEAMS),)):
    for name, kind, url in channels:
        r = client.post("/api/v1/alerts/channels", json={"name": name, "kind": kind, "webhook_url": url}, headers=ADMIN)
        assert r.status_code == 201, r.text
    env.world.runs.append(Obs("j1", "customers", "dbt", "run", "failed", env.world.now, "run-1", None, detail))
    assert client.post("/api/v1/alerts/rules/run", headers=ADMIN).json()["delivered"] == 1
    return client.get("/api/v1/alerts?state=open", headers=ADMIN).json()["items"][0]


def token_of(message: webhooks.Message, label: str) -> str:
    return next(b.url for b in message.buttons if b.label == label).rsplit("/", 1)[1]


def state_of(client, alert_id) -> dict:
    return client.get(f"/api/v1/alerts/{alert_id}", headers=ADMIN).json()


# ── the message ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_message_has_acknowledge_and_resolve_buttons_signed_for_this_alert_and_channel(client, env):
    alert = raise_alert(client, env)
    kind, message = env.sent[-1]
    assert kind == "teams" and [b.label for b in message.buttons] == ["Acknowledge", "Resolve"]
    assert [b.style for b in message.buttons] == ["default", "positive"]
    assert all(b.url.startswith(f"{BASE}/api/v1/alerts/actions/") for b in message.buttons)
    ack = links.read_token(token_of(message, "Acknowledge"))
    assert (ack.alert_id, ack.action, ack.channel_kind, ack.channel_name) == (alert["id"], "acknowledge", "teams", "teams-ops")
    assert links.read_token(token_of(message, "Resolve")).action == "resolve"
    assert dict(message.facts)["State"] == "open" and message.heading == "job-run-failed"


def test_every_channel_gets_its_own_links(client, env):
    raise_alert(client, env, channels=(("teams-ops", "teams", TEAMS), ("slack-ops", "slack", SLACK)))
    by_kind = {k: m for k, m in env.sent}
    assert links.read_token(token_of(by_kind["teams"], "Resolve")).channel_name == "teams-ops"
    assert links.read_token(token_of(by_kind["slack"], "Resolve")).channel_name == "slack-ops"
    assert token_of(by_kind["teams"], "Resolve") != token_of(by_kind["slack"], "Resolve")


def test_without_a_public_url_or_secret_there_are_no_buttons(client, env, monkeypatch):
    monkeypatch.setattr(get_settings(), "alerts_action_secret", None)
    raise_alert(client, env)
    assert env.sent[-1][1].buttons == ()


def test_the_test_message_has_no_buttons(client, env):
    cid = client.post("/api/v1/alerts/channels", json={"name": "t", "kind": "teams", "webhook_url": TEAMS}, headers=ADMIN).json()["id"]
    client.post(f"/api/v1/alerts/channels/{cid}/test", headers=ADMIN)
    assert env.sent[-1][1].buttons == ()


# ── the page ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_opening_the_link_only_shows_a_confirmation_and_changes_nothing(client, env):
    alert = raise_alert(client, env)
    token = token_of(env.sent[-1][1], "Acknowledge")
    for _ in range(3):                                                       # a link scanner / preview fetching it, repeatedly
        r = client.get(f"/api/v1/alerts/actions/{token}")                    # NO login headers: the token is the credential
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "Acknowledge this alert?" in r.text and 'method="post"' in r.text and "job-run-failed" not in r.text.replace("customers", "")
    assert state_of(client, alert["id"])["state"] == "open"
    assert [e["action"] for e in state_of(client, alert["id"])["events"]] == ["raised", "sent to teams-ops"]


def test_the_button_on_the_page_acknowledges_as_the_channel_and_says_so(client, env):
    alert = raise_alert(client, env)
    r = client.post(f"/api/v1/alerts/actions/{token_of(env.sent[-1][1], 'Acknowledge')}")
    assert r.status_code == 200 and "Alert acknowledged" in r.text
    detail = state_of(client, alert["id"])
    assert detail["state"] == "acknowledged"
    last = detail["events"][-1]
    assert (last["action"], last["actor"]) == ("acknowledged", "Teams channel teams-ops") and "button" in last["note"]
    assert client.get("/api/v1/alerts/counts", headers=ADMIN).json()["acknowledged"] == 1


def test_resolve_works_from_open_and_from_acknowledged_and_a_second_click_is_harmless(client, env):
    alert = raise_alert(client, env)
    message = env.sent[-1][1]
    client.post(f"/api/v1/alerts/actions/{token_of(message, 'Acknowledge')}")
    client.post(f"/api/v1/alerts/actions/{token_of(message, 'Acknowledge')}")                      # again
    assert [e["action"] for e in state_of(client, alert["id"])["events"]].count("acknowledged") == 1
    r = client.post(f"/api/v1/alerts/actions/{token_of(message, 'Resolve')}")
    assert "Alert resolved" in r.text and state_of(client, alert["id"])["state"] == "resolved"
    again = client.post(f"/api/v1/alerts/actions/{token_of(message, 'Resolve')}")
    assert again.status_code == 200 and "Already resolved" in again.text
    ack_after = client.get(f"/api/v1/alerts/actions/{token_of(message, 'Acknowledge')}")
    assert "Already resolved" in ack_after.text and "<form" not in ack_after.text
    assert [e["action"] for e in state_of(client, alert["id"])["events"]].count("resolved") == 1


def test_the_action_is_in_the_audit_log_as_a_channel_action_with_no_user(client, env):
    import asyncio
    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models import AuditLog
    alert = raise_alert(client, env)
    client.post(f"/api/v1/alerts/actions/{token_of(env.sent[-1][1], 'Resolve')}")

    async def rows():
        async with AsyncSessionLocal() as db:
            return (await db.scalars(select(AuditLog).where(AuditLog.action == "alert.resolve", AuditLog.resource_id == uuid.UUID(alert["id"])))).all()
    [row] = asyncio.run(rows())
    assert row.user_id is None and row.metadata_["via"] == "channel link" and row.metadata_["channel"] == "teams-ops"


@pytest.mark.parametrize("junk", ["abc", "abc.def", "x" * 300, "e30.e30"])
def test_a_forged_or_junk_link_is_a_plain_400_and_does_nothing(client, env, junk):
    alert = raise_alert(client, env)
    for r in (client.get(f"/api/v1/alerts/actions/{junk}"), client.post(f"/api/v1/alerts/actions/{junk}")):
        assert r.status_code == 400 and "cannot be used" in r.text
    assert state_of(client, alert["id"])["state"] == "open"


def test_a_link_with_the_action_changed_is_refused(client, env):
    alert = raise_alert(client, env)
    payload, sig = token_of(env.sent[-1][1], "Acknowledge").split(".")
    import base64, json
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["x"] = "resolve"
    forged = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=").decode() + "." + sig
    assert client.post(f"/api/v1/alerts/actions/{forged}").status_code == 400
    assert state_of(client, alert["id"])["state"] == "open"


def test_an_expired_link_is_a_410(client, env, monkeypatch):
    alert = raise_alert(client, env)
    token = token_of(env.sent[-1][1], "Resolve")
    import time
    real = time.time()
    monkeypatch.setattr(links.time, "time", lambda: real + 8 * 86_400)
    r = client.post(f"/api/v1/alerts/actions/{token}")
    assert r.status_code == 410 and "expired" in r.text
    assert state_of(client, alert["id"])["state"] == "open"


def test_a_link_to_a_deleted_alert_is_a_404_page(client, env):
    token = links.make_token("00000000-0000-0000-0000-000000000abc", "resolve", "teams", "c")
    r = client.get(f"/api/v1/alerts/actions/{token}")
    assert r.status_code == 404 and "no longer exists" in r.text


def test_links_switched_off_refuse_even_a_genuine_token(client, env, monkeypatch):
    raise_alert(client, env)
    token = token_of(env.sent[-1][1], "Resolve")
    monkeypatch.setattr(get_settings(), "alerts_action_secret", None)
    assert client.post(f"/api/v1/alerts/actions/{token}").status_code == 400


def test_whatever_the_alert_says_is_escaped_in_the_page_and_the_page_is_locked_down(client, env):
    raise_alert(client, env, detail='<script>alert("x")</script> & "quotes"')
    token = token_of(env.sent[-1][1], "Acknowledge")
    r = client.get(f"/api/v1/alerts/actions/{token}")
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text
    h = r.headers
    assert h["cache-control"] == "no-store" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in h["content-security-policy"] and "form-action 'self'" in h["content-security-policy"]


def test_the_page_is_not_part_of_the_api_document(client):
    paths = client.get("/openapi.json", headers=ADMIN).json()["paths"]
    assert not any("/alerts/actions" in p for p in paths)


def test_pressing_a_button_in_the_channel_posts_a_status_update_back_to_it(client, env):
    alert = raise_alert(client, env)
    before = len(env.sent)
    client.post(f"/api/v1/alerts/actions/{token_of(env.sent[-1][1], 'Acknowledge')}")
    [(kind, message)] = env.sent[before:]
    assert kind == "teams" and message.tone == "ack"
    assert dict(message.facts)["By"] == "Teams channel teams-ops" and dict(message.facts)["State"] == "acknowledged"
    client.post(f"/api/v1/alerts/actions/{token_of(env.sent[0][1], 'Resolve')}")
    assert env.sent[-1][1].tone == "resolved" and env.sent[-1][1].buttons == ()          # nothing left to do once it is resolved
    assert state_of(client, alert["id"])["state"] == "resolved"


def test_the_acknowledged_follow_up_carries_a_resolve_button_that_resolves_the_alert(client, env):
    alert = raise_alert(client, env)
    client.post(f"/api/v1/alerts/actions/{token_of(env.sent[-1][1], 'Acknowledge')}")
    follow_up = env.sent[-1][1]
    assert follow_up.tone == "ack" and [(b.label, b.style) for b in follow_up.buttons] == [("Resolve", "positive")]   # not Acknowledge again
    claims = links.read_token(token_of(follow_up, "Resolve"))
    assert (claims.alert_id, claims.action, claims.channel_name) == (alert["id"], "resolve", "teams-ops")
    assert state_of(client, alert["id"])["state"] == "acknowledged"
    r = client.post(f"/api/v1/alerts/actions/{token_of(follow_up, 'Resolve')}")
    assert "Alert resolved" in r.text and state_of(client, alert["id"])["state"] == "resolved"
    assert [e["action"] for e in state_of(client, alert["id"])["events"]][-2:] == ["acknowledged", "resolved"]


def test_acknowledging_from_the_workspace_also_gives_the_channel_a_resolve_button(client, env):
    alert = raise_alert(client, env)
    client.post(f"/api/v1/alerts/{alert['id']}/acknowledge", json={}, headers=ADMIN)
    assert [b.label for b in env.sent[-1][1].buttons] == ["Resolve"]


def test_resolving_straight_from_open_skips_acknowledge_and_is_final(client, env):
    alert = raise_alert(client, env)
    client.post(f"/api/v1/alerts/actions/{token_of(env.sent[-1][1], 'Resolve')}")
    detail = state_of(client, alert["id"])
    assert detail["state"] == "resolved" and [e["action"] for e in detail["events"]] == ["raised", "sent to teams-ops", "resolved"]
    assert env.sent[-1][1].tone == "resolved" and env.sent[-1][1].buttons == ()


def test_an_acknowledged_follow_up_without_links_set_up_has_no_buttons(client, env, monkeypatch):
    alert = raise_alert(client, env)
    monkeypatch.setattr(get_settings(), "alerts_action_secret", None)
    client.post(f"/api/v1/alerts/{alert['id']}/acknowledge", json={}, headers=ADMIN)
    assert env.sent[-1][1].tone == "ack" and env.sent[-1][1].buttons == ()
