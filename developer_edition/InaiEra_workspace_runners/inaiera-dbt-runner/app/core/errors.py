"""Error envelope and common error helpers."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class DbtRunnerError(HTTPException):
    """Standard error with the documented envelope shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message, "details": details or {}},
        )


def not_found(resource: str, resource_id: str) -> DbtRunnerError:
    return DbtRunnerError(
        404, "NOT_FOUND", f"{resource} not found",
        {"resource": resource, "id": resource_id},
    )


def forbidden(reason: str) -> DbtRunnerError:
    return DbtRunnerError(403, "FORBIDDEN", reason)


def validation_failed(field: str, message: str) -> DbtRunnerError:
    return DbtRunnerError(
        400, "VALIDATION_FAILED", message, {"field": field},
    )


def conflict(
    message: str, *, code: str = "CONFLICT", resource: str | None = None, field: str | None = None,
    details: dict[str, Any] | None = None,
) -> DbtRunnerError:
    """THE 409 (X-008) — the same shape in Workspace, the dbt Runner and the Flink Runner:

        {"code": <STABLE, UPPER_SNAKE>, "message": <human text>, "details": {"resource": <kind>, "field": <name>, ...}}

    `code` is what a client branches on, `details.resource` the kind of thing that collided (workspace, file, job, project ...) and
    `details.field` the attribute to change (for a composite key, the names joined with `+`; absent when no single field applies). The bare
    `CONFLICT` code exists only for the unmapped safety net below — every deliberate conflict names its reason."""
    d = dict(details or {})
    if resource:
        d["resource"] = resource
    if field:
        d["field"] = field
    return DbtRunnerError(409, code, message, d)


# Unique constraint / unique index NAME (as Postgres reports it) -> (code, resource, field). Every unique rule of this service's models is
# listed (tests/test_error_contract.py fails on one that is missing), so a race on any of them is the same 409 as the friendly pre-check.
CONSTRAINT_CONFLICTS: dict[str, tuple[str, str, str]] = {
    "uq_project_repo_ref": ("PROJECT_ALREADY_REGISTERED", "project", "git_url+git_ref+project_path"),
    "uq_dbtrunner_git_cred": ("GIT_CREDENTIAL_EXISTS", "git_credential", "provider"),
    "uq_metadata_outbox_idempotency_key": ("OUTBOX_EVENT_EXISTS", "metadata_outbox_event", "idempotency_key"),
    "uq_runs_idempotency_key": ("RUN_ALREADY_EXISTS", "run", "idempotency_key"),
    "uq_scheduled_occurrence": ("OCCURRENCE_ALREADY_CLAIMED", "scheduled_occurrence", "scheduled_fire_time"),
}


def unique_violation(exc: BaseException) -> str | None:
    """The violated constraint's name if `exc` is a Postgres UNIQUE violation (SQLSTATE 23505), else None."""
    cause = getattr(getattr(exc, "orig", None), "__cause__", None) or getattr(exc, "orig", None)
    sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    if sqlstate != "23505":
        return None
    return getattr(cause, "constraint_name", None) or ""


def conflict_for_unique_violation(exc: BaseException, message: str | None = None, **override: Any) -> DbtRunnerError | None:
    """The standard 409 for a unique violation (None if `exc` is not one). Code/resource/field come from CONSTRAINT_CONFLICTS unless overridden."""
    name = unique_violation(exc)
    if name is None:
        return None
    code, resource, field = CONSTRAINT_CONFLICTS.get(name, ("CONFLICT", "resource", ""))
    code, resource, field = override.get("code") or code, override.get("resource") or resource, override.get("field") or field
    return conflict(message or f"A {resource} with this {field or 'value'} already exists.", code=code, resource=resource,
                    field=field or None, details=override.get("details"))


async def flush_or_conflict(db, message: str | None = None, *, constraint: str | None = None, **override: Any) -> None:
    """Flush pending changes; a UNIQUE violation becomes the standard 409 (W-022, X-008) instead of an internal error.

    The up-front "is it taken?" check keeps the message friendly, and this catches the race it cannot — and any case it forgot. The failed
    flush has aborted the transaction, so it is rolled back before the conflict is raised; capture what you need from ORM objects BEFORE
    calling. With `constraint`, only a violation of THAT constraint is translated. Any other integrity error (a foreign key, a check) is a
    real bug and stays an error."""
    from sqlalchemy.exc import IntegrityError

    try:
        await db.flush()
    except IntegrityError as e:
        name = unique_violation(e)
        if name is None or (constraint and name and name != constraint):
            raise
        await db.rollback()
        raise conflict_for_unique_violation(e, message, **override) from None


