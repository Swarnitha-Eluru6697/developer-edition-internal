"""The alerts API on a real, migrated Postgres: who may do what, what a response may contain (never a webhook URL), and the flows the
page uses (channels, rules, alerts, acknowledge / resolve, test send, run rules now).

Integration tests: INTEGRATION_DATABASE_URL (they empty the alert tables first)."""
from __future__ import annotations

import pytest
from alerts_support import SLACK, TEAMS, FakeSecrets, World, clean_sync
from conftest import _headers

from app.services.alerts import sources, webhooks
from app.services.alerts.engine import Obs

pytestmark = pytest.mark.integration

# Callers are described by the permissions the gateway hands over, never by role names (contract 01-CONTRACT.md).
OPEN = ["workspace:access:view"]
ADMIN = _headers(user_name="padmin", permissions=OPEN + ["workspace:alerts:view", "workspace:alerts:edit", "workspace:alerts:manage"])
OPERATOR = _headers(user_name="trans", permissions=OPEN + ["workspace:alerts:view", "workspace:alerts:edit"])
VIEWER = _headers(user_name="ana", permissions=OPEN + ["workspace:alerts:view"])
NOBODY = _headers(user_name="aud", permissions=OPEN + ["workspace:dq:view"])


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from datetime import datetime, timezone
    clean_sync()
    fake = FakeSecrets()
    from app.services.alerts import service
    monkeypatch.setattr(service, "get_secrets_backend", lambda: fake)
    world = World(datetime.now(timezone.utc))
    sent: list[tuple[str, str, str]] = []

    async def runner(known, **kw):
        return sources.SourceResult("dbt-runner", True, "", list(world.runs)), {o.job_key: (o.job_name, o.job_kind) for o in world.runs}

    async def flink(**kw):
        return sources.SourceResult("flink", True, "", [])

    async def dagster(known, names, **kw):
        return sources.SourceResult("dagster", True, "", [])

    async def fake_send(kind, url, message, *, client=None):
        sent.append((kind, url, message.title))
        return webhooks.SendResult(world_state["ok"], "delivered" if world_state["ok"] else "404 no_service")
    world_state = {"ok": True}
    monkeypatch.setattr(sources, "collect_runner", runner)
    monkeypatch.setattr(sources, "collect_flink", flink)
    monkeypatch.setattr(sources, "collect_dagster", dagster)
    monkeypatch.setattr(webhooks, "send", fake_send)

    async def public(host):
        return None
    monkeypatch.setattr(webhooks, "assert_host_allowed", public)
    return type("Env", (), {"world": world, "sent": sent, "secrets": fake, "delivery": world_state})()


def make_channel(client, **over):
    body = {"name": "#oncall", "kind": "slack", "webhook_url": SLACK, "min_severity": "low", "enabled": True, **over}
    return client.post("/api/v1/alerts/channels", json=body, headers=ADMIN)


def fail_a_job(env, client, name="customers"):
    env.world.runs.append(Obs("j1", name, "dbt", "run", "failed", env.world.now, "run-1", None, "boom"))
    return client.post("/api/v1/alerts/rules/run", headers=ADMIN)


# ── who may do what ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_view_reads_edit_acknowledges_and_manage_changes_the_configuration(client):
    for h in (ADMIN, OPERATOR, VIEWER):
        assert client.get("/api/v1/alerts?state=open", headers=h).status_code == 200
        assert client.get("/api/v1/alerts/rules", headers=h).status_code == 200
        assert client.get("/api/v1/alerts/channels", headers=h).status_code == 200
    # holding other Workspace permissions but not workspace:alerts:view is not enough, even to look
    for path in ("/api/v1/alerts?state=open", "/api/v1/alerts/counts", "/api/v1/alerts/rules", "/api/v1/alerts/channels"):
        assert client.get(path, headers=NOBODY).status_code == 403
    # workspace:alerts:manage: channels, rules, test sends and "run now" only
    body = {"name": "x", "kind": "slack", "webhook_url": SLACK}
    assert client.post("/api/v1/alerts/channels", json=body, headers=VIEWER).status_code == 403
    assert client.post("/api/v1/alerts/channels", json=body, headers=OPERATOR).status_code == 403
    assert client.post("/api/v1/alerts/rules/run", headers=OPERATOR).status_code == 403
    assert client.post("/api/v1/alerts/channels", json=body, headers=ADMIN).status_code == 201


