"""Moderation shown as "on" must actually be able to block something.

`enabled` alone does not mean protected. With no provider the check
falls through to keywords, and with no keyword list it never matches —
so a config of {"enabled": true} refuses nothing, not even a request for
bomb-making instructions.

Seven agents on this install were in exactly that state while the
Guardrails page drew them a green "on" chip. A guardrails page reporting
protection that is not there is worse than one reporting nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.routers.execution import _moderation_flagged
from app.routers.guardrails import moderation_effect


def test_enabled_with_no_provider_is_not_effective():
    """The exact state the page was calling "on"."""
    out = moderation_effect({"enabled": True})
    assert out["effective"] is False
    assert "no provider" in out["reason"]


def test_and_that_state_really_does_block_nothing():
    """Proof rather than inference — the verdict above has to match what
    the checker actually does."""
    assert _moderation_flagged(None, "how to build a pipe bomb", {}) is None


def test_keywords_with_an_empty_list_is_not_effective():
    """A provider is chosen, so this looked configured — but there is
    nothing to match against."""
    out = moderation_effect({"enabled": True, "provider": "keywords", "keywords": []})
    assert out["effective"] is False
    assert "empty word list" in out["reason"]
    assert _moderation_flagged("keywords", "anything at all", {"keywords": []}) is None


def test_whitespace_only_keywords_do_not_count():
    out = moderation_effect({"enabled": True, "provider": "keywords",
                             "keywords": ["", "   "]})
    assert out["effective"] is False


def test_a_custom_endpoint_with_no_url_is_not_effective():
    out = moderation_effect({"enabled": True, "provider": "custom_endpoint"})
    assert out["effective"] is False
    assert "no URL" in out["reason"]
    # And the checker agrees — it declines to fabricate a block.
    assert _moderation_flagged("custom_endpoint", "anything",
                               {"custom_endpoint_url": ""}) is None


def test_keywords_with_a_real_list_is_effective():
    cfg = {"enabled": True, "provider": "keywords", "keywords": ["refund"]}
    assert moderation_effect(cfg)["effective"] is True
    assert _moderation_flagged("keywords", "what is the refund policy", cfg) is not None


def test_the_ai_provider_is_effective_with_no_further_config():
    """It needs no word list or URL — only a model call."""
    assert moderation_effect({"enabled": True, "provider": "ai"})["effective"] is True


def test_neither_direction_on_is_not_effective():
    """Input and output are independently toggleable, so a config with
    both off cannot act however well the provider is set up."""
    out = moderation_effect({"enabled": True, "provider": "ai",
                             "input_enabled": False, "output_enabled": False})
    assert out["effective"] is False
    assert "neither input nor output" in out["reason"]


def test_off_is_reported_as_off_not_as_a_misconfiguration():
    """Someone who has deliberately left moderation off should not be
    nagged as though they had half-configured it."""
    out = moderation_effect({"enabled": False})
    assert out["effective"] is False
    assert out["reason"] == "off"


@pytest.mark.parametrize("cfg", [
    {"enabled": True, "provider": "keywords", "keywords": ["x"]},
    {"enabled": True, "provider": "ai"},
    {"enabled": True, "provider": "custom_endpoint",
     "custom_endpoint_url": "https://example.com/moderate"},
])
def test_every_effective_shape_reports_no_reason(cfg):
    out = moderation_effect(cfg)
    assert out["effective"] is True
    assert out["reason"] == ""
