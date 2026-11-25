# Conflict and error semantics (X-008)

One shape for the 409, in Workspace, the dbt Runner and the Flink Runner:

```json
{"detail": {"code": "WORKSPACE_NAME_TAKEN", "message": "Workspace with name 'x' already exists",
            "details": {"resource": "workspace", "field": "name"}}}
```

* `code` — stable, UPPER_SNAKE, what a client branches on. The bare `CONFLICT` exists only as the safety-net answer for a unique rule that has
  no mapping yet (a test fails until it is mapped).
* `details.resource` — the kind of thing that collided (`workspace`, `file`, `connection`, `project`, `run`, `job`, `deployment`, ...).
* `details.field` — the attribute to change; for a composite key the names joined with `+` (`git_url+git_ref+project_path`); absent when no
  single field applies. Other `details` keys are service-specific extras (e.g. the colliding values).
* `details` is always present (an object).

How it is produced, identically in the three services (`app/core/errors.py`): `conflict(message, code=, resource=, field=, details=)` for a
deliberate conflict (state, version, duplicate found by a pre-check); `CONSTRAINT_CONFLICTS` maps every UNIQUE constraint/index of the models to
`(code, resource, field)`; `flush_or_conflict(db, message, constraint=)` turns a unique violation raised by a flush into that 409 (rolling the
transaction back; any other integrity error stays an error); and an app-wide `IntegrityError` handler gives the same 409 for a unique violation
no route translated, so a race can never surface as a raw database error or a 500.

Codes by service: Workspace `WORKSPACE_NAME_TAKEN`, `FILE_PATH_TAKEN`, `FILE_VERSION_CONFLICT`, `CONNECTION_NAME_TAKEN`, `SESSION_ENDED`,
`GIT_BINDING_NOT_READY`, `GIT_IMPORT_INCOMPLETE`, `GIT_REPOSITORY_LOCKED`, ...; dbt Runner `PROJECT_ALREADY_REGISTERED`, `RUN_NOT_CANCELLABLE`,
`RUN_ALREADY_EXISTS`, `OCCURRENCE_ALREADY_CLAIMED`, ...; Flink Runner `JOB_NAME_TAKEN`, `JOB_NOT_DEPLOYABLE`, `JOB_NOT_RUNNING`,
`JOB_STATE_CHANGED`, `DEPLOYMENT_IN_PROGRESS`, `STOP_IN_PROGRESS`, `SAVEPOINT_ALREADY_EXISTS`, ... (full lists: each service's `CONSTRAINT_CONFLICTS` and
its `conflict(` call sites).

Enforced by: each service's `tests/test_error_contract.py` (every unique rule mapped, every `conflict()` call names `code=` and `resource=`,
codes UPPER_SNAKE, handler behaviour) and Workspace's `tests/test_error_envelope_across_services.py` (the three `conflict()` functions give the
same body for the same call, have the same signature, and no code means two things).

Not unified here: the 4xx/5xx errors other than 409 keep their existing shapes (the runners' `NOT_FOUND`/`VALIDATION_*`, FastAPI's own 422 body);
the dbt and Flink 500 bodies already carry a request id (FL-010).