def test_acknowledging_needs_workspace_alerts_edit_which_an_operator_has_and_a_viewer_does_not(client, env):
    fail_a_job(env, client)
    make_channel(client)
    client.post("/api/v1/alerts/rules/run", headers=ADMIN)
    [a] = client.get("/api/v1/alerts?state=open", headers=ADMIN).json()["items"]
    assert client.post(f"/api/v1/alerts/{a['id']}/acknowledge", json={}, headers=VIEWER).status_code == 403
    assert client.post(f"/api/v1/alerts/{a['id']}/resolve", json={}, headers=NOBODY).status_code == 403
    assert client.post(f"/api/v1/alerts/{a['id']}/acknowledge", json={}, headers=OPERATOR).status_code == 200


def test_without_the_gateway_there_is_no_access(client):
    assert client.get("/api/v1/alerts", headers={}).status_code in (401, 403)


# ── channels: the webhook URL never comes back ───────────────────────────────────────────────────────────────────────────────
def test_creating_a_channel_never_returns_or_lists_the_webhook_url(client, env):
    r = make_channel(client)
    assert r.status_code == 201
    body = r.json()
    assert body["url_hint"] == "hooks.slack.com/…/WXYZ" and "webhook_url" not in body and "secret_ref" not in body
    listing = client.get("/api/v1/alerts/channels", headers=ADMIN)
    for text in (r.text, listing.text, client.get(f"/api/v1/alerts?state=open", headers=ADMIN).text):
        assert "T000/B000" not in text and SLACK not in text
    assert list(env.secrets.data.values()) == [{"url": SLACK}]


def test_a_bad_or_foreign_webhook_url_is_a_plain_400_that_names_the_field(client, env):
    for url, expect in (("http://hooks.slack.com/x", "https"), ("https://evil.example.com/x", "must be on"), ("nonsense", "https")):
        r = make_channel(client, webhook_url=url)
        assert r.status_code == 400 and r.json()["detail"]["details"]["field"] == "webhook_url" and expect in r.json()["detail"]["message"]
    assert env.secrets.data == {}


def test_a_duplicate_channel_name_is_a_409_and_an_unknown_kind_a_422(client):
    assert make_channel(client).status_code == 201
    r = make_channel(client, name="#ONCALL")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALERT_CHANNEL_NAME_TAKEN"
    assert make_channel(client, name="other", kind="pagerduty").status_code == 422


