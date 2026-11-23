"""The alerts API and the Acknowledge / Resolve link pages, through the real routes, against a REAL PostgreSQL (DATACONNECT_REAL_DB_TESTS=1):
who may do what, the webhook URL never coming back, error shapes, Send test, the full alert lifecycle, and the signed links (GET changes nothing,
POST acts as the channel, forged / expired links, the follow-up's Resolve button, not in the OpenAPI document, audit rows)."""

import re
from urllib.parse import unquote

import pytest
from alerts_real_db import (  # noqa: F401 - the fixtures are found by pytest under their own names: factory, hooks, scratch_url
    SLACK,
    TEAMS,
    add_pipeline,
    factory_fixture,
    hooks_fixture,
    new_org,
    scratch_url_fixture,
    set_state,
    skip_without_db,
)
from conftest import gateway_headers
from fastapi.testclient import TestClient
from personas import PERSONAS
from sqlalchemy import text

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.services.alerts.service import AlertService

pytestmark = skip_without_db
P = settings.api_prefix


@pytest.fixture
def api(factory, monkeypatch):
    async def session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise
    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(settings, "alerts_public_url", "https://dc.example.com")
    monkeypatch.setattr(settings, "alerts_action_secret", "x" * 40)
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def h(org, persona="admin", user="ann"):
    # The tenant and the actor come from the gateway principal (x-tenant-id / x-user-name).
    return gateway_headers(permissions=PERSONAS[persona], username=user, tenant_id=org)


async def failed_alert(factory, hooks, org, name="orders-cdc"):
    pid = await add_pipeline(factory, org, name)
    await set_state(factory, org, pid, "failed", error="Kafka Connect reports the connector FAILED: boom")
    async with factory() as s, hooks.client() as c:
        await AlertService(s, org).sweep(client=c)


# ── who may do what ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_everyone_can_read_only_admin_manages_and_integrators_may_acknowledge(factory, hooks, api):
    org = await new_org(factory)
    for role in ("admin", "integrator", "viewer"):
        assert api.get(f"{P}/alerts/rules", headers=h(org, role)).status_code == 200
        assert api.get(f"{P}/alerts?state=open", headers=h(org, role)).status_code == 200
    rule = {"name": "loud", "kind": "dlq_errors", "severity": "high", "threshold": 5, "window_seconds": 900}
    assert api.post(f"{P}/alerts/rules", json=rule, headers=h(org, "integrator")).status_code == 403
    assert api.post(f"{P}/alerts/rules", json=rule, headers=h(org, "viewer")).status_code == 403
    assert api.post(f"{P}/alerts/channels", json={"name": "c", "kind": "slack", "webhook_url": SLACK}, headers=h(org, "integrator")).status_code == 403
    assert api.post(f"{P}/alerts/rules", json=rule, headers=h(org, "admin")).status_code == 201

    await failed_alert(factory, hooks, org)
    async with factory() as s:
        aid = (await s.execute(text("SELECT id FROM dc_alerts WHERE org_id = :o"), {"o": org})).scalar()
    assert api.post(f"{P}/alerts/{aid}/acknowledge", json={}, headers=h(org, "viewer")).status_code == 403
    r = api.post(f"{P}/alerts/{aid}/acknowledge", json={"note": "on it"}, headers=h(org, "integrator", "ivan"))
    assert r.status_code == 200
    assert r.json()["state"] == "acknowledged"
    assert r.json()["last_actor"] == "ivan"


async def test_another_organizations_alerts_rules_and_channels_are_invisible(factory, hooks, api):
    org, other = await new_org(factory), await new_org(factory)
    await failed_alert(factory, hooks, org)
    cid = api.post(f"{P}/alerts/channels", json={"name": "c", "kind": "slack", "webhook_url": SLACK}, headers=h(org)).json()["id"]
    async with factory() as s:
        aid = (await s.execute(text("SELECT id FROM dc_alerts WHERE org_id = :o"), {"o": org})).scalar()
    assert api.get(f"{P}/alerts/{aid}", headers=h(other)).status_code == 404
    assert api.post(f"{P}/alerts/{aid}/resolve", json={}, headers=h(other)).status_code == 404
    assert api.delete(f"{P}/alerts/channels/{cid}", headers=h(other)).status_code == 404
    assert api.get(f"{P}/alerts/counts", headers=h(other)).json() == {"open": 0, "acknowledged": 0, "resolved": 0, "undelivered": 0}


