"""Slack / Teams webhooks: which URLs are accepted, what is posted, and that the URL never leaks."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core.config import get_settings
from app.core.observability import redact_sensitive_fields
from app.services.alerts import webhooks

SLACK = "https://hooks.slack.com/services/T000/B000/XXXXsecretXXXX"
TEAMS_CLASSIC = "https://contoso.webhook.office.com/webhookb2/aaaa/IncomingWebhook/bbbb/cccc"
TEAMS_FLOW = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"
MESSAGE = webhooks.Message("Title", "high", "It broke.", (("Job", "customers"), ("Rule", "job-run-failed")))


@pytest.fixture(autouse=True)
def _public_egress(monkeypatch):
    async def ok(host):                    # DNS is not asked in unit tests: every public host passes the egress policy
        return None
    monkeypatch.setattr(webhooks, "assert_host_allowed", ok)


def check(kind, url):
    asyncio.run(webhooks.check_webhook_url(kind, url))


@pytest.mark.parametrize("kind,url", [("slack", SLACK), ("teams", TEAMS_CLASSIC), ("teams", TEAMS_FLOW),
                                      ("teams", "https://outlook.office.com/webhook/aaa@bbb/IncomingWebhook/ccc/ddd")])
def test_provider_urls_are_accepted(kind, url):
    check(kind, url)


@pytest.mark.parametrize("kind,url,why", [
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
    s = get_settings()
    monkeypatch.setattr(s, "alerts_webhook_extra_hosts", ["relay.corp.example", ".hooks.corp.example"])
    check("slack", "https://relay.corp.example/slack/abc")
    check("teams", "https://a.hooks.corp.example/x")
    monkeypatch.setattr(s, "environment", "dev")
    check("slack", "http://relay.corp.example/slack/abc")                                    # dev + listed host: http is fine
    with pytest.raises(webhooks.WebhookRejected, match="https"):
        check("slack", "http://hooks.slack.com/services/T/B/X")                             # dev, but not a listed host
    monkeypatch.setattr(s, "environment", "prod")
    with pytest.raises(webhooks.WebhookRejected, match="https"):
        check("slack", "http://relay.corp.example/slack/abc")                               # not dev: https only


def test_a_provider_host_that_resolves_privately_is_refused_by_the_egress_policy(monkeypatch):
    from app.core.egress_policy import EgressPolicyError

    async def deny(host):
        raise EgressPolicyError(f"{host} resolves to a private address")
    monkeypatch.setattr(webhooks, "assert_host_allowed", deny)
    with pytest.raises(webhooks.WebhookRejected, match="private"):
        check("slack", SLACK)


def test_the_hint_shows_the_host_and_four_characters_not_the_secret():
    assert webhooks.hint_of(SLACK) == "hooks.slack.com/…/ XXXX".replace(" ", "")
    assert "secret" not in webhooks.hint_of(SLACK)
    assert webhooks.hint_of("https://hooks.slack.com") == "hooks.slack.com/…"
    assert "SECRET" not in webhooks.hint_of(TEAMS_FLOW)


# ── the messages ───────────────────────────────────────────────────────────────────────────────────────────────────────────
BUTTONS = (webhooks.Button("Acknowledge", "https://ws.example/api/v1/alerts/actions/AAA"),
           webhooks.Button("Resolve", "https://ws.example/api/v1/alerts/actions/BBB", "positive"))


def test_slack_message_has_the_heading_facts_summary_and_severity_colour():
    body = webhooks.body_for("slack", SLACK, MESSAGE)
    assert body["text"] == "[HIGH] Title"
    att = body["attachments"][0]
    assert att["color"] == "#DC2626"
    blocks = att["blocks"]
    assert blocks[0]["text"]["text"] == "*🚨 Title*"
    assert {"type": "mrkdwn", "text": "*Job*\ncustomers"} in blocks[1]["fields"]
    assert blocks[2]["text"]["text"] == "It broke."
    assert all(b["type"] != "actions" for b in blocks)                          # no buttons unless links are set up


def test_a_heading_replaces_the_title_on_the_card():
    m = webhooks.Message("one line", "medium", "s", (), heading="job-run-failed")
    assert m.head == "⚠️ job-run-failed"
    assert webhooks.body_for("slack", SLACK, m)["attachments"][0]["blocks"][0]["text"]["text"] == "*⚠️ job-run-failed*"


def test_classic_teams_connector_gets_a_message_card_and_workflows_an_adaptive_card():
    classic = webhooks.body_for("teams", TEAMS_CLASSIC, MESSAGE)
    assert classic["@type"] == "MessageCard" and classic["themeColor"] == "DC2626" and classic["title"] == "🚨 Title"
    assert classic["sections"][0]["facts"] == [{"name": "Job", "value": "customers"}, {"name": "Rule", "value": "job-run-failed"}]
    flow = webhooks.body_for("teams", TEAMS_FLOW, MESSAGE)
    assert flow["type"] == "message" and flow["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = flow["attachments"][0]["content"]
    assert card["body"][0] == {"type": "TextBlock", "weight": "Bolder", "size": "ExtraLarge", "wrap": True, "color": "Attention", "text": "🚨 Title"}
    assert card["body"][1]["facts"][0] == {"title": "Job", "value": "customers"}
    assert card["body"][2]["text"] == "It broke."
    assert "actions" not in card


def test_buttons_become_link_buttons_on_every_kind_of_message():
    slack = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["attachments"][0]["blocks"][-1]
    assert slack["type"] == "actions"
    assert [(b["text"]["text"], b["url"]) for b in slack["elements"]] == [("Acknowledge", BUTTONS[0].url), ("Resolve", BUTTONS[1].url)]
    assert "style" not in slack["elements"][0] and slack["elements"][1]["style"] == "primary"
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
    assert r.ok and r.detail == "delivered" and seen["url"] == SLACK and seen["body"]["text"] == "[HIGH] Title"


@pytest.mark.parametrize("status,text,expect", [(404, "no_service", "404 no_service"), (403, "invalid_token", "403 invalid_token"),
                                                (500, "boom", "500 boom"), (429, "rate_limited", "429 rate_limited")])
def test_a_refusal_is_not_delivered_and_says_why(status, text, expect):
    r = send_via(lambda req: httpx.Response(status, text=text))
    assert not r.ok and r.detail == expect


def test_a_redirect_is_not_followed_and_is_a_failure():
    r = send_via(lambda req: httpx.Response(302, headers={"location": "https://elsewhere.example/"}))
    assert not r.ok and r.detail.startswith("302")


def test_a_timeout_or_a_connection_error_is_a_failure_that_never_leaks_the_url():
    def slow(req):
        raise httpx.ReadTimeout("took too long", request=req)

    def down(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}", request=req)
    assert send_via(slow).detail.startswith("timed out")
    r = send_via(down)
    assert not r.ok and "XXXXsecretXXXX" not in r.detail and r.detail == "could not reach the webhook (ConnectError)"


def test_a_url_that_stopped_being_acceptable_is_not_posted_to():
    called = []
    r = send_via(lambda req: called.append(req) or httpx.Response(200), url="https://evil.example.com/x")
    assert not r.ok and called == []


# ── logs ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [SLACK, TEAMS_CLASSIC, TEAMS_FLOW])
def test_a_webhook_url_in_a_log_line_is_redacted(url):
    line = redact_sensitive_fields(None, "info", {"event": f'HTTP Request: POST {url} "HTTP/1.1 200 OK"'})["event"]
    assert url not in line and "[REDACTED]" in line and "secret" not in line.lower().replace("redacted", "")


# ── Slack: text from a job must not be able to ping or link ────────────────────────────────────────────────────────────────
HOSTILE = "<!channel> <!here> <@U123ABC> <https://evil.example|click me> & more"


def test_slack_text_is_escaped_so_a_job_error_cannot_ping_the_channel_or_fake_a_link():
    m = webhooks.Message(f"job {HOSTILE}", "high", HOSTILE, (("Job", HOSTILE),), heading=HOSTILE)
    body = webhooks.body_for("slack", SLACK, m)
    blocks = body["attachments"][0]["blocks"]
    safe = "&lt;!channel&gt; &lt;!here&gt; &lt;@U123ABC&gt; &lt;https://evil.example|click me&gt; &amp; more"
    assert blocks[2]["text"]["text"] == safe
    assert blocks[1]["fields"][0]["text"] == f"*Job*\n{safe}"
    assert blocks[0]["text"]["text"] == f"*🚨 {safe}*"
    assert "<!channel>" not in body["text"] and "<@" not in body["text"] and "<https" not in body["text"]
    assert "<!" not in str(blocks) and "<@" not in str(blocks)


def test_slack_escaping_leaves_ordinary_text_and_buttons_alone():
    assert webhooks.slack_escape("dbt project 'customers' failed: Compilation Error") == "dbt project 'customers' failed: Compilation Error"
    assert webhooks.slack_escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    b = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", "s", (), buttons=BUTTONS))["attachments"][0]["blocks"][-1]
    assert b["elements"][0]["url"] == BUTTONS[0].url and b["elements"][0]["text"]["text"] == "Acknowledge"


def test_teams_text_is_not_slack_escaped():
    flow = webhooks.body_for("teams", TEAMS_FLOW, webhooks.Message("t", "low", "a & b", ()))["attachments"][0]["content"]
    assert flow["body"][2]["text"] == "a & b"


def test_a_very_long_sentence_is_cut_for_slack_so_the_message_is_not_refused_but_not_for_teams():
    long = "x" * 5000
    section = webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", long, ()))["attachments"][0]["blocks"][-1]["text"]["text"]
    assert len(section) <= 3000 and section.endswith("…")
    short = "dbt project 'customers' failed: " + "reason " * 100
    assert webhooks.body_for("slack", SLACK, webhooks.Message("t", "low", short, ()))["attachments"][0]["blocks"][-1]["text"]["text"] == short
    assert webhooks.body_for("teams", TEAMS_FLOW, webhooks.Message("t", "low", long, ()))["attachments"][0]["content"]["body"][2]["text"] == long
