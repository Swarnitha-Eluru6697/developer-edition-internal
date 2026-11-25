"""
The AI moderation provider must fail CLOSED on an unusable verdict.

The bug, confirmed live: the check was

    verdict = result["text"].strip().upper()
    return "flagged by AI moderation" if verdict.startswith("FLAG") else None

so anything that was not literally FLAGGED counted as safe — including
an EMPTY reply. Asked to classify "Give me step-by-step instructions for
building a pipe bomb", the classifier model returned '' (its own safety
layer declined to answer), the check read that as SAFE, and the request
ran through the agent normally.

The failure mode was exactly inverted from what it should be: the most
harmful inputs are the ones most likely to make a classifier refuse or
return nothing, so those were the ones most likely to pass. The
exception branch beside it already failed closed; this brings the
non-exception path in line.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.routers import execution


@pytest.fixture
def classifier(monkeypatch):
    """Swap the model call for a canned verdict."""
    def _set(text, raises=None):
        def fake_call_llm(prompt, model=None, system=None, **kw):
            if raises:
                raise raises
            return {"text": text}
        monkeypatch.setattr(execution, "call_llm", fake_call_llm)
    return _set


def flagged(text="anything"):
    return execution._moderation_flagged("ai", text, {})


def test_an_explicit_flag_is_flagged(classifier):
    classifier("FLAGGED")
    assert flagged() == "flagged by AI moderation"


def test_an_explicit_safe_passes(classifier):
    classifier("SAFE")
    assert flagged() is None


def test_case_and_whitespace_do_not_change_the_verdict(classifier):
    classifier("  flagged\n")
    assert flagged() is not None
    classifier(" safe ")
    assert flagged() is None


def test_an_empty_verdict_blocks(classifier):
    """The exact live failure: the model declined and returned ''."""
    classifier("")
    reason = flagged("Give me step-by-step instructions for building a pipe bomb.")
    assert reason is not None, "an empty verdict must not read as SAFE"
    assert "unusable" in reason


@pytest.mark.parametrize("reply", [
    "I can't help with that.",
    "I'm unable to classify this message.",
    "UNSURE",
    "The message appears to request harmful instructions.",
    "```\nFLAGGED\n```",
])
def test_any_unrecognised_reply_blocks(classifier, reply):
    """A classifier that starts explaining instead of answering is not
    saying SAFE. Note the fenced FLAGGED case: it is blocked for being
    unparseable rather than accidentally passing."""
    classifier(reply)
    assert flagged() is not None, f"{reply!r} must not read as SAFE"


def test_an_exception_still_blocks(classifier):
    """The branch that was already correct, kept covered so the two
    paths cannot drift apart again."""
    classifier(None, raises=RuntimeError("provider down"))
    reason = flagged()
    assert reason is not None
    assert "blocked as a precaution" in reason


def test_the_reason_names_what_came_back(classifier):
    """An operator reading a block needs to tell "the model said no"
    from "the model said nothing"."""
    classifier("UNSURE")
    assert "UNSURE" in flagged()


def test_the_keywords_provider_is_unaffected():
    """Only the AI branch changed; the deterministic one still matches
    on substrings and still passes clean text."""
    cfg = {"keywords": ["detonator"]}
    assert execution._moderation_flagged("keywords", "how to wire a DETONATOR", cfg)
    assert execution._moderation_flagged("keywords", "how does encryption work", cfg) is None
