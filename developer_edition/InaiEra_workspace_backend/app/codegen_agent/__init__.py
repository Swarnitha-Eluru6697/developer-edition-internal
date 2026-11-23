"""Codegen agent — the harness that replaces the single Claude tool-use loop.

Implements the "model proposes, harness disposes" design: every generated
artifact passes a deterministic validation ladder before it can be delivered,
and failures route back to the stage that caused them rather than blindly
retrying the generator. Inference runs on self-hosted, in-perimeter models
via an OpenAI-compatible gateway — no external LLM providers.

Public surface used by app/services/codegen.py:
    Harness              — orchestrates ground -> plan -> generate -> validate -> repair
    HarnessDeps          — the collaborators the harness needs (model client, backend, publisher)
    HarnessOutcome       — the final result of one turn (delivered artifact or diagnosis)

The per-target context/validation behaviour lives in the existing
CodegenBackend classes (app/services/codegen.py); the harness calls them
through the CodegenBackend protocol so SQL/dbt/flinksql/python each keep
their own grounding and validation while sharing one orchestration loop.
"""
from __future__ import annotations

from app.codegen_agent.contracts import (
    Diagnostic,
    ErrorClass,
    GroundingPack,
    Plan,
    RepairRoute,
    RungResult,
    ValidationReport,
)
from app.codegen_agent.harness import Harness, HarnessDeps, HarnessOutcome

__all__ = [
    "Harness",
    "HarnessDeps",
    "HarnessOutcome",
    "GroundingPack",
    "Plan",
    "Diagnostic",
    "ValidationReport",
    "RungResult",
    "ErrorClass",
    "RepairRoute",
]
