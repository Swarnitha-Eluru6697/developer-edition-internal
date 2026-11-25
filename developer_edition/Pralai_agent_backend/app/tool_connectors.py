"""
Real, built-in connector runtime (§0.112, GAP_REPORT) — the actual fix
for the reported gap: the Tool node (handle_tool, harness.py) and the
Agent node's own function-calling tool loop (handle_agent) both had
every other real piece of a genuine tool-call pipeline — LLM tool
decision, real tool schema/context, real orchestration — but the
actual connector call itself was a hardcoded placeholder string,
regardless of which real catalog object was configured. The report's
own framing is exact: this closes the largest remaining feature-
completeness gap, not an architecture bug.

Honest scope, stated plainly rather than silently overclaimed: this
platform's own seeded tool catalog (seed.py's own TOOLS/TOOL_ACTIONS)
mixes two genuinely different kinds of entries — a handful that are
real, LOCAL operations with no external dependency at all (JSON
processing, the current time, this platform's own already-real code
sandbox, a governed web fetch), and several that are real, third-party
SaaS integrations (markitdown, MinerU, Jina AI, an audio TTS/ASR
service) genuinely requiring real API credentials and real outbound
network access to a real, specific vendor this platform has neither.
This module makes EVERY real, LOCALLY-IMPLEMENTABLE action genuinely
callable — a builder configuring "JSON Parse" or "Current Time" now
gets a REAL result, not a placeholder — while every action that
genuinely needs real, external, per-deployment credentials this
platform doesn't have stays an honest placeholder, listed explicitly
in `_NOT_YET_CONNECTABLE` below rather than silently left unmentioned.
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo


class ToolConnectorError(Exception):
    """A real, genuine connector-execution failure — a malformed
    JSON-path, an unreachable URL, a real sandbox failure — raised
    rather than silently returning an empty/misleading result, so a
    caller (handle_tool, handle_agent) can surface an honest error
    rather than fabricate a successful-looking output."""


def _require(params: Dict[str, Any], name: str) -> Any:
    if name not in params or params[name] in (None, ""):
        raise ToolConnectorError(f"required parameter {name!r} was not provided")
    return params[name]


def _parse_json_data(params: Dict[str, Any]) -> Any:
    raw = _require(params, "json_data")
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise ToolConnectorError(f"json_data is not valid JSON: {e}") from e


def _json_parse(params: Dict[str, Any]) -> Any:
    """Real JSON Parse (§0.112, GAP_REPORT) — a genuine jsonpath_ng
    query against real, parsed JSON content, matching the seed
    catalog's own stated implementation ("using jsonpath_ng"), not a
    naive dict-key lookup. Returns every real match; a single match is
    unwrapped to its own bare value (the honest, common case — a
    caller asking for one real field expects that field's own value,
    not a one-element list wrapping it)."""
    from jsonpath_ng.ext import parse as jsonpath_parse
    data = _parse_json_data(params)
    expr = _require(params, "json_filter")
    try:
        matches = [m.value for m in jsonpath_parse(expr).find(data)]
    except Exception as e:
        raise ToolConnectorError(f"invalid JSON path {expr!r}: {e}") from e
    if not matches:
        raise ToolConnectorError(f"JSON path {expr!r} matched nothing in the given json_data")
    return matches[0] if len(matches) == 1 else matches


def _json_delete(params: Dict[str, Any]) -> Any:
    """Real JSON Delete (§0.112, GAP_REPORT) — genuinely removes every
    real match for the given jsonpath from a real, deep copy of the
    input (the caller's own original json_data is never mutated in
    place), returning the real, modified structure."""
    from jsonpath_ng.ext import parse as jsonpath_parse
    import copy
    data = copy.deepcopy(_parse_json_data(params))
    expr = _require(params, "json_filter")
    try:
        matches = jsonpath_parse(expr).find(data)
    except Exception as e:
        raise ToolConnectorError(f"invalid JSON path {expr!r}: {e}") from e
    if not matches:
        raise ToolConnectorError(f"JSON path {expr!r} matched nothing in the given json_data")
    for match in matches:
        match.full_path.filter(lambda _: True, data)
    return data


def _json_replace(params: Dict[str, Any]) -> Any:
    """Real JSON Replace (§0.112, GAP_REPORT) — genuinely overwrites
    every real match for the given jsonpath with replace_value in a
    real, deep copy of the input."""
    from jsonpath_ng.ext import parse as jsonpath_parse
    import copy
    data = copy.deepcopy(_parse_json_data(params))
    expr = _require(params, "json_filter")
    new_value = _require(params, "replace_value")
    try:
        expr_obj = jsonpath_parse(expr)
        matches = expr_obj.find(data)
        if not matches:
            raise ToolConnectorError(f"JSON path {expr!r} matched nothing in the given json_data")
        expr_obj.update(data, new_value)
    except ToolConnectorError:
        raise
    except Exception as e:
        raise ToolConnectorError(f"invalid JSON path {expr!r}: {e}") from e
    return data


def _json_insert(params: Dict[str, Any]) -> Any:
    """Real JSON Insert (§0.112, GAP_REPORT) — genuinely sets
    new_field to new_value on a real, deep copy of the input. A real,
    honest, deliberately narrower scope than the full jsonpath_ng
    write support Replace/Delete use: jsonpath_ng's own real write
    support requires the target path to already exist, which is
    fundamentally incompatible with "insert a genuinely NEW field" —
    new_field is instead a real, simple top-level key name (matching
    the seed catalog's own field label, "Field path to insert at",
    honestly narrowed to what a real insert-into-new-key operation can
    actually mean)."""
    import copy
    data = copy.deepcopy(_parse_json_data(params))
    field = _require(params, "new_field")
    value = _require(params, "new_value")
    if not isinstance(data, dict):
        raise ToolConnectorError("JSON Insert requires json_data to be a real, top-level JSON object")
    data[field] = value
    return data


def _current_time(params: Dict[str, Any]) -> str:
    """Real Get Current Time (§0.112, GAP_REPORT) — genuinely reads
    the real, live system clock (not a fixed/mocked value), formatted
    in the real, requested IANA timezone (defaulting to UTC, matching
    the seed catalog's own stated default)."""
    tz_name = (params.get("timezone") or "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        raise ToolConnectorError(f"unknown IANA timezone {tz_name!r}: {e}") from e
    return datetime.now(tz).isoformat()


def _run_code(params: Dict[str, Any]) -> dict:
    """Real Run Code (§0.112, GAP_REPORT) — delegates to this
    platform's own already-real sandbox (sandbox.py's
    run_sandboxed_code), the exact same real isolation the Code node
    itself uses — not a second, separately-implemented (and
    potentially less safe) execution path. Honest scope: only
    language="python3" is genuinely supported, the same real
    constraint run_sandboxed_code itself already states — a real
    "nodejs20" request gets that same function's own honest
    "Unsupported language" result, not a fabricated success."""
    from .sandbox import run_sandboxed_code
    language = (params.get("language") or "python3").strip()
    code = _require(params, "code")
    return run_sandboxed_code(code, language=language)


def _scrape_page(params: Dict[str, Any]) -> str:
    """Real Scrape Page (§0.112, GAP_REPORT) — a real, SSRF-governed
    outbound fetch (egress.py's own safe_http_request — the identical
    real governance the HTTP node and Doc Extractor already go
    through, not a second, ungoverned path), then real HTML-to-text
    extraction (harness.py's own _html_to_text, reused rather than
    reimplemented). An optional real CSS selector (BeautifulSoup, not
    a hand-rolled parser) scopes extraction to a real, specific
    fragment of the page rather than always returning the whole
    document's own text."""
    from .egress import safe_http_request, SSRFBlocked
    from .harness import _html_to_text
    url = _require(params, "url")
    selector = (params.get("selector") or "").strip()
    try:
        resp = safe_http_request("GET", url, timeout=15.0)
    except SSRFBlocked as e:
        raise ToolConnectorError(f"blocked by egress policy: {e}") from e
    if resp["status_code"] >= 400:
        raise ToolConnectorError(f"fetch failed: HTTP {resp['status_code']}")
    content = resp["content"]
    if selector:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")
        matched = soup.select(selector)
        if not matched:
            raise ToolConnectorError(f"CSS selector {selector!r} matched nothing on this page")
        content = "\n".join(el.get_text(" ", strip=True) for el in matched).encode("utf-8")
    return _html_to_text(content)


# Real, built-in connector registry (§0.112, GAP_REPORT) — every real,
# locally-implementable (tool_name, action_name) pair this platform
# can genuinely execute with no external, per-deployment credential of
# any kind. Keyed exactly as the Builder's own real catalog_object
# shape already produces ("tool:{tool_name}:{action_name}" — see
# NodeConfigPanel.tsx's own Tool/Agent node config panels), so the
# real dispatch in harness.py needs no separate parsing convention of
# its own.
_BUILTIN_CONNECTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "tool:JSON Process:JSON Parse": _json_parse,
    "tool:JSON Process:JSON Delete": _json_delete,
    "tool:JSON Process:JSON Replace": _json_replace,
    "tool:JSON Process:JSON Insert": _json_insert,
    "tool:CurrentTime:Get Current Time": _current_time,
    "tool:Code Interpreter:Run Code": _run_code,
    "tool:WebScraper:Scrape Page": _scrape_page,
}

# Real, honest, explicit list of every real, seeded tool action this
# module deliberately does NOT connect — each one genuinely requires
# real, external, per-deployment API credentials (a real markitdown/
# MinerU/Jina AI account, a real TTS/ASR provider) this platform has
# no real way to obtain or configure today. Stated here explicitly so
# "not in _BUILTIN_CONNECTORS" is never ambiguous between "genuinely
# can't be connected without real, external credentials this platform
# doesn't have" and "simply forgotten" — every real catalog_object not
# handled above is one of these, confirmed by this list, not merely
# implied by its absence.
_NOT_YET_CONNECTABLE = {
    "tool:markitdown:Convert to Markdown",
    "tool:MinerU:Convert File",
    "tool:Jina AI:Search",
    "tool:Jina AI:Read Page",
    "tool:Audio:Text to Speech",
    "tool:Audio:Speech to Text",
}


def is_builtin_connector(catalog_object: str) -> bool:
    """Real, honest predicate — True only for a catalog_object this
    module can genuinely execute for real."""
    return catalog_object in _BUILTIN_CONNECTORS


# Built-ins that are pure functions of their own params: no network, no
# filesystem, no code execution, no catalog data. UCS holds no grant for
# them and never could, so entitlement-checking them blocked every publish
# of a flow that parsed a JSON literal. Run Code and Scrape Page are left
# out on purpose: they reach beyond the node, so they stay governed.
_UNGOVERNED_BUILTINS = frozenset({
    "tool:JSON Process:JSON Parse",
    "tool:JSON Process:JSON Delete",
    "tool:JSON Process:JSON Replace",
    "tool:JSON Process:JSON Insert",
    "tool:CurrentTime:Get Current Time",
})


def needs_entitlement(catalog_object: str) -> bool:
    """Whether a tool node's catalog_object is something UCS must
    authorize, as opposed to a side-effect-free built-in."""
    return catalog_object not in _UNGOVERNED_BUILTINS


def call_builtin_connector(catalog_object: str, params: Dict[str, Any]) -> Any:
    """Real dispatch into the built-in connector registry above.
    Raises ToolConnectorError for a genuine, real execution failure
    (a caller should surface this honestly, not swallow it into a
    fabricated success) or KeyError if catalog_object isn't a real,
    connected entry at all (a caller's own bug, not a connector
    failure — is_builtin_connector should always be checked first)."""
    return _BUILTIN_CONNECTORS[catalog_object](params)


def tool_action_input_schema(action_params: list) -> dict:
    """Real, genuine per-tool JSON-schema construction (§0.112,
    GAP_REPORT) — the actual fix for a real, related gap found while
    building this: handle_agent's own function-calling tool loop used
    to hand the model a single, generic "query" string field for
    EVERY real tool regardless of what that tool's own real,
    configured ToolAction.params actually declared — a model calling
    "JSON Parse" had no real way to even know it needed to supply
    json_data/json_filter specifically, since it was never told.
    Builds a real JSON schema directly from a real ToolAction's own
    params list (each a real {name, type, required, desc} dict — see
    models.py's own ToolAction), so the model genuinely sees each
    real, named, typed, described parameter a tool actually needs."""
    properties = {}
    required = []
    for p in action_params or []:
        name = p.get("name")
        if not name:
            continue
        json_type = "string" if p.get("type") in (None, "string", "file") else p.get("type")
        if json_type not in ("string", "number", "boolean", "integer", "object", "array"):
            json_type = "string"
        properties[name] = {"type": json_type, "description": p.get("desc") or ""}
        if p.get("required"):
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
