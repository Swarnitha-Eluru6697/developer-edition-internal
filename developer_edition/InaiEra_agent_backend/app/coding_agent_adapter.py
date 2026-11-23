"""
Real workflow-generation coding agent (§0.46, GAP_REPORT) - takes a plain-
language prompt and produces a real, structurally-valid flow graph, via
ANY OpenAI-compatible endpoint: mlx-lm running locally on Apple Silicon
for development, real vLLM serving a real Qwen model in production. This
is a deliberately SEPARATE integration from model_adapter.py's own
Anthropic client - every other LLM-touching node in this platform keeps
using that; this is the one, scoped exception, not a platform-wide model
swap.

Real design choice to reduce hallucination surface: the model is asked to
generate only the MIDDLE of a flow - real domain-logic nodes (llm,
condition, code, retrieval, tool, respond/answer, etc.) - never the
mandatory trigger/gov scaffold every real agent already gets from
create_agent's own default_flow. Those two nodes are always
deterministic and never LLM-authored, so the model's own real job is
narrower and the boilerplate can never be malformed. The model marks
which of its own generated nodes is the real entry point (wired after
gov) - everything else is discovered by which nodes are actually
reachable from real edges, exactly the same real reachability concept
the execution engine's own dead-node propagation (harness.py,
GAP_REPORT 0.40/0.41) already uses.

Real, honest mock fallback: with no CODING_AGENT_LLM_BASE_URL configured,
returns a clearly-labeled placeholder flow rather than either failing
outright or silently pretending to have generated something real - the
same honest pattern model_adapter.call_llm already uses for the
no-API-key case.
"""
import json
import os
import uuid
from typing import Any, Dict, List, Tuple

CODING_AGENT_LLM_BASE_URL = os.getenv("CODING_AGENT_LLM_BASE_URL", "")
CODING_AGENT_LLM_MODEL = os.getenv("CODING_AGENT_LLM_MODEL", "Qwen2.5-Coder-7B-Instruct")
CODING_AGENT_LLM_API_KEY = os.getenv("CODING_AGENT_LLM_API_KEY", "not-needed")

# A real, deliberately scoped v1 subset of the platform's own 26 real
# node types - the ones a generated workflow/chatapp most plausibly
# needs. Each entry is a real, compact schema (not the full prose from
# frontend/src/nodeDocs.ts, which is written for a human reading
# documentation, not for constraining a model's own structured output)
# - a genuinely different real artifact serving a different real
# purpose, not a second copy of the same content. Expanding real
# coverage to more of the 26 types is a stated, reasonable follow-on.
NODE_SCHEMA_CATALOG: Dict[str, Dict[str, Any]] = {
    "llm": {
        "description": "Calls a language model with a system/user prompt.",
        "config": {"system_prompt": "string, may reference {{var}}", "user_prompt": "string, may reference {{var}}"},
    },
    "condition": {
        "description": "Branches true/false based on a real expression.",
        "config": {"expression": "a real boolean comparison, e.g. '{{score}} >= 70'"},
        "handles": ["true", "false"],
    },
    "code": {
        "description": "Runs real, sandboxed Python. main() receives arg1/arg2, returns the real result.",
        "config": {"code": "python source defining def main(**kwargs): ...", "output_name": "string, defaults to 'result'"},
    },
    "template": {
        "description": "Renders text with real {{var}} substitution.",
        "config": {"template": "string with {{var}} references"},
    },
    "retrieval": {
        "description": "Real semantic search against configured Knowledge Bases.",
        "config": {"query": "string, may reference {{var}}", "knowledge_bases": "array of KB names"},
    },
    "classifier": {
        "description": "Picks one of several named classes via a real model call.",
        "config": {"classes": "array of class name strings"},
        "handles": "one per class name",
    },
    "parameter_extractor": {
        "description": "Extracts structured parameters from unstructured text via a real model call.",
        "config": {"input_variable": "string", "parameters": "array of {name, type, description, required}"},
    },
    "hitl": {
        "description": "Genuinely pauses for human input, then resumes.",
        "config": {"form_content": "string, may reference {{var}}", "actions": "array of {id, title, style}"},
        "handles": "one per action id",
    },
    "answer": {
        "description": "Chatflow apps only - streams content to the user. A flow may have more than one.",
        "config": {"content": "string, may reference {{var}} including {{input}}"},
    },
    "respond": {
        "description": "Workflow apps only - the flow's single final output. Never generate this node - it always already exists.",
        "config": {},
    },
}

_SYSTEM_PROMPT = """You design agent workflow graphs for a real platform. You will be given a plain-language request. Respond with ONLY a single real JSON object (no markdown fences, no prose) matching this exact shape:

{
  "nodes": [{"id": "string", "type": "one of the real types below", "config": {...}}],
  "edges": [{"source": "node id", "target": "node id", "source_handle": "null, or a real handle name for condition/classifier/hitl"}],
  "entry_node_id": "the id of the first real node - this is what gets wired after the platform's own trigger/gov scaffold"
}

Never include a trigger or gov node - those always already exist. Every workflow-type request must end at a real "respond"-bound node (the platform supplies the actual respond node; just make sure your last real node's own output is the one meant to reach it). Every chatflow-type request should end with at least one real "answer" node.

Real node types and their real config shapes:
""" + json.dumps(NODE_SCHEMA_CATALOG, indent=2)


