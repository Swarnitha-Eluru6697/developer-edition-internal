"""Alerts go to the channel they were routed to — and only that one.

"Send alerts only to that channel" is the whole point of the feature, so
the tests that matter are the negative ones: an alert below a channel's
severity floor, or belonging to a different agent, must NOT arrive. A
fan-out that over-delivers is not a cosmetic bug — it is the on-call
channel filling with noise until people mute it.

The other three things covered here are the ways this feature could
quietly do damage: leaking the webhook URL (a credential), letting a
plaintext or internal URL through, and losing an alert because a channel
was down.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import alert_delivery
from app.routers import alerts as alerts_router


def dest(**kw):
    base = dict(id=1, name="oncall", kind="slack", enabled=True,
                min_severity="low", agent_id=None,
                webhook_url_encrypted="enc", url_hint="hooks.slack.com/…/abc123",
                last_attempt_at=None, last_status=None, last_detail=None)
    base.update(kw)
    return SimpleNamespace(**base)


def alert(**kw):
    base = dict(id="al-1", sev="high", rule="error-rate", agent_id="agent-a",
                agent_name="Acme bot FAQ", summary="Error rate above 5%.",
                ts="2026-09-23 10:00",
                # fan_out records delivery on the alert now — the Open tab
                # filters on it.
                delivered_at=None, delivered_to=[])
    base.update(kw)
    return SimpleNamespace(**base)


# --- routing: the negative cases are the ones that matter ----------------

def test_an_alert_below_the_floor_does_not_arrive():
    assert not alert_delivery.matches(dest(min_severity="high"), alert(sev="medium"))
    assert not alert_delivery.matches(dest(min_severity="high"), alert(sev="low"))


def test_an_alert_at_or_above_the_floor_arrives():
    assert alert_delivery.matches(dest(min_severity="high"), alert(sev="high"))
    assert alert_delivery.matches(dest(min_severity="low"), alert(sev="low"))


def test_a_channel_scoped_to_one_agent_ignores_every_other_agent():
    """The literal ask: alerts for this agent, to this channel, only."""
    scoped = dest(agent_id="agent-a")
    assert alert_delivery.matches(scoped, alert(agent_id="agent-a"))
    assert not alert_delivery.matches(scoped, alert(agent_id="agent-b"))


def test_an_unscoped_channel_receives_every_agent():
    catch_all = dest(agent_id=None)
    assert alert_delivery.matches(catch_all, alert(agent_id="agent-a"))
    assert alert_delivery.matches(catch_all, alert(agent_id="agent-b"))


def test_a_disabled_channel_receives_nothing():
    """Disabling must actually stop delivery — otherwise the switch is
    decoration and someone turns it off expecting silence."""
    assert not alert_delivery.matches(dest(enabled=False), alert(sev="high"))


def test_both_filters_are_and_ed():
    """Right agent but too low, and high enough but wrong agent — each
    alone is enough to exclude."""
    d = dest(agent_id="agent-a", min_severity="high")
    assert not alert_delivery.matches(d, alert(agent_id="agent-a", sev="low"))
    assert not alert_delivery.matches(d, alert(agent_id="agent-b", sev="high"))
    assert alert_delivery.matches(d, alert(agent_id="agent-a", sev="high"))


def test_an_unknown_severity_over_notifies_rather_than_disappearing():
    """A severity name nobody anticipated should be noisy, not silent:
    an alert that matches no rule must not be the one that vanishes."""
    assert alert_delivery.matches(dest(min_severity="high"), alert(sev="catastrophic"))


def test_fan_out_delivers_only_to_matching_destinations():
    """The end-to-end shape of the routing, not just the predicate."""
    sent = []

    class FakeSession:
        def add(self, o): pass
        def commit(self): pass

    a = alert(sev="high", agent_id="agent-a")
    destinations = [
        dest(id=1, name="acme-oncall", agent_id="agent-a", min_severity="high"),
        dest(id=2, name="other-agent", agent_id="agent-b", min_severity="low"),
        dest(id=3, name="too-quiet", agent_id=None, min_severity="high"),
        dest(id=4, name="switched-off", agent_id=None, enabled=False),
    ]
    original = alert_delivery.deliver
    alert_delivery.deliver = lambda d, al, **kw: (sent.append(d.name) or
                                                  {"ok": True, "detail": "HTTP 200"})
    try:
        results = alert_delivery.fan_out(FakeSession(), a, destinations)
    finally:
        alert_delivery.deliver = original

    assert sent == ["acme-oncall", "too-quiet"], sent
    assert [r["destination"] for r in results] == ["acme-oncall", "too-quiet"]


# --- the webhook URL is a credential -------------------------------------

def test_the_api_never_returns_the_webhook_url():
    """Anyone holding an incoming-webhook URL can post into that
    channel, so it is write-only. A UI that could read it back would put
    it in a browser's network tab on every page load."""
    d = dest(webhook_url_encrypted="gAAAAAB-secret-ciphertext")
    body = alerts_router._public(d)
    assert "webhook_url" not in body
    assert "gAAAAAB-secret-ciphertext" not in repr(body)
    # …but the UI still needs to know one is set.
    assert body["configured"] is True


