"""Validate service. Wraps DbtExecutor.validate_model() for the
POST /api/v1/validate endpoint that Workspace's codegen chat calls. No
project registration, no Git, no persistence — purely a parse/compile/
dry-run check on a candidate model so Claude can self-correct before the
user ever sees the code.
"""
from __future__ import annotations

from app.executors.dbt_executor import get_dbt_executor
from app.schemas.project import ValidateRequest, ValidateResponse


async def validate_model(request: ValidateRequest) -> ValidateResponse:
    executor = get_dbt_executor()
    result = executor.validate_model(
        path=request.path,
        sql=request.sql,
        schema_yml=request.schema_yml,
        engine=request.engine,
    )
    return ValidateResponse(
        valid=result["valid"],
        errors=result.get("errors", []),
        compiled_sql=result.get("compiled_sql"),
    )
