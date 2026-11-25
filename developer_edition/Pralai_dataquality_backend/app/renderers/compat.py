"""Renderer identity and engine compatibility (DQ-ART-004 / DQ-014).

RENDERER_VERSION is the semantic version of the compile logic — bump it
whenever an unchanged input would compile to different output. It is part
of every ArtifactVersion hash.

SUPPORTED_ENGINES declares, per job_type, the engine version range the
artifacts this renderer emits are valid for. Every ArtifactVersion stores
this declaration; apply refuses (with an actionable diagnostic) when the
target engine version — the FlinkSQL runner's structured plan reports the
Flink version; the dbt runner re-checks its own dbt-core version against
the declaration sent with the artifact — is outside it.

Why these floors: the FlinkSQL artifact uses window TVFs (TUMBLE, 1.13+),
JSON_OBJECT (1.15+) and FIRST_VALUE/LAST_VALUE in windowed aggregates; it
deliberately avoids ARRAY_AGG (1.20+) so it runs on the platform's 1.19
cluster. dbt artifacts use schema.yml v2 + dbt_utils.expression_is_true
with `config:` blocks (dbt-core 1.5+).
"""
from __future__ import annotations

import re
from typing import Any

from app.core import quality_contract

RENDERER_VERSION = "2.0.0"
HASH_VERSION = 2

SUPPORTED_ENGINES: dict[str, dict[str, Any]] = {
    "dbt_project": {
        "engine": "dbt-core", "min": "1.5.0", "max_exclusive": "2.0.0",
        "artifact": "dbt-schema-yml-v2", "requires_packages": ["dbt-labs/dbt_utils"],
    },
    "flink_job": {
        "engine": "flink", "min": "1.17.0", "max_exclusive": "2.0.0",
        "artifact": "flinksql-quality-job",
        "quality_contract": quality_contract.CONTRACT_VERSION,
    },
    "python_job": {
        "engine": "great_expectations", "min": "0.18.0", "max_exclusive": "0.19.0",
        "artifact": "gx-suite+soda-checks",
    },
}

_NUM_RE = re.compile(r"\d+")


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(v).split(".")[:3]:
        m = _NUM_RE.match(chunk)
        if not m:
            break
        parts.append(int(m.group(0)))
    if not parts:
        raise ValueError(f"unparseable version {v!r}")
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def supported_range(job_type: str) -> dict[str, Any]:
    return dict(SUPPORTED_ENGINES[job_type])


def in_range(version: str, spec: dict[str, Any]) -> bool:
    try:
        v = parse_version(version)
    except ValueError:
        return False
    return parse_version(spec["min"]) <= v < parse_version(spec["max_exclusive"])


def check_engine_compatibility(
    job_type: str, target_engine_version: str | None,
    ops_pins: dict[str, list[str]] | None = None,
) -> dict[str, Any] | None:
    """None when compatible, else an actionable diagnostic."""
    spec = SUPPORTED_ENGINES.get(job_type)
    if spec is None:
        return {"reason": "unknown_job_type", "error": f"No renderer for job_type {job_type!r}"}
    rng = f"{spec['engine']} >={spec['min']},<{spec['max_exclusive']}"
    if not target_engine_version:
        return {
            "reason": "engine_version_unknown",
            "error": (f"The target {spec['engine']} version is unknown, so compatibility with "
                      f"this artifact ({rng}) cannot be verified."),
            "renderer_version": RENDERER_VERSION, "supported_range": spec,
            "remediation": "Make sure the runner reports its engine version (FlinkSQL runner "
                           "structured plan `engine_version`, DBTRUNNER_ENGINE_VERSION).",
        }
    if not in_range(target_engine_version, spec):
        return {
            "reason": "engine_version_incompatible",
            "error": (f"Artifact rendered by renderer {RENDERER_VERSION} supports {rng}, but the "
                      f"target runs {spec['engine']} {target_engine_version}."),
            "renderer_version": RENDERER_VERSION, "target_engine_version": target_engine_version,
            "supported_range": spec,
            "remediation": (f"Upgrade/downgrade the {spec['engine']} runtime into {rng}, or deploy "
                            "a DQ renderer release that supports this engine version."),
        }
    pins = (ops_pins or {}).get(job_type) or []
    if pins and not any(target_engine_version == p or target_engine_version.startswith(p + ".")
                        for p in pins):
        return {
            "reason": "engine_version_not_pinned",
            "error": (f"{spec['engine']} {target_engine_version} is inside the renderer's range "
                      f"but this deployment only allows {sorted(pins)} for {job_type}."),
            "renderer_version": RENDERER_VERSION, "target_engine_version": target_engine_version,
            "supported_range": spec, "allowed_by_deployment": sorted(pins),
            "remediation": "Confirm the runner fleet runs this version, then add it to "
                           "SUPPORTED_ENGINE_VERSIONS.",
        }
    return None
