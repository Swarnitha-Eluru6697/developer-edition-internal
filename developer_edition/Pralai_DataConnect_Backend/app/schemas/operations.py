from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError, model_validator

from app.schemas.operation_results import RESULT_MODEL_BY_OPERATION_TYPE, OperationResult


class Operation(BaseModel):
    """A long-running action's pollable status — see app/models/operations.py.

    `result`'s real shape depends on `operation_type` — see
    app/schemas/operation_results.py's RESULT_MODEL_BY_OPERATION_TYPE for
    the actual per-type contract each work callable is built against (GAP-11).
    The stored result is read back as its own operation_type's contract; a row persisted before that contract existed
    (or that no longer fits it) stays a plain dict instead of turning the read into a 500.
    """

    @model_validator(mode="before")
    @classmethod
    def _read_result_as_its_types_contract(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            return data
        model = RESULT_MODEL_BY_OPERATION_TYPE.get(data.get("operation_type"))
        if model is None:
            return data
        try:
            return {**data, "result": model.model_validate(data["result"])}
        except ValidationError:
            return data  # legacy row: leave it as stored

    id: str
    operation_type: str
    resource_type: str
    resource_id: str
    status: str
    # GAP-04: independent of `status`. status=succeeded with result_status=invalid means the action took effect but its result
    # could not be recorded (error_code=OPERATION_RESULT_INVALID); `result` is null. null: not finished, or failed/stale.
    result_status: str | None = None
    result: OperationResult | None = None
    # BUG-11: error_message is now always a SafeError's generic, non-leaking
    # message (app/services/safe_error.py) — error_code/retryable are the
    # rest of that classification, correlation_id ties it back to the
    # (redacted) diagnostic log entry with the real cause.
    error_message: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    correlation_id: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
