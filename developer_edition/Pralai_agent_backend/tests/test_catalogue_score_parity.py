"""The drawer's quality score must be the number UCS will compute.

The drawer showed 38 for an agent the catalogue scored 0 — same agent,
same data, two methods. This platform averaged the observed percentages;
UCS buckets each dimension's VERDICT (pass 100 / warn 70 / fail 0),
takes a weighted average over the dimensions present, then caps the
result when any dimension failed.

A second opinion on the same screen is worse than one imperfect number,
so the formula here reproduces UCS's exactly. These tests pin it: if
UCS's weights or cap change, they fail rather than the two views quietly
drifting apart again.

Reference: InaiEra-unified-catalog-backend
app/services/quality_service.py::recompute_score and
config.DQ_SCORE_WEIGHTS / DQ_FAIL_SCORE_CAP.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.agent_catalog import (UCS_DIMENSION, UCS_FAIL_SCORE_CAP, UCS_WEIGHTS,
                               catalogue_score)


def dims(**verdicts):
    return {name: {"verdict": v, "observed": None, "threshold": None,
                   "detail": "", "rows_scanned": 0}
            for name, v in verdicts.items()}


def test_all_passing_scores_100():
    assert catalogue_score(dims(accuracy="pass", reliability="pass", drift="pass")) \
        == (100, "pass")


def test_any_failing_dimension_caps_the_score():
    """UCS caps at DQ_FAIL_SCORE_CAP when the worst dimension fails, so a
    single failure cannot be averaged away by two passes."""
    score, status = catalogue_score(dims(accuracy="pass", reliability="fail", drift="pass"))
    assert status == "fail"
    assert score <= UCS_FAIL_SCORE_CAP


def test_the_worst_dimension_sets_the_status_not_the_average():
    """Two passes and a fail is a failing agent, whatever the mean says."""
    _, status = catalogue_score(dims(accuracy="pass", reliability="pass", drift="fail"))
    assert status == "fail"
    _, status = catalogue_score(dims(accuracy="pass", reliability="warn", drift="pass"))
    assert status == "warn"


def test_the_score_is_bucketed_not_the_observed_percentage():
    """The bug this file exists for: averaging observed percentages gave
    38 where the catalogue said 0. A dimension's verdict is worth
    100/70/0 regardless of how close to the threshold it landed."""
    a = catalogue_score(dims(accuracy="warn", reliability="warn", drift="warn"))
    assert a == (70, "warn"), "warn is worth 70, not whatever percentage was observed"


def test_nothing_measured_is_unknown_not_zero():
    """An agent with no eval runs and no runs has not scored zero; it has
    not been measured, and those are different problems."""
    assert catalogue_score(dims(accuracy="unknown", reliability="unknown")) == (None, "unknown")


def test_an_unmeasured_dimension_does_not_drag_the_score_down():
    """It is left out of the average rather than counted as a failure."""
    measured_only = catalogue_score(dims(accuracy="pass", reliability="pass"))
    with_unknown = catalogue_score(dims(accuracy="pass", reliability="pass", drift="unknown"))
    assert measured_only == with_unknown


def test_reliability_outweighs_accuracy():
    """UCS weights reliability 0.20 against validity 0.10 — an agent's
    run-success rate says more about it than any single eval set. Equal
    weighting would report a failing agent as merely borderline."""
    reliability_bad = catalogue_score(dims(accuracy="pass", reliability="warn"))[0]
    accuracy_bad = catalogue_score(dims(accuracy="warn", reliability="pass"))[0]
    assert reliability_bad < accuracy_bad


def test_the_agent_dimensions_are_named_the_way_ucs_weights_them():
    """Pushed as "custom" they landed in a bucket UCS's weights do not
    mention, so reliability and drift were scored at a default weight
    and collapsed into a single chip."""
    assert UCS_DIMENSION["reliability"] == "reliability"
    assert UCS_DIMENSION["drift"] == "drift"
    # accuracy has no entry in UCS's enum, so it rides on validity.
    assert UCS_DIMENSION["accuracy"] == "validity"
    for mapped in UCS_DIMENSION.values():
        assert mapped in UCS_WEIGHTS, f"{mapped} has no weight, so it would score by default"


@pytest.mark.parametrize("verdicts,expected", [
    ({"accuracy": "fail", "reliability": "fail", "drift": "pass"}, 33),
    ({"accuracy": "fail", "reliability": "fail", "drift": "fail"}, 0),
    ({"accuracy": "warn", "reliability": "pass", "drift": "pass"}, 93),
])
def test_known_values_observed_live(verdicts, expected):
    """Taken from live agents where both views were compared directly —
    testing chatflow-knowledge base (33) and Acme bot FAQ (93)."""
    assert catalogue_score(dims(**verdicts))[0] == expected