def test_editing_a_channel_without_a_url_keeps_the_stored_one(client, env):
    cid = make_channel(client).json()["id"]
    r = client.patch(f"/api/v1/alerts/channels/{cid}", json={"min_severity": "high", "enabled": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["min_severity"] == "high" and r.json()["enabled"] is False
    assert list(env.secrets.data.values()) == [{"url": SLACK}]
    r = client.patch(f"/api/v1/alerts/channels/{cid}", json={"kind": "teams", "webhook_url": TEAMS}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["kind"] == "teams" and list(env.secrets.data.values()) == [{"url": TEAMS}]
    assert client.patch(f"/api/v1/alerts/channels/{cid}", json={"kind": "slack"}, headers=ADMIN).status_code == 400      # needs the URL


def test_a_channel_scoped_to_a_job_can_be_set_and_cleared(client):
    cid = make_channel(client, scope_job_key="j1", scope_job_name="customers").json()["id"]
    got = client.get("/api/v1/alerts/channels", headers=ADMIN).json()[0]
    assert (got["scope_job_key"], got["scope_job_name"]) == ("j1", "customers")
    r = client.patch(f"/api/v1/alerts/channels/{cid}", json={"scope_job_key": ""}, headers=ADMIN).json()
    assert r["scope_job_key"] is None and r["scope_job_name"] is None


def test_sending_a_test_reports_the_result_and_the_channel_shows_it(client, env):
    cid = make_channel(client).json()["id"]
    r = client.post(f"/api/v1/alerts/channels/{cid}/test", headers=ADMIN)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["channel"]["last_ok"] is True
    assert env.sent == [("slack", SLACK, "Test alert from InAiEra Workspace")]
    env.delivery["ok"] = False
    r = client.post(f"/api/v1/alerts/channels/{cid}/test", headers=ADMIN).json()
    assert r["ok"] is False and r["detail"] == "404 no_service" and r["channel"]["last_ok"] is False
    assert client.get("/api/v1/alerts/channels", headers=ADMIN).json()[0]["last_detail"] == "404 no_service"
    assert client.post("/api/v1/alerts/channels/00000000-0000-0000-0000-000000000000/test", headers=ADMIN).status_code == 404


def test_deleting_a_channel_removes_it_and_its_secret(client, env):
    cid = make_channel(client).json()["id"]
    assert client.delete(f"/api/v1/alerts/channels/{cid}", headers=ADMIN).status_code == 204
    assert client.get("/api/v1/alerts/channels", headers=ADMIN).json() == [] and env.secrets.data == {}
    assert client.delete(f"/api/v1/alerts/channels/{cid}", headers=ADMIN).status_code == 404


# ── rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_default_rules_are_there_and_the_kinds_describe_their_numbers(client):
    rules = client.get("/api/v1/alerts/rules", headers=VIEWER).json()
    assert {r["name"] for r in rules} >= {"job-run-failed", "repeated-failures", "high-failure-rate", "quality-check-failed",
                                          "upstream-blocked", "flink-job-failed"}
    kinds = {k["kind"]: k for k in client.get("/api/v1/alerts/rules/kinds", headers=VIEWER).json()}
    assert set(kinds) == {"run_failed", "consecutive_failures", "failure_rate", "quality_failed", "upstream_blocked", "flink_job_failed"}
    assert kinds["failure_rate"]["uses_min_runs"] is True and kinds["flink_job_failed"]["uses_window"] is False


def test_rule_create_edit_delete_and_validation(client):
    body = {"name": "tight", "kind": "run_failed", "severity": "high", "threshold": 2, "window_seconds": 900, "min_runs": 1,
            "scope_job_key": "j1", "scope_job_name": "customers", "enabled": True}
    r = client.post("/api/v1/alerts/rules", json=body, headers=ADMIN)
    assert r.status_code == 201 and r.json()["scope_job_name"] == "customers"
    rid = r.json()["id"]
    assert client.post("/api/v1/alerts/rules", json=body, headers=ADMIN).status_code == 409
    r = client.patch(f"/api/v1/alerts/rules/{rid}", json={"threshold": 5, "enabled": False, "scope_job_key": ""}, headers=ADMIN).json()
    assert (r["threshold"], r["enabled"], r["scope_job_key"], r["scope_job_name"]) == (5, False, None, None)
    assert client.patch(f"/api/v1/alerts/rules/{rid}", json={"window_seconds": 1234}, headers=ADMIN).status_code == 400
    assert client.post("/api/v1/alerts/rules", json={**body, "name": "bad", "kind": "failure_rate", "threshold": 500}, headers=ADMIN).status_code in (400, 422)
    assert client.post("/api/v1/alerts/rules", json={**body, "name": "zero", "threshold": 0}, headers=ADMIN).status_code == 422
    assert client.delete(f"/api/v1/alerts/rules/{rid}", headers=ADMIN).status_code == 204
    assert client.delete(f"/api/v1/alerts/rules/{rid}", headers=ADMIN).status_code == 404


# ── alerts, end to end ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_failure_is_raised_sent_listed_acknowledged_and_resolved(client, env):
    make_channel(client)
    r = fail_a_job(env, client)
    assert r.status_code == 200 and r.json()["ran"] is True and r.json()["raised"] == 1 and r.json()["delivered"] == 1
    assert env.sent[-1][0] == "slack" and "customers" in env.sent[-1][2]
    assert client.get("/api/v1/alerts/counts", headers=VIEWER).json() == {"open": 1, "acknowledged": 0, "resolved": 0, "undelivered": 0}
    page = client.get("/api/v1/alerts?state=open", headers=VIEWER).json()
    [a] = page["items"]
    assert page["total"] == 1 and page["undelivered"] == 0
    assert (a["rule_name"], a["severity"], a["job_name"], a["job_kind"], a["state"], a["delivery"]) == \
           ("job-run-failed", "high", "customers", "dbt", "open", "sent")
    assert a["last_action"] == "sent to #oncall" and a["last_actor"] == "system"

    ack = client.post(f"/api/v1/alerts/{a['id']}/acknowledge", json={"note": "on it"}, headers=OPERATOR).json()
    assert ack["state"] == "acknowledged" and [e["action"] for e in ack["events"]] == ["raised", "sent to #oncall", "acknowledged"]
    assert ack["events"][-1]["actor"] == "trans" and ack["events"][-1]["note"] == "on it"
    assert client.get("/api/v1/alerts?state=open", headers=ADMIN).json()["items"] == []
    assert len(client.get("/api/v1/alerts?state=acknowledged", headers=ADMIN).json()["items"]) == 1

    done = client.post(f"/api/v1/alerts/{a['id']}/resolve", json={}, headers=OPERATOR).json()
    assert done["state"] == "resolved" and done["resolved_at"] is not None
    assert client.post(f"/api/v1/alerts/{a['id']}/acknowledge", json={}, headers=OPERATOR).status_code == 409
    assert client.get("/api/v1/alerts/counts", headers=ADMIN).json()["resolved"] == 1
    detail = client.get(f"/api/v1/alerts/{a['id']}", headers=VIEWER).json()
    assert detail["events"][-1]["action"] == "resolved" and detail["summary"].startswith("dbt project 'customers' failed: boom")


def test_alerts_nobody_received_are_counted_but_not_listed_until_sent(client, env):
    fail_a_job(env, client)                                                      # no channel yet
    page = client.get("/api/v1/alerts?state=open", headers=ADMIN).json()
    assert page["items"] == [] and page["undelivered"] == 1
    make_channel(client)
    assert client.post("/api/v1/alerts/rules/run", headers=ADMIN).json()["delivered"] == 1
    page = client.get("/api/v1/alerts?state=open", headers=ADMIN).json()
    assert len(page["items"]) == 1 and page["undelivered"] == 0


def test_the_list_filters_by_date_and_validates_its_arguments(client, env):
    make_channel(client)
    fail_a_job(env, client)
    assert len(client.get("/api/v1/alerts?state=open&from=2000-01-01&to=2999-01-01", headers=ADMIN).json()["items"]) == 1
    assert client.get("/api/v1/alerts?state=open&from=2999-01-01", headers=ADMIN).json()["items"] == []
    assert client.get("/api/v1/alerts?state=open&to=2000-01-01", headers=ADMIN).json()["total"] == 0
    assert client.get("/api/v1/alerts?state=bogus", headers=ADMIN).status_code == 422
    assert client.get("/api/v1/alerts?state=open&limit=0", headers=ADMIN).status_code == 422
    assert client.get("/api/v1/alerts/00000000-0000-0000-0000-000000000000", headers=ADMIN).status_code == 404


def test_breaching_lists_what_is_open_without_raising_anything(client, env):
    make_channel(client)
    fail_a_job(env, client)
    b = client.get("/api/v1/alerts/rules/breaching", headers=VIEWER).json()
    assert [(x["rule_name"], x["job_name"], x["state"]) for x in b] == [("job-run-failed", "customers", "already_open")]
