"""Slack / Teams webhooks: which URLs are accepted, what is posted, and that the URL never leaks.
Ported from the Workspace's tests/test_alerts_webhooks.py."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core.config import settings
from app.core.network_policy import DestinationBlockedError
from app.core.redaction import redact_secrets
from app.services.alerts import webhooks

SLACK = "https://hooks.slack.com/services/T000/B000/XXXXsecretXXXX"
TEAMS_CLASSIC = "https://contoso.webhook.office.com/webhookb2/aaaa/IncomingWebhook/bbbb/cccc"
TEAMS_FLOW = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"
MESSAGE = webhooks.Message("Title", "high", "It broke.", (("Pipeline", "customers"), ("Rule", "pipeline-failed")))


@pytest.fixture(autouse=True)
def _public_egress(monkeypatch):
    async def ok(host, *, org_id=None, purpose=""):    # DNS is not asked in unit tests: every public host passes the SSRF guard
        return None
    monkeypatch.setattr(webhooks, "validate_destination", ok)


def check(kind, url):
    asyncio.run(webhooks.check_webhook_url(kind, url))


@pytest.mark.parametrize(("kind", "url"), [("slack", SLACK), ("teams", TEAMS_CLASSIC), ("teams", TEAMS_FLOW),
                                      ("teams", "https://outlook.office.com/webhook/aaa@bbb/IncomingWebhook/ccc/ddd")])
def test_provider_urls_are_accepted(kind, url):
    check(kind, url)


@pytest.mark.parametrize(("kind", "url", "why"), [
    ("slack", "http://hooks.slack.com/services/T/B/X", "https"),
    ("slack", "https://evil.example.com/services/T/B/X", "must be on hooks.slack.com"),
    ("teams", SLACK, "must be on"),                                        # a Slack URL is not a Teams channel
    ("slack", TEAMS_CLASSIC, "must be on"),
    ("slack", "https://hooks.slack.com.evil.com/services/T/B/X", "must be on"),          # suffix tricks
    ("teams", "https://evilwebhook.office.com.attacker.io/x", "must be on"),
    ("slack", "https://user:pw@hooks.slack.com/services/T/B/X", "not a valid"),
    ("slack", "not a url", "https"),
    ("slack", "", "https"),
    ("slack", "ftp://hooks.slack.com/x", "https"),
])
def test_other_urls_are_refused_with_a_plain_reason(kind, url, why):
    with pytest.raises(webhooks.WebhookRejected, match=why):
        check(kind, url)


def test_an_extra_host_is_accepted_and_http_only_in_dev(monkeypatch):
    s = settings
    monkeypatch.setattr(s, "alerts_webhook_extra_hosts", ["relay.corp.example", ".hooks.corp.example"])
    check("slack", "https://relay.corp.example/slack/abc")
    check("teams", "https://a.hooks.corp.example/x")
    monkeypatch.setattr(s, "env", "development")
    check("slack", "http://relay.corp.example/slack/abc")                                    # dev + listed host: http is fine
    with pytest.raises(webhooks.WebhookRejected, match="https"):
        check("slack", "http://hooks.slack.com/services/T/B/X")                             # dev, but not a listed host
    monkeypatch.setattr(s, "env", "production")
    with pytest.raises(webhooks.WebhookRejected, match="https"):
        check("slack", "http://relay.corp.example/slack/abc")                               # not dev: https only


def test_a_provider_host_that_resolves_privately_is_refused_by_the_ssrf_guard(monkeypatch):
    async def deny(host, *, org_id=None, purpose=""):
        raise DestinationBlockedError(f"{host} resolves to a private address")
    monkeypatch.setattr(webhooks, "validate_destination", deny)
    with pytest.raises(webhooks.WebhookRejected, match="public address"):
        check("slack", SLACK)


def test_the_hint_shows_the_host_and_four_characters_not_the_secret():
    assert webhooks.hint_of(SLACK) == "hooks.slack.com/…/ XXXX".replace(" ", "")
    assert "secret" not in webhooks.hint_of(SLACK)
    assert webhooks.hint_of("https://hooks.slack.com") == "hooks.slack.com/…"
    assert "SECRET" not in webhooks.hint_of(TEAMS_FLOW)


# ── the messages ───────────────────────────────────────────────────────────────────────────────────────────────────────────
BUTTONS = (webhooks.Button("Acknowledge", "https://dc.example/api/v1/dataconnect/alerts/actions/AAA"),
           webhooks.Button("Resolve", "https://dc.example/api/v1/dataconnect/alerts/actions/BBB", "positive"))


def test_slack_message_has_the_heading_facts_summary_and_severity_colour():
    body = webhooks.body_for("slack", SLACK, MESSAGE)
    assert body["text"] == "[HIGH] Title"
    att = body["attachments"][0]
    assert att["color"] == "#DC2626"
    blocks = att["blocks"]
    assert blocks[0]["text"]["text"] == "*🚨 Title*"
    assert {"type": "mrkdwn", "text": "*Pipeline*\ncustomers"} in blocks[1]["fields"]
    assert blocks[2]["text"]["text"] == "It broke."
    assert all(b["type"] != "actions" for b in blocks)                          # no buttons unless links are set up


def test_a_heading_replaces_the_title_on_the_card():
    m = webhooks.Message("one line", "medium", "s", (), heading="pipeline-degraded")
    assert m.head == "⚠️ pipeline-degraded"
    assert webhooks.body_for("slack", SLACK, m)["attachments"][0]["blocks"][0]["text"]["text"] == "*⚠️ pipeline-degraded*"


def test_classic_teams_connector_gets_a_message_card_and_workflows_an_adaptive_card():
    classic = webhooks.body_for("teams", TEAMS_CLASSIC, MESSAGE)
    assert classic["@type"] == "MessageCard"
    assert classic["themeColor"] == "DC2626"
    assert classic["title"] == "🚨 Title"
    assert classic["sections"][0]["facts"] == [{"name": "Pipeline", "value": "customers"}, {"name": "Rule", "value": "pipeline-failed"}]
    flow = webhooks.body_for("teams", TEAMS_FLOW, MESSAGE)
    assert flow["type"] == "message"
    assert flow["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = flow["attachments"][0]["content"]
    assert card["body"][0] == {"type": "TextBlock", "weight": "Bolder", "size": "ExtraLarge", "wrap": True, "color": "Attention", "text": "🚨 Title"}
    assert card["body"][1]["facts"][0] == {"title": "Pipeline", "value": "customers"}
    assert card["body"][2]["text"] == "It broke."
    assert "actions" not in card


def test_buttons_become_link_buttons_on_every_kind_of_message():
    slack = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["attachments"][0]["blocks"][-1]
    assert slack["type"] == "actions"
    assert [(b["text"]["text"], b["url"]) for b in slack["elements"]] == [("Acknowledge", BUTTONS[0].url), ("Resolve", BUTTONS[1].url)]
    assert "style" not in slack["elements"][0]
    assert slack["elements"][1]["style"] == "primary"
    classic = webhooks.body_for("teams", TEAMS_CLASSIC, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["potentialAction"]
    assert [(a["name"], a["targets"][0]["uri"]) for a in classic] == [("Acknowledge", BUTTONS[0].url), ("Resolve", BUTTONS[1].url)]
    flow = webhooks.body_for("teams", TEAMS_FLOW, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["attachments"][0]["content"]["actions"]
    assert flow == [{"type": "Action.OpenUrl", "title": "Acknowledge", "url": BUTTONS[0].url},
                    {"type": "Action.OpenUrl", "title": "Resolve", "url": BUTTONS[1].url, "style": "positive"}]


# ── sending ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def send_via(handler, kind="slack", url=SLACK):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await webhooks.send(kind, url, MESSAGE, client=c)
    return asyncio.run(go())


def test_a_2xx_answer_is_delivered_and_the_body_is_the_message():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, text="ok")
    r = send_via(handler)
    assert r.ok
    assert r.detail == "delivered"
    assert seen["url"] == SLACK
    assert seen["body"]["text"] == "[HIGH] Title"


@pytest.mark.parametrize(("status", "text", "expect"), [(404, "no_service", "404 no_service"), (403, "invalid_token", "403 invalid_token"),
                                                (500, "boom", "500 boom"), (429, "rate_limited", "429 rate_limited")])
def test_a_refusal_is_not_delivered_and_says_why(status, text, expect):
    r = send_via(lambda req: httpx.Response(status, text=text))
    assert not r.ok
    assert r.detail == expect


def test_a_redirect_is_not_followed_and_is_a_failure():
    r = send_via(lambda req: httpx.Response(302, headers={"location": "https://elsewhere.example/"}))
    assert not r.ok
    assert r.detail.startswith("302")


def test_a_timeout_or_a_connection_error_is_a_failure_that_never_leaks_the_url():
    def slow(req):
        raise httpx.ReadTimeout("took too long", request=req)

    def down(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}", request=req)
    assert send_via(slow).detail.startswith("timed out")
    r = send_via(down)
    assert not r.ok
    assert "XXXXsecretXXXX" not in r.detail
    assert r.detail == "could not reach the webhook (ConnectError)"


def test_a_url_that_stopped_being_acceptable_is_not_posted_to():
    called = []
    r = send_via(lambda req: called.append(req) or httpx.Response(200), url="https://evil.example.com/x")
    assert not r.ok
    assert called == []


# ── logs ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
TEAMS_POWER = "https://default1234.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/abc/triggers/manual?sig=SECRET"


@pytest.mark.parametrize("url", [SLACK, TEAMS_CLASSIC, TEAMS_FLOW, TEAMS_POWER, "https://outlook.office365.com/webhook/aaa@bbb/IncomingWebhook/SECRET"])
def test_a_webhook_url_in_a_log_line_is_redacted(url):
    line = redact_secrets(f'HTTP Request: POST {url} "HTTP/1.1 200 OK"')
    assert url not in line
    assert "********" in line
    assert "secret" not in line.lower()
    assert line.startswith("HTTP Request: POST https://")
    assert line.endswith('"HTTP/1.1 200 OK"')


def test_httpx_request_log_lines_go_through_the_redaction(caplog, monkeypatch):
    import logging

    import structlog

    from app.core.logging import configure_logging

    monkeypatch.setattr(structlog, "configure", lambda **kw: None)     # only the stdlib part: other tests rely on structlog's defaults
    configure_logging()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "POST", SLACK, "HTTP/1.1 200 OK")
    assert caplog.records
    assert all("XXXXsecretXXXX" not in r.getMessage() for r in caplog.records)


def test_other_urls_in_a_log_line_are_left_alone():
    assert redact_secrets("GET http://kafka-connect:8083/connectors/x/status") == "GET http://kafka-connect:8083/connectors/x/status"


# ── Slack: text from a pipeline error must not be able to ping or link ────────────────────────────────────────────────────────────────
HOSTILE = "<!channel> <!here> <@U123ABC> <https://evil.example|click me> & more"


def test_slack_text_is_escaped_so_an_error_cannot_ping_the_channel_or_fake_a_link():
    m = webhooks.Message(f"pipeline {HOSTILE}", "high", HOSTILE, (("Pipeline", HOSTILE),), heading=HOSTILE)
    body = webhooks.body_for("slack", SLACK, m)
    blocks = body["attachments"][0]["blocks"]
    safe = "&lt;!channel&gt; &lt;!here&gt; &lt;@U123ABC&gt; &lt;https://evil.example|click me&gt; &amp; more"
    assert blocks[2]["text"]["text"] == safe
    assert blocks[1]["fields"][0]["text"] == f"*Pipeline*\n{safe}"
    assert blocks[0]["text"]["text"] == f"*🚨 {safe}*"
    assert "<!channel>" not in body["text"]
    assert "<@" not in body["text"]
    assert "<https" not in body["text"]
    assert "<!" not in str(blocks)
    assert "<@" not in str(blocks)


def test_slack_escaping_leaves_ordinary_text_and_buttons_alone():
    assert webhooks.slack_escape("Pipeline 'customers' failed: ConfigException") == "Pipeline 'customers' failed: ConfigException"
    assert webhooks.slack_escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    b = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["attachments"][0]["blocks"][-1]
    assert b["elements"][0]["url"] == BUTTONS[0].url
    assert b["elements"][0]["text"]["text"] == "Acknowledge"


def test_teams_text_is_not_slack_escaped():
    flow = webhooks.body_for("teams", TEAMS_FLOW, webhooks.Message("t", "low", "a & b", ()))["attachments"][0]["content"]
    assert flow["body"][2]["text"] == "a & b"


def test_a_very_long_sentence_is_cut_for_slack_so_the_message_is_not_refused_but_not_for_teams():
    long = "x" * 5000
    section = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", long, ()))["attachments"][0]["blocks"][-1]["text"]["text"]
    assert len(section) <= 3000
    assert section.endswith("…")
    short = "Pipeline 'customers' failed: " + "reason " * 100
    assert webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", short, ()))["attachments"][0]["blocks"][-1]["text"]["text"] == short
    assert webhooks.body_for("teams", TEAMS_FLOW, webhooks.Message("t", "low", long, ()))["attachments"][0]["content"]["body"][2]["text"] == long