def test_the_hint_identifies_the_channel_without_being_usable():
    hint = alerts_router._hint("https://hooks.slack.com/services/T000/B111/abcdefXYZ123")
    assert "hooks.slack.com" in hint
    assert "B111" not in hint
    assert "abcdefXYZ123" not in hint


def test_a_plaintext_webhook_url_is_rejected():
    """Posting a channel credential over http would expose it to
    anything on the path."""
    with pytest.raises(Exception) as e:
        alerts_router._validate(alerts_router.DestinationIn(
            name="x", webhook_url="http://hooks.slack.com/services/a/b/c"))
    assert "https" in str(e.value)


@pytest.mark.parametrize("field,value", [("kind", "email"), ("min_severity", "urgent")])
def test_unrecognised_kind_or_severity_is_rejected(field, value):
    """An unknown kind would silently get the Slack payload shape; an
    unknown floor would compare against nothing."""
    kw = {"name": "x", "webhook_url": "https://hooks.slack.com/services/a/b/c", field: value}
    with pytest.raises(Exception):
        alerts_router._validate(alerts_router.DestinationIn(**kw))


# --- delivery must never destroy the alert -------------------------------

def test_delivery_failure_is_reported_not_raised(monkeypatch):
    """An undeliverable alert is still a real alert."""
    monkeypatch.setattr(alert_delivery, "decrypt_secret", lambda _: "https://example.com/hook")
    def boom(*a, **kw):
        raise ConnectionError("channel unreachable")
    monkeypatch.setattr(alert_delivery, "safe_http_request", boom)
    out = alert_delivery.deliver(dest(), alert())
    assert out["ok"] is False
    assert "channel unreachable" in out["detail"]


def test_an_ssrf_block_is_reported_distinctly(monkeypatch):
    """The platform refusing and the channel being down need different
    fixes, so they must not read the same in the UI."""
    monkeypatch.setattr(alert_delivery, "decrypt_secret", lambda _: "https://10.0.0.1/hook")
    def blocked(*a, **kw):
        raise alert_delivery.SSRFBlocked("private address")
    monkeypatch.setattr(alert_delivery, "safe_http_request", blocked)
    out = alert_delivery.deliver(dest(), alert())
    assert out["ok"] is False
    assert "egress policy" in out["detail"]


def test_a_destination_with_no_url_fails_without_calling_out(monkeypatch):
    monkeypatch.setattr(alert_delivery, "decrypt_secret", lambda _: None)
    def must_not_run(*a, **kw):
        raise AssertionError("should not have attempted a request")
    monkeypatch.setattr(alert_delivery, "safe_http_request", must_not_run)
    assert alert_delivery.deliver(dest(), alert())["ok"] is False


def test_one_failing_channel_does_not_stop_the_others():
    calls = []

    class FakeSession:
        def add(self, o): pass
        def commit(self): pass

    original = alert_delivery.deliver
    def flaky(d, al, **kw):
        calls.append(d.name)
        if d.name == "broken":
            return {"ok": False, "detail": "HTTP 404: no_team"}
        return {"ok": True, "detail": "HTTP 200"}
    alert_delivery.deliver = flaky
    try:
        results = alert_delivery.fan_out(
            FakeSession(), alert(), [dest(id=1, name="broken"), dest(id=2, name="working")])
    finally:
        alert_delivery.deliver = original

    assert calls == ["broken", "working"]
    assert [r["ok"] for r in results] == [False, True]


def test_the_outcome_is_recorded_on_the_destination():
    """A channel that has been silently failing for a week is worth
    being able to see."""
    class FakeSession:
        def add(self, o): pass
        def commit(self): pass

    d = dest(name="broken")
    original = alert_delivery.deliver
    alert_delivery.deliver = lambda *a, **kw: {"ok": False, "detail": "HTTP 404: no_team"}
    try:
        alert_delivery.fan_out(FakeSession(), alert(), [d])
    finally:
        alert_delivery.deliver = original

    assert d.last_status == "failed"
    assert "no_team" in d.last_detail
    assert d.last_attempt_at is not None


# --- payload shape per provider ------------------------------------------

