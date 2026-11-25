"""Semantic caps for JSON API request content.

The raw-body middleware was scoped to /api/uploads, so every other JSON
endpoint — flows, prompts, structured inputs, batches, configuration —
had no size bound at all. Bytes are only half of it though: a 900 KB
flow is comfortably inside any reasonable body limit while still
containing 50,000 nodes, and every one of those nodes is validated,
deep-copied, hashed and persisted on save, then walked again on publish
and on every run. A byte limit alone answers "can we afford to read
this", not "can we afford to act on it".

So there are two layers, and they refuse for different reasons:

  * a default body limit in middleware.py, enforced at the ASGI layer
    before Pydantic parses anything, answering with 413;
  * the caps below, enforced by the handler that understands what the
    field means, answering with 422.

Every value is generous for real use. The largest flow in this repo's
own seed data has 9 nodes; the cap is 500.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException


# The default raw-body ceiling for every JSON API path, enforced in
# middleware.py before Pydantic parses anything. Deliberately far above
# any real request this platform makes (the largest flow in its own seed
# data is a few kilobytes) and far below what would cost real memory.
MAX_JSON_BODY_BYTES = int(os.getenv("MAX_JSON_BODY_BYTES", str(1024 * 1024)))


def _limit(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# A flow is walked by the validator, the sub-agent manifest builder, the
# webhook syncer, the compiler and every run. Node count multiplies all
# of that, and edge count multiplies the graph traversal on top.
MAX_FLOW_NODES = _limit("MAX_FLOW_NODES", 500)
MAX_FLOW_EDGES = _limit("MAX_FLOW_EDGES", 1500)

# Nesting is the one that is invisible in a size check: a few hundred
# kilobytes of deeply nested arrays costs recursion, not bytes, in every
# validator and serializer that walks it.
MAX_JSON_DEPTH = _limit("MAX_JSON_DEPTH", 32)

# A prompt is sent to a provider and billed by the token. This is not a
# security boundary so much as a guard against an accident that costs
# real money.
MAX_PROMPT_CHARS = _limit("MAX_PROMPT_CHARS", 100_000)

# One run's own input. Generous — a pasted document is a legitimate
# input — but not unbounded.
MAX_RUN_INPUT_CHARS = _limit("MAX_RUN_INPUT_CHARS", 200_000)
MAX_STRUCTURED_INPUT_FIELDS = _limit("MAX_STRUCTURED_INPUT_FIELDS", 200)

# A batch runs one agent per row, synchronously. 10,000 rows is not a
# large JSON body and is hours of provider calls.
MAX_BATCH_ROWS = _limit("MAX_BATCH_ROWS", 500)


def json_depth(value: Any, _depth: int = 0) -> int:
    """How deeply nested a decoded JSON value is.

    Iterative rather than recursive past a point would be better still,
    but the cap is checked as it descends: it stops at MAX_JSON_DEPTH + 1
    instead of walking a structure that is already too deep, so a
    pathological body cannot make the CHECK expensive.
    """
    if _depth > MAX_JSON_DEPTH:
        return _depth
    if isinstance(value, dict):
        return max((json_depth(v, _depth + 1) for v in value.values()), default=_depth)
    if isinstance(value, (list, tuple)):
        return max((json_depth(v, _depth + 1) for v in value), default=_depth)
    return _depth


def reject(detail: str) -> None:
    """422, not 413: the body was readable and well-formed, and what is
    wrong with it is its meaning. A 413 here would tell a caller to send
    fewer bytes when the actual problem is too many nodes."""
    raise HTTPException(422, detail)


def check_json_depth(value: Any, what: str) -> None:
    depth = json_depth(value)
    if depth > MAX_JSON_DEPTH:
        reject(f"{what} nests more than {MAX_JSON_DEPTH} levels deep "
               f"({depth}+). Deep nesting costs recursion in every validator and "
               f"serializer that walks it, regardless of how few bytes it is.")


def check_flow_size(nodes: Any, edges: Any) -> None:
    """Bound a flow before it is persisted or validated.

    Checked at the save chokepoint rather than only at publish, so an
    oversized flow is refused when it arrives instead of being stored
    and re-walked on every subsequent read."""
    node_count = len(nodes or [])
    edge_count = len(edges or [])
    if node_count > MAX_FLOW_NODES:
        reject(f"This flow has {node_count} nodes; the limit is {MAX_FLOW_NODES}.")
    if edge_count > MAX_FLOW_EDGES:
        reject(f"This flow has {edge_count} edges; the limit is {MAX_FLOW_EDGES}.")
    check_json_depth({"nodes": nodes, "edges": edges}, "This flow")


def check_run_input(user_input: Any, structured_inputs: Any) -> None:
    if isinstance(user_input, str) and len(user_input) > MAX_RUN_INPUT_CHARS:
        reject(f"Input is {len(user_input)} characters; the limit is {MAX_RUN_INPUT_CHARS}.")
    if isinstance(structured_inputs, dict):
        if len(structured_inputs) > MAX_STRUCTURED_INPUT_FIELDS:
            reject(f"{len(structured_inputs)} structured input fields were sent; the limit is "
                   f"{MAX_STRUCTURED_INPUT_FIELDS}.")
        for key, value in structured_inputs.items():
            if isinstance(value, str) and len(value) > MAX_RUN_INPUT_CHARS:
                reject(f"Structured input {key!r} is {len(value)} characters; the limit is "
                       f"{MAX_RUN_INPUT_CHARS}.")
        check_json_depth(structured_inputs, "Structured inputs")


def check_batch_rows(rows: Any) -> None:
    count = len(rows or [])
    if count > MAX_BATCH_ROWS:
        reject(f"This batch has {count} rows; the limit is {MAX_BATCH_ROWS}. Each row is a real "
               f"agent run, so a large batch is hours of provider calls rather than a large "
               f"request.")
    check_json_depth(rows, "Batch rows")