# ── channels ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_the_webhook_url_goes_in_and_never_comes_back(factory, hooks, api):
    org = await new_org(factory)
    r = api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK, "min_severity": "medium"}, headers=h(org))
    assert r.status_code == 201
    body = r.json()
    assert body["url_hint"] == "hooks.slack.com/…/XXXX"
    assert "webhook_url" not in body
    assert "secret" not in r.text.lower()
    for resp in (api.get(f"{P}/alerts/channels", headers=h(org)), api.patch(f"{P}/alerts/channels/{body['id']}", json={"name": "renamed"}, headers=h(org))):
        assert resp.status_code == 200
        assert "XXXXsecretXXXX" not in resp.text
    async with factory() as s:
        audit = (await s.execute(text("SELECT string_agg(coalesce(after_state::text, ''), ' ') FROM dc_audit_logs WHERE org_id = :o"), {"o": org})).scalar()
    assert "XXXXsecretXXXX" not in audit
    assert "hooks.slack.com/…/XXXX" in audit


async def test_errors_name_the_field_and_carry_a_stable_code(factory, hooks, api):
    org = await new_org(factory)
    bad = api.post(f"{P}/alerts/channels", json={"name": "x", "kind": "teams", "webhook_url": SLACK}, headers=h(org))
    assert bad.status_code == 422
    assert bad.json()["field_errors"][0]["field"] == "webhook_url"
    api.post(f"{P}/alerts/channels", json={"name": "Ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    dup = api.post(f"{P}/alerts/channels", json={"name": "ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    assert dup.status_code == 409
    assert dup.json()["code"] == "ALERT_CHANNEL_NAME_TAKEN"
    assert dup.json()["field_errors"][0]["field"] == "name"
    too_big = api.post(f"{P}/alerts/rules", json={"name": "r", "kind": "pipeline_failed", "threshold": 3}, headers=h(org))
    assert too_big.status_code == 422
    assert too_big.json()["field_errors"][0]["field"] == "threshold"
    assert api.get(f"{P}/alerts/not-a-uuid", headers=h(org)).status_code == 404


async def test_send_test_reports_the_result_on_the_card(factory, hooks, api):
    org = await new_org(factory)
    cid = api.post(f"{P}/alerts/channels", json={"name": "c", "kind": "slack", "webhook_url": SLACK}, headers=h(org)).json()["id"]
    ok = api.post(f"{P}/alerts/channels/{cid}/test", headers=h(org)).json()
    assert ok["ok"] is True
    assert ok["channel"]["last_ok"] is True
    assert "Test alert from InAiEra DataConnect" in hooks.texts()[0]
    hooks.fail.add(SLACK)
    bad = api.post(f"{P}/alerts/channels/{cid}/test", headers=h(org)).json()
    assert bad["ok"] is False
    assert bad["detail"] == "500 boom"
    assert bad["channel"]["last_detail"] == "500 boom"


# ── the alert lifecycle through the API ─────────────────────────────────────────────────────────────────────────────────────────
async def test_run_rules_now_then_the_tabs_counts_and_history(factory, hooks, api):
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    pid = await add_pipeline(factory, org, "orders-cdc")
    await set_state(factory, org, pid, "failed", error="boom")
    run = api.post(f"{P}/alerts/rules/run", headers=h(org)).json()
    assert run["ran"]
    assert run["raised"] == 1
    assert run["delivered"] == 1
    assert "runtime" in run["sources"]
    assert api.get(f"{P}/alerts/counts", headers=h(org)).json() == {"open": 1, "acknowledged": 0, "resolved": 0, "undelivered": 0}
    page = api.get(f"{P}/alerts?state=open", headers=h(org)).json()
    [a] = page["items"]
    assert (a["pipeline_name"], a["rule_name"], a["direction"], a["delivery"]) == ("orders-cdc", "pipeline-failed", "SOURCE", "sent")
    api.post(f"{P}/alerts/{a['id']}/resolve", json={"note": "restarted the database"}, headers=h(org, user="bob"))
    detail = api.get(f"{P}/alerts/{a['id']}", headers=h(org)).json()
    assert [e["action"] for e in detail["events"]] == ["raised", "sent to slack-ops", "resolved"]
    assert detail["events"][-1]["actor"] == "bob"
    assert detail["events"][-1]["note"] == "restarted the database"
    assert api.get(f"{P}/alerts?state=resolved", headers=h(org)).json()["total"] == 1
    # still failed, but resolved by hand with nothing newer: reported as such, and the next sweep does not raise it again
    assert [(b["pipeline_name"], b["state"]) for b in api.get(f"{P}/alerts/rules/breaching", headers=h(org)).json()] == [
        ("orders-cdc", "resolved_by_hand")]
    assert api.post(f"{P}/alerts/rules/run", headers=h(org)).json()["raised"] == 0


# ── the signed Acknowledge / Resolve links ──────────────────────────────────────────────────────────────────────────────────────
def buttons(hooks) -> dict[str, str]:
    """label -> url of the link buttons in the newest Slack message."""
    import json

    blocks = json.loads(hooks.texts()[-1])["attachments"][0]["blocks"]
    actions = next((b for b in blocks if b["type"] == "actions"), {"elements": []})
    return {e["text"]["text"]: e["url"] for e in actions["elements"]}


async def test_each_channel_gets_its_own_signed_buttons_and_get_changes_nothing(factory, hooks, api):
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    await failed_alert(factory, hooks, org)
    b = buttons(hooks)
    assert set(b) == {"Acknowledge", "Resolve"}
    assert all(u.startswith(f"https://dc.example.com{P}/alerts/actions/") for u in b.values())
    path = b["Acknowledge"].removeprefix("https://dc.example.com")
    page = api.get(path)                                                  # no gateway headers: the person is in Slack
    assert page.status_code == 200
    assert "Acknowledge this alert?" in page.text
    assert "<form method=\"post\">" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert api.get(f"{P}/alerts/counts", headers=h(org)).json()["open"] == 1          # still open: GET changes nothing


async def test_post_acts_as_the_channel_and_the_follow_up_carries_only_resolve(factory, hooks, api):
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    await failed_alert(factory, hooks, org)
    first = buttons(hooks)
    done = api.post(first["Acknowledge"].removeprefix("https://dc.example.com"))
    assert done.status_code == 200
    assert "Alert acknowledged" in done.text
    again = api.post(first["Acknowledge"].removeprefix("https://dc.example.com"))
    assert "Already acknowledged" in again.text                          # a second click changes nothing
    follow_up = buttons(hooks)
    assert list(follow_up) == ["Resolve"]
    assert "acknowledged by Slack channel slack-ops" in unquote(hooks.texts()[-1])
    api.post(follow_up["Resolve"].removeprefix("https://dc.example.com"))
    async with factory() as s:
        actions = (await s.execute(text("SELECT e.action, e.actor FROM dc_alert_events e JOIN dc_alerts a ON a.id = e.alert_id "
                                        "WHERE a.org_id = :o ORDER BY e.at"), {"o": org})).all()
        audit = (await s.execute(text("SELECT action, actor FROM dc_audit_logs WHERE org_id = :o AND resource_type = 'alert' ORDER BY created_at"),
                                 {"o": org})).all()
    assert [(a, b) for a, b in actions if a in ("acknowledged", "resolved")] == [
        ("acknowledged", "Slack channel slack-ops"), ("resolved", "Slack channel slack-ops")]
    assert [tuple(r) for r in audit] == [("acknowledged", "Slack channel slack-ops"), ("resolved", "Slack channel slack-ops")]


async def test_forged_expired_and_unknown_links_are_refused(factory, hooks, api, monkeypatch):
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    await failed_alert(factory, hooks, org)
    url = buttons(hooks)["Resolve"].removeprefix("https://dc.example.com")
    token = url.rsplit("/", 1)[1]
    payload, sig = token.split(".")
    assert api.post(f"{P}/alerts/actions/{payload}.{sig[:-2]}AA").status_code == 400
    assert api.get(f"{P}/alerts/actions/garbage").status_code == 400
    monkeypatch.setattr(settings, "alerts_action_link_ttl_seconds", -10)
    from app.services.alerts import links

    expired = links.make_token(re.search(r'"a":"([^"]+)"', __import__("base64").urlsafe_b64decode(payload + "==").decode()).group(1),
                               "resolve", "slack", "slack-ops")
    assert api.get(f"{P}/alerts/actions/{expired}").status_code == 410
    monkeypatch.setattr(settings, "alerts_action_link_ttl_seconds", 3600)
    unknown = links.make_token("00000000-0000-0000-0000-000000000000", "resolve", "slack", "x")    # genuine, but no such alert
    assert api.get(f"{P}/alerts/actions/{unknown}").status_code == 404
    assert api.get(f"{P}/alerts/counts", headers=h(org)).json()["open"] == 1          # nothing was resolved


async def test_without_the_secret_there_are_no_buttons_and_no_link_works(factory, hooks, api, monkeypatch):
    monkeypatch.setattr(settings, "alerts_action_secret", None)
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    await failed_alert(factory, hooks, org)
    assert buttons(hooks) == {}


async def test_alert_text_on_the_confirm_page_is_escaped(factory, hooks, api):
    org = await new_org(factory)
    api.post(f"{P}/alerts/channels", json={"name": "slack-ops", "kind": "slack", "webhook_url": SLACK}, headers=h(org))
    pid = await add_pipeline(factory, org, "x")
    await set_state(factory, org, pid, "failed", error="<script>alert(1)</script>")
    async with factory() as s, hooks.client() as c:
        await AlertService(s, org).sweep(client=c)
    page = api.get(buttons(hooks)["Resolve"].removeprefix("https://dc.example.com")).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_link_route_is_not_in_the_openapi_document_but_the_api_is():
    paths = app.openapi()["paths"]
    assert not any("/actions/" in p for p in paths)
    assert f"{P}/alerts/channels/{{channel_id}}/test" in paths
    assert f"{P}/alerts/{{alert_id}}/acknowledge" in paths
