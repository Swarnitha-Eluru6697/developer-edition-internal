"""After someone answers a Human Input node, the flow must continue.

The edge resolver marked every outgoing edge whose `source_handle` did
not equal the chosen action id as DEAD. A human-input node with one
plain exit has no handle on that edge, so nothing activated: the run
reported success, stopped at the hitl node, and returned a null output.

Observed on Showcase 7, whose spans ended at `hitl:resumed` with the
aggregator, output and respond nodes never reached.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.harness import _resolve_outgoing_edges


def by_source(edges):
    """The resolver takes edges grouped by their source node."""
    out = {}
    for e in edges:
        out.setdefault(e["source"], []).append(e)
    return out


def ctx(action_id=None):
    return {"vars": {} if action_id is None else {"hitl_action_id": action_id}}


HITL = {"id": "n4", "type": "hitl"}


def test_a_single_unnamed_exit_is_taken_whichever_action_was_chosen():
    """The reported case: one edge, no handle, action "continue"."""
    edges = [{"id": "e1", "source": "n4", "target": "n6"}]
    activated, dead = _resolve_outgoing_edges(HITL, ctx("continue"), by_source(edges))
    assert activated == {"e1"}
    assert dead == set()


def test_per_action_edges_still_route_by_action():
    """A form offering Approve and Reject wires an edge per action, and
    only the chosen one may activate."""
    edges = [
        {"id": "approve", "source": "n4", "target": "ok", "source_handle": "approve"},
        {"id": "reject", "source": "n4", "target": "no", "source_handle": "reject"},
    ]
    activated, dead = _resolve_outgoing_edges(HITL, ctx("approve"), by_source(edges))
    assert activated == {"approve"}
    assert dead == {"reject"}


def test_a_named_edge_wins_over_an_unnamed_one():
    """With both present the explicit route is the intended one."""
    edges = [
        {"id": "approve", "source": "n4", "target": "ok", "source_handle": "approve"},
        {"id": "fallback", "source": "n4", "target": "other"},
    ]
    activated, dead = _resolve_outgoing_edges(HITL, ctx("approve"), by_source(edges))
    assert activated == {"approve"}
    assert "fallback" in dead


def test_an_action_with_no_matching_edge_falls_back_to_the_plain_exit():
    """Adding a third button to a form should not strand the run."""
    edges = [
        {"id": "approve", "source": "n4", "target": "ok", "source_handle": "approve"},
        {"id": "plain", "source": "n4", "target": "after"},
    ]
    activated, _ = _resolve_outgoing_edges(HITL, ctx("escalate"), by_source(edges))
    assert activated == {"plain"}


def test_before_a_response_every_exit_is_open():
    """No action chosen yet — the node has not been answered, so this
    branch must not apply at all."""
    edges = [
        {"id": "e1", "source": "n4", "target": "a"},
        {"id": "e2", "source": "n4", "target": "b"},
    ]
    activated, dead = _resolve_outgoing_edges(HITL, ctx(None), by_source(edges))
    assert activated == {"e1", "e2"}
    assert dead == set()


def test_a_condition_node_is_untouched_by_this():
    """Its handles are true/false and must keep routing on branch_taken."""
    node = {"id": "n3", "type": "condition"}
    edges = [
        {"id": "t", "source": "n3", "target": "a", "source_handle": "true"},
        {"id": "f", "source": "n3", "target": "b", "source_handle": "false"},
    ]
    activated, dead = _resolve_outgoing_edges(node, {"vars": {"branch_taken": True}}, by_source(edges))
    assert activated == {"t"} and dead == {"f"}
