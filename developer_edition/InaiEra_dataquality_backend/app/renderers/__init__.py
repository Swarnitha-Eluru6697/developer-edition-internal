"""Rendering engine — pure functions (no DB, no I/O) that compile ruleset
items into engine artifacts. See the submodules:

  params      typed rule parameters
  security    engine-specific quoting / literal rendering / custom_sql lexer
  capability  engine capability matrix + per-rule supported/unsupported/degraded
  plan        the FlinkSQL runner's StructuredJobPlan
  dbt / flink / python   the renderers
  validation  structural validation of compiled artifacts
  compat      renderer version + supported engine versions

Boundary with UCS: these produce the CODE that produces results; runners
execute it and results flow to UCS.
"""
from app.renderers.capability import ENGINE_CAPABILITY_MATRIX, classify_items  # noqa: F401
from app.renderers.compat import (  # noqa: F401
    HASH_VERSION, RENDERER_VERSION, SUPPORTED_ENGINES, check_engine_compatibility,
)
