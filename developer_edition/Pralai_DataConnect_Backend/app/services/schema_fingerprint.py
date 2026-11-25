"""
Deterministic content fingerprint for a schema's field list.

Used by schema drift processing (see app/services/schema_service.py) to
record exactly what shape was compared at the moment drift was detected —
`SchemaDriftApproval.detected_fingerprint` (the observed schema) and
`current_fingerprint` (the approved schema it was compared against) — so
that row remains a faithful record of that comparison even if the approved
SchemaVersion is edited afterward.

The fingerprint is independent of field ordering (fields are sorted by name,
recursively) and of inference-only observational metadata (confidence,
null_only) — two schemas with the same contract-relevant shape always
fingerprint identically, regardless of which sample order produced them.
"""

import hashlib
import json
from typing import Any

# Deliberately the same contract-relevant key set as
# schema_service._CONTRACT_FIELD_KEYS — a fingerprint should reflect the
# approved-contract shape of a field, not how confident inference was.
_FINGERPRINT_KEYS = ("name", "type", "nullable", "primary_key", "fields", "items", "precision", "scale")


def _canonicalize(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical = []
    for field in sorted(fields, key=lambda f: f["name"]):
        entry = {key: field.get(key) for key in _FINGERPRINT_KEYS}
        if entry.get("fields"):
            entry["fields"] = _canonicalize(entry["fields"])
        if entry.get("items"):
            entry["items"] = _canonicalize([entry["items"]])[0]
        canonical.append(entry)
    return canonical


def compute_schema_fingerprint(fields: list[dict[str, Any]]) -> str:
    canonical = _canonicalize(fields or [])
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