def test_a_workflows_url_gets_an_adaptive_card_not_a_messagecard():
    """Microsoft retired Office 365 connectors. A Power Automate
    Workflows endpoint answers 202 Accepted to a MessageCard it will
    then silently fail to render — the POST looks successful and nothing
    appears in the channel. Observed live against a real
    powerplatform.com URL."""
    body = alert_delivery.payload_for(
        "teams", alert(),
        "https://default123.21.environment.api.powerplatform.com/powerautomate/x/y")
    assert body["type"] == "message"
    card = body["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"
    assert "@type" not in body


def test_a_classic_connector_url_still_gets_a_messagecard():
    """The old connectors are not all gone, and they reject an Adaptive
    Card wrapper."""
    body = alert_delivery.payload_for(
        "teams", alert(), "https://acme.webhook.office.com/webhookb2/aaa/IncomingWebhook/b/c")
    assert body["@type"] == "MessageCard"


@pytest.mark.parametrize("url,workflows", [
    ("https://x.environment.api.powerplatform.com/a", True),
    ("https://prod-12.westus.logic.azure.com/workflows/a", True),
    ("https://acme.webhook.office.com/webhookb2/a", False),
    ("https://outlook.office.com/webhook/a", False),
])
def test_the_two_teams_endpoint_styles_are_told_apart(url, workflows):
    assert alert_delivery._is_workflows_url(url) is workflows


def test_a_202_from_workflows_says_queued_rather_than_delivered(monkeypatch):
    """202 means the flow was queued, not that a message appeared.
    Reporting it as a plain success is how a silently-failing channel
    goes unnoticed for a week."""
    url = "https://x.environment.api.powerplatform.com/a"
    monkeypatch.setattr(alert_delivery, "decrypt_secret", lambda _: url)
    monkeypatch.setattr(alert_delivery, "safe_http_request",
                        lambda *a, **kw: {"status_code": 202, "text": ""})
    out = alert_delivery.deliver(dest(kind="teams"), alert())
    assert out["ok"] is True
    assert "queued" in out["detail"]


def test_slack_and_teams_get_different_payloads():
    """Posting a Slack block payload to a Teams connector is a 400, and
    vice versa — the kind field has to actually change the body."""
    a = alert()
    slack = alert_delivery.payload_for("slack", a)
    teams = alert_delivery.payload_for("teams", a, "https://acme.webhook.office.com/webhookb2/a")
    assert "blocks" in slack and "@type" not in slack
    assert teams["@type"] == "MessageCard" and "blocks" not in teams


def test_the_payload_carries_what_a_reader_needs():
    """A notification someone reads on a phone has to say which agent
    and how bad, without opening the platform."""
    a = alert(sev="high", agent_name="Acme bot FAQ", summary="Error rate above 5%.")
    slack = alert_delivery.payload_for("slack", a)
    assert "Acme bot FAQ" in slack["text"]
    assert "HIGH" in slack["text"]
    assert "Error rate above 5%." in slack["text"]
    teams = alert_delivery.payload_for("teams", a, "https://acme.webhook.office.com/webhookb2/a")
    assert teams["sections"][0]["text"] == "Error rate above 5%."
    assert teams["themeColor"] == "D93025"  # high reads red, not the default blue


# --- acting on an alert from the chat card --------------------------------
#
# A link in a channel is visible to everyone there and gets copied,
# quoted and forwarded, so it has to be unforgeable on its own.

def test_a_token_authorises_exactly_one_alert_and_one_action():
    from app import alert_actions
    tok = alert_actions.sign("al-123", "resolve")
    assert alert_actions.verify("al-123", "resolve", tok)
    # Edited into a different alert, or a different action, it must fail.
    assert not alert_actions.verify("al-124", "resolve", tok)
    assert not alert_actions.verify("al-123", "acknowledge", tok)


def test_an_absent_or_junk_token_is_refused():
    from app import alert_actions
    assert not alert_actions.verify("al-123", "resolve", "")
    assert not alert_actions.verify("al-123", "resolve", "deadbeef")
    assert not alert_actions.verify("al-123", "resolve", None)


def test_the_id_and_action_cannot_be_slid_past_each_other():
    """Without a separator, ("al-1", "23resolve") and ("al-123",
    "resolve") would hash to the same token."""
    from app import alert_actions
    assert alert_actions.sign("al-1", "resolve") != alert_actions.sign("al-123", "resolve")


def test_an_unknown_action_cannot_be_signed_at_all():
    """Signing an arbitrary action string would let a valid signature be
    reused for something added later."""
    from app import alert_actions
    with pytest.raises(ValueError):
        alert_actions.sign("al-123", "delete")
    assert not alert_actions.verify("al-123", "delete", "anything")


def test_both_cards_carry_an_acknowledge_and_a_resolve_button():
    """Reading an alert in a channel and then having to go find it in
    the platform is how alerts stay unacknowledged."""
    a = alert()
    slack = alert_delivery.payload_for("slack", a)
    labels = [e["text"]["text"] for b in slack["blocks"]
              if b["type"] == "actions" for e in b["elements"]]
    assert labels == ["Acknowledge", "Resolve"]
    assert all(e.get("url") for b in slack["blocks"]
               if b["type"] == "actions" for e in b["elements"])

    card = alert_delivery.payload_for(
        "teams", a, "https://x.environment.api.powerplatform.com/a")
    actions = card["attachments"][0]["content"]["actions"]
    assert [x["title"] for x in actions] == ["Acknowledge", "Resolve"]
    assert all(x["type"] == "Action.OpenUrl" and x["url"] for x in actions)

    classic = alert_delivery.payload_for(
        "teams", a, "https://acme.webhook.office.com/webhookb2/a")
    assert [x["name"] for x in classic["potentialAction"]] == ["Acknowledge", "Resolve"]
