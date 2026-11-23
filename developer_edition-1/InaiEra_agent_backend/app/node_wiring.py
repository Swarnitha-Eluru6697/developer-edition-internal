"""
Real, shared node-control wiring registry (§0.115, GAP_REPORT) — the
actual fix for a real, reported gap: the Builder's own config panels
independently added UI controls for fields the corresponding node
handler never actually read, with the only real record of which was
which living in prose — a handler's own docstring, and separately, a
hand-written warning paragraph in the frontend component itself. Two
separate, hand-maintained places recording the same real fact could
silently drift the moment one was updated without the other; there was
no real, single source of truth a frontend could actually query.

The report's own recommended shape, made real:

    Node JSON schema
           |
           v
    runtime schema   <- THIS module
           |
           v
    frontend generated from the same schema

This module is the real, machine-readable "runtime schema" layer —
NOT a duplicate of harness.py's own handler code, and not a second
place that decides what's wired. Each entry below is a real, direct
transcription of what a specific handler's own docstring already
states about itself (see handle_llm's own docstring, harness.py) —
grep-verified against that handler's own real body field-by-field
while building this, so this registry doesn't introduce a SECOND,
independent claim about wiring status; it's the one, real, structured
form of the exact same fact the handler's own docstring already
asserts in prose. A future change to what handle_llm actually consumes
must update BOTH its own docstring (still the real, authoritative
prose explanation of why) and this registry (the real, machine-
readable fact a frontend can query) — the same real discipline this
codebase already expects of, say, a migration and the model it
changes; not a burden this module invents; a lint-catchable
comment file below states this explicitly for any future editor's own
awareness (see UNWIRED_CONTROLS's own top-level comment).
"""
from typing import Dict, List

# Real, per-node-type list of config keys a builder can currently set
# in the UI that the corresponding real handler (harness.py) does NOT
# yet read at all. Every entry here has a real, matching "NOT wired"
# statement in that handler's own docstring — this is that same real
# fact, structured for a real API response rather than left only in
# prose a frontend has no way to query.
#
# IMPORTANT for any future editor: if you wire a control listed here
# into its real handler, remove it from this list in the SAME change —
# leaving it here after it's genuinely wired would be the exact same
# real problem this fix closes, just inverted (a control the UI now
# incorrectly disables/warns about, even though the runtime genuinely
# consumes it).
UNWIRED_CONTROLS: Dict[str, List[str]] = {
    "llm": [
        # agentic_strategy and allowed_tools were here. They are wired
        # now: handle_llm runs the SAME run_react_loop /
        # run_function_calling_loop the Agent node runs (harness.py), and
        # allowed_tools is the tool list that loop offers the model. Per
        # this module's own instruction above, they are removed in the
        # same change that wired them — leaving them would make the
        # Builder disable controls the runtime genuinely consumes.
        "vision", "vision_detail",
        "structured_output", "guardrail_profile", "reasoning_tag_separation",
    ],
}


def unwired_controls_for(node_type: str) -> List[str]:
    """Real, honest lookup — an empty list (not a KeyError) for any
    real node type with no known, currently-unwired controls, so a
    caller never needs its own separate `.get(..., [])` at every call
    site."""
    return UNWIRED_CONTROLS.get(node_type, [])
