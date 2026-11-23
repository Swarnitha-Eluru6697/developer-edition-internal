"""Signed links for the Acknowledge / Resolve buttons: what a token can do, and that it cannot be forged, altered or used late."""
from __future__ import annotations

import base64
import json

import pytest

from app.core.config import get_settings
from app.services.alerts import links

ALERT = "5c881da9-c64c-403a-9964-f4c7abc753f3"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "alerts_action_secret", "s3cret-" * 6)
    monkeypatch.setattr(s, "alerts_public_url", "https://ws.example.com/")
    monkeypatch.setattr(s, "alerts_action_link_ttl_seconds", 3600)


def test_a_token_carries_exactly_alert_action_channel_and_expiry():
    c = links.read_token(links.make_token(ALERT, "resolve", "teams", "teams-workspace-alerts", now=1_000_000), now=1_000_000)
    assert (c.alert_id, c.action, c.channel_kind, c.channel_name, c.expires_at) == (ALERT, "resolve", "teams", "teams-workspace-alerts", 1_003_600)


def test_the_url_is_the_public_url_plus_the_actions_route_without_a_double_slash():
    url = links.action_url(ALERT, "acknowledge", "slack", "#oncall")
    assert url.startswith("https://ws.example.com/api/v1/alerts/actions/") and "//api" not in url
    assert links.read_token(url.rsplit("/", 1)[1]).action == "acknowledge"


def test_a_token_is_not_valid_after_its_expiry_and_valid_just_before():
    t = links.make_token(ALERT, "acknowledge", "teams", "c", now=1_000_000)
    assert links.read_token(t, now=1_003_600).action == "acknowledge"
    with pytest.raises(links.LinkExpired):
        links.read_token(t, now=1_003_601)


def test_an_altered_token_is_refused():
    t = links.make_token(ALERT, "acknowledge", "teams", "c")
    payload, sig = t.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["x"] = "resolve"                                                      # acknowledge -> resolve
    forged = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=").decode() + "." + sig
    with pytest.raises(links.LinkInvalid):
        links.read_token(forged)
    body2 = {**json.loads(base64.urlsafe_b64decode(payload + "==")), "a": "00000000-0000-0000-0000-000000000000"}   # another alert
    other = base64.urlsafe_b64encode(json.dumps(body2, separators=(",", ":")).encode()).rstrip(b"=").decode() + "." + sig
    with pytest.raises(links.LinkInvalid):
        links.read_token(other)


@pytest.mark.parametrize("junk", ["", ".", "abc", "abc.def", "....", "a" * 500, "e30.e30"])
def test_junk_is_refused_without_an_error(junk):
    with pytest.raises(links.LinkInvalid):
        links.read_token(junk)


def test_a_token_signed_with_another_secret_is_refused(monkeypatch):
    t = links.make_token(ALERT, "resolve", "teams", "c")
    monkeypatch.setattr(get_settings(), "alerts_action_secret", "a-different-secret-entirely-xxxxxxxxxxxx")
    with pytest.raises(links.LinkInvalid):
        links.read_token(t)


def test_without_a_secret_nothing_is_accepted_and_links_are_off(monkeypatch):
    t = links.make_token(ALERT, "resolve", "teams", "c")
    monkeypatch.setattr(get_settings(), "alerts_action_secret", None)
    assert links.enabled() is False
    with pytest.raises(links.LinkInvalid):
        links.read_token(t)


def test_links_need_both_the_public_url_and_the_secret(monkeypatch):
    assert links.enabled() is True
    monkeypatch.setattr(get_settings(), "alerts_public_url", "  ")
    assert links.enabled() is False


def test_only_the_two_actions_exist():
    with pytest.raises(ValueError):
        links.make_token(ALERT, "delete", "teams", "c")


def _signed(body: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=").decode()
    return f"{payload}.{links._sign(payload)}"


def test_a_validly_signed_token_for_an_unknown_action_is_refused():
    with pytest.raises(links.LinkInvalid):
        links.read_token(_signed({"a": ALERT, "x": "delete", "k": "teams", "n": "c", "e": 9_999_999_999}))


def test_a_token_forged_with_an_empty_secret_is_refused_when_no_secret_is_set(monkeypatch):
    monkeypatch.setattr(get_settings(), "alerts_action_secret", None)
    forged = _signed({"a": ALERT, "x": "resolve", "k": "teams", "n": "c", "e": 9_999_999_999})      # signed with "": anyone can compute it
    with pytest.raises(links.LinkInvalid):
        links.read_token(forged)
    monkeypatch.setattr(get_settings(), "alerts_action_secret", "")
    with pytest.raises(links.LinkInvalid):
        links.read_token(forged)
