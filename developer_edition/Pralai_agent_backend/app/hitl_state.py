"""The durable resume state for a paused Human-Input node.

The reported gap: `_execute_flow` handed its ENTIRE runtime ctx to
`HitlRequest.ctx_snapshot`, a plaintext JSONB column. Runtime ctx is not
a small thing — it accumulates every node's output for the whole run, so
a pause persisted prompts, retrieved documents, tool results, HTTP
response bodies and, concretely, the HTTP node's own `headers_sent`
(harness.py), which is where an `Authorization: Bearer {{env.API_KEY}}`
lands once interpolate_config has substituted the real value. A
secondary plaintext copy of a secret undoes the encrypted-at-rest
design for EnvVar that put it there in the first place.

Four things, matching the report's own recommendations:

1. An explicit resume-state schema (RESUME_CTX_KEYS) rather than "the
   whole ctx". Derived by enumerating every top-level ctx key the
   handlers actually read, then removing the ones that cannot matter
   after a pause:

     _webhook_payload, _structured_inputs  read only by handle_trigger,
        which ran before the pause and never re-runs — resume starts at
        the node AFTER the hitl node. Both are caller-supplied payloads,
        so they are also the most likely to carry a bearer token.
     _llm_usage        reset per call; per-node accounting, not run state.
     _idempotency_key, _current_node_id    set per node execution.
        Persisting a stale one is not merely useless but wrong: it keys
        idempotency for a node that has already finished.
     _durable_execution   a per-run path flag the resume path sets itself.

2. Credentials, headers and cookies removed by key name, at any depth.

3. Secret-DERIVED values removed by value: anything matching a known
   EnvVar secret is replaced, wherever it ended up. This is the part
   that catches a secret which has already been interpolated into a
   prompt, a URL or a tool argument, where no key name gives it away.

4. Encrypted at rest, and deleted once the run that owned it finishes.

What this deliberately does NOT do is drop `vars`. Downstream nodes
reference upstream variables by name ({{var}}), so resuming without them
would produce a run that silently sees empty values — a correctness bug
traded for a security one. `vars` is scrubbed, not removed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

# Every top-level key the resumed flow genuinely needs. An allowlist:
# a key added to ctx later is absent from the snapshot until someone
# decides it belongs here, which is the safe direction to fail in.
RESUME_CTX_KEYS = frozenset({
    "input", "vars", "output", "structured_output",
    "agent_id", "agent_name", "invoking_user",
    # Governance and sub-agent state that IS read after a resume:
    # handle_gov reads _objects_to_check (dropping it would let a
    # post-resume gov node check nothing), handle_subagent reads the
    # manifest and the hop chain, and the LLM node reads the chat-history
    # feature flag.
    "_objects_to_check", "_subagent_manifest", "_agent_chain",
    "_chat_history_enabled",
})

# Key names whose VALUE is a credential or a transport header, at any
# depth. `headers_sent` is named explicitly because it is this
# platform's own field (harness.py's http_output) and is exactly where
# an interpolated Authorization header ends up.
SENSITIVE_KEY_RE = re.compile(
    r"(authorization|auth[_-]?header|cookie|set[_-]?cookie|headers?[_-]?sent|"
    r"params?[_-]?sent|query[_-]?params?|"
    r"api[_-]?key|access[_-]?key|secret|password|passwd|passphrase|credential|"
    r"bearer|private[_-]?key|session[_-]?id|csrf|x[_-]?api[_-]?key|token)",
    re.IGNORECASE,
)

REDACTED = "[redacted — not persisted in the HITL resume state]"

# Below this length a "secret" is too short to redact by value without
# corrupting ordinary text: a 3-character secret would blank out every
# incidental occurrence of those characters in every prompt.
MIN_SECRET_VALUE_LENGTH = 6

# A pause is resumed by a person, so the state has to survive a worker
# restart; encryption therefore has to be reversible with a key that
# outlives the process. With only an ephemeral key configured, the
# scrubbed state is stored as-is rather than sealed with a key that will
# not exist after a restart — which would turn every pause into an
# unresumable run. Scrubbing is unconditional either way; production
# already requires a durable key (secrets_crypto.check_production_key_requirement).
ENCRYPTED_FIELD = "__enc__"


def _is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and bool(SENSITIVE_KEY_RE.search(key))


def _secret_values(env_map: Optional[Mapping[str, str]]) -> list:
    if not env_map:
        return []
    return sorted(
        (str(v) for v in env_map.values() if v and len(str(v)) >= MIN_SECRET_VALUE_LENGTH),
        key=len, reverse=True,  # longest first, so a secret containing another is replaced whole
    )


def _scrub(value: Any, secrets: list, depth: int = 0) -> Any:
    """Recursively remove credentials by key name and secrets by value.

    Depth-bounded: a resume state is data, but it arrives from node
    handlers and nothing guarantees it is shallow, and a cycle would
    otherwise hang the pause.
    """
    if depth > 12:
        return REDACTED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = REDACTED if _is_sensitive_key(k) else _scrub(v, secrets, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secrets, depth + 1) for v in value]
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, REDACTED)
        return scrubbed
    return value


def resume_state(ctx: Mapping[str, Any], env_map: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The narrow, scrubbed state a paused run needs to continue."""
    secrets = _secret_values(env_map)
    state: Dict[str, Any] = {}
    for key in RESUME_CTX_KEYS:
        if key in ctx:
            state[key] = _scrub(ctx[key], secrets)
    state.setdefault("vars", {})
    return state


def encode_snapshot(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The value to store in the JSONB column: the state sealed with the
    platform's own Fernet key when that key is durable."""
    import json

    from .secrets_crypto import encrypt_secret, using_ephemeral_key

    plain = dict(state or {})
    if using_ephemeral_key():
        return plain
    sealed = encrypt_secret(json.dumps(plain, default=str))
    return {ENCRYPTED_FIELD: sealed} if sealed else plain


def decode_snapshot(stored: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The inverse. Accepts a plaintext snapshot too, so a row written
    before this change still resumes rather than failing closed on
    someone's paused work."""
    import json

    from .secrets_crypto import decrypt_secret

    if not stored:
        return {}
    if ENCRYPTED_FIELD not in stored:
        return dict(stored)
    try:
        decrypted = decrypt_secret(stored[ENCRYPTED_FIELD])
        return json.loads(decrypted) if decrypted else {}
    except Exception:
        # An undecryptable snapshot must not be mistaken for an empty
        # one: resuming with {} would run the rest of the flow against
        # silently missing variables. Callers check for this marker.
        return {"__undecryptable__": True}


def is_undecryptable(state: Optional[Mapping[str, Any]]) -> bool:
    return bool(state) and bool(state.get("__undecryptable__"))