class CodingAgentGenerationError(Exception):
    """A real, honest failure - the model's own output didn't parse as
    JSON, or didn't reference only real node types, or referenced a
    real edge to/from a node id that doesn't exist. Never silently
    patched into something that merely looks valid."""


class CodingAgentUnavailable(Exception):
    """A real, honest, DIFFERENT failure from CodingAgentGenerationError
    - the real endpoint (mlx-lm locally, real vLLM in production)
    couldn't be reached at all, or timed out, rather than responding
    with something invalid. Kept as its own real exception type so
    routers/agents.py can return the right real HTTP status for each -
    503 here (nothing to blame the model's own output for), 422 for a
    genuine validation failure."""


def _mock_generate(prompt: str) -> Dict[str, Any]:
    """Real, honest mock - used only when no real endpoint is
    configured. Clearly labeled, never presented as a genuine model
    response, matching model_adapter.call_llm's own established
    pattern for the no-API-key case."""
    node_id = f"n-mock-{uuid.uuid4().hex[:6]}"
    return {
        "nodes": [{
            "id": node_id, "type": "llm",
            "config": {
                "system_prompt": f"[MOCK - no CODING_AGENT_LLM_BASE_URL configured] Generated in place of a real response to: {prompt[:150]}",
                "user_prompt": "{{input}}",
            },
        }],
        "edges": [],
        "entry_node_id": node_id,
        "_mock": True,
    }


def _call_real_llm(prompt: str) -> Dict[str, Any]:
    from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
    client = OpenAI(base_url=CODING_AGENT_LLM_BASE_URL, api_key=CODING_AGENT_LLM_API_KEY)
    try:
        completion = client.chat.completions.create(
            model=CODING_AGENT_LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
    except (APIConnectionError, APITimeoutError) as e:
        raise CodingAgentUnavailable(
            f"Couldn't reach the coding agent's LLM endpoint at {CODING_AGENT_LLM_BASE_URL} — is it running? ({e})"
        ) from e
    except APIStatusError as e:
        raise CodingAgentUnavailable(f"The coding agent's LLM endpoint returned a real error: {e}") from e
    raw = completion.choices[0].message.content or ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        raise CodingAgentGenerationError(f"The model's own response wasn't valid JSON: {e}") from e


def _validate_generated_flow(data: Dict[str, Any]) -> Tuple[List[dict], List[dict], str]:
    """Real, honest structural validation - before anything from this
    is ever persisted."""
    if not isinstance(data, dict):
        raise CodingAgentGenerationError("The model's own response wasn't a real JSON object.")
    nodes = data.get("nodes")
    edges = data.get("edges", [])
    entry_node_id = data.get("entry_node_id")
    if not isinstance(nodes, list) or not nodes:
        raise CodingAgentGenerationError("The model's own response had no real nodes.")
    if not entry_node_id:
        raise CodingAgentGenerationError("The model's own response didn't name a real entry_node_id.")

    node_ids = set()
    for n in nodes:
        if not isinstance(n, dict) or "id" not in n or "type" not in n:
            raise CodingAgentGenerationError("A generated node was missing a real id or type.")
        if n["type"] not in NODE_SCHEMA_CATALOG:
            raise CodingAgentGenerationError(f"The model referenced an unknown node type: '{n['type']}'.")
        if n["type"] in ("trigger", "gov", "respond"):
            raise CodingAgentGenerationError(f"The model generated a real '{n['type']}' node - those are never LLM-authored.")
        if n["id"] in node_ids:
            raise CodingAgentGenerationError(f"Duplicate real node id: '{n['id']}'.")
        node_ids.add(n["id"])

    if entry_node_id not in node_ids:
        raise CodingAgentGenerationError(f"entry_node_id '{entry_node_id}' doesn't match any real generated node.")

    for e in edges:
        if not isinstance(e, dict) or "source" not in e or "target" not in e:
            raise CodingAgentGenerationError("A generated edge was missing a real source or target.")
        if e["source"] not in node_ids or e["target"] not in node_ids:
            raise CodingAgentGenerationError(f"A generated edge referenced a real node id that doesn't exist: {e}")

    return nodes, edges, entry_node_id


def generate_flow(prompt: str) -> Dict[str, Any]:
    """The real, top-level entry point - returns {nodes, edges,
    entry_node_id}, already validated, ready for routers/agents.py to
    splice after the real trigger/gov scaffold. Raises
    CodingAgentGenerationError on any real failure - callers decide
    how to surface that, this function never silently degrades."""
    if not CODING_AGENT_LLM_BASE_URL:
        raw = _mock_generate(prompt)
    else:
        raw = _call_real_llm(prompt)
    nodes, edges, entry_node_id = _validate_generated_flow(raw)
    return {"nodes": nodes, "edges": edges, "entry_node_id": entry_node_id, "mock": raw.get("_mock", False)}
