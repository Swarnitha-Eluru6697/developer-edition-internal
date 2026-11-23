"""Error envelope — same shape as dbt-runner (code/message/details)."""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException


class DQServiceError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(status_code=status_code,
                         detail={"code": code, "message": message, "details": details or {}})


def not_found(resource: str, resource_id: str) -> DQServiceError:
    return DQServiceError(404, "NOT_FOUND", f"{resource} not found",
                          {"resource": resource, "id": resource_id})

def forbidden(reason: str) -> DQServiceError:
    return DQServiceError(403, "FORBIDDEN", reason)

def validation_failed(field: str, message: str) -> DQServiceError:
    return DQServiceError(400, "VALIDATION_FAILED", message, {"field": field})

def conflict(message: str) -> DQServiceError:
    return DQServiceError(409, "CONFLICT", message)

def system_rule_immutable() -> DQServiceError:
    return DQServiceError(409, "SYSTEM_RULE_IMMUTABLE",
                          "System rules cannot be modified or deleted")
