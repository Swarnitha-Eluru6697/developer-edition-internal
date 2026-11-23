"""
The exception hierarchy behind DataConnect's structured error envelope (see
app/schemas/errors.py for the response shape, app/main.py for the handlers
that turn these into it). Every subclass fixes `code`/`status_code`/
`retryable` to one value for its whole category — a route or service raises
the specific subclass for what actually went wrong, and never has to also
know or repeat its HTTP status/error code/retryability at the call site.

Why a raised exception rather than an HTTPException with a fancier `detail`:
HTTPException's `detail` is typed as `Any` and every route already uses it
for a plain string — bolting structure onto that would mean every existing
`raise HTTPException(404, "not found")` either needs rewriting or silently
falls back to an unstructured envelope. A distinct hierarchy lets both
coexist: plain HTTPException still works (app/main.py wraps it in the same
envelope shape, inferring `code` from its status), and call sites that need
field errors, a specific error code, or extra `details` raise one of these
instead — an incremental adoption path, not a big-bang rewrite of every
`raise` in the codebase.
"""

from typing import Any

from app.schemas.errors import FieldError


class DataConnectError(Exception):
    """Base for every structured, UI-safe error this codebase raises deliberately."""

    code: str = "internal_error"
    status_code: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        field_errors: list[FieldError] | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        if retryable is not None:
            self.retryable = retryable


class ValidationError(DataConnectError):
    """Request input failed a business-rule check the request schema itself couldn't express
    (as opposed to RequestValidationError, which FastAPI raises for a request body that fails
    Pydantic's own field-level validation — both render as the same "validation_error" code)."""

    code = "validation_error"
    status_code = 422
    retryable = False


class InvalidTopicNameError(ValidationError):
    """A topic name that does not parse as the six-part name this platform builds (BUG-05) — malformed, a wildcard, a
    platform-internal topic. Same 422 as any other invalid input, with its own stable code a UI can switch on."""

    code = "INVALID_TOPIC_NAME"


class QualityJobInputMissingError(DataConnectError):
    """A quality execution job's staged input records are gone (BUG-06), so the job can be neither retried nor resumed: running it
    again cannot recreate them. NOT retryable. 410 Gone. The way forward is to submit the batch again — `details.action` says so, and
    `details.resubmit_path` is the endpoint that does it (it takes the records again)."""

    code = "QUALITY_JOB_INPUT_MISSING"
    status_code = 410
    retryable = False


class ConnectorConfigError(DataConnectError):
    """A connection's connector_config is missing a required field or has an invalid value for
    its source type — see app/services/connection_service.py's _validate_config(). 422, not 400:
    the request is well-formed, but semantically invalid against that source type's schema —
    matches the status this replaced (a plain HTTPException(422, ...)) exactly."""

    code = "connector_config_error"
    status_code = 422
    retryable = False


class SourceConnectivityError(DataConnectError):
    """The backend reached out to a customer's actual source system (a database, an HTTP
    endpoint, a bucket) and that call failed — unreachable host, refused connection, auth
    rejected by the source itself. Retryable: the source may recover on its own; nothing about
    the request itself needs to change first."""

    code = "SOURCE_CONNECTION_FAILED"
    status_code = 502
    retryable = True


class KafkaConnectError(DataConnectError):
    """A call to the Kafka Connect REST API (deploy/pause/resume/restart/delete a connector)
    failed to even complete — Connect was unreachable, timed out, or returned an error task
    state. Retryable: the worker may recover on its own. See KafkaConnectConfigError below for
    the other failure shape (Connect reached and responded, but rejected the request outright) —
    the two call for different next steps (wait and retry vs. fix the connector config first),
    so they're distinct codes even though both come from the same REST call
    (_raise_for_connect in app/services/runtime_orchestrator.py picks between them by response
    status). FE-BUG-04: this is the base class specifically so `except KafkaConnectError:`
    call sites (app/services/kafka_connect_retry.py, tests) keep catching either shape without
    change."""

    code = "KAFKA_CONNECT_UNAVAILABLE"
    status_code = 502
    retryable = True


class KafkaConnectConfigError(KafkaConnectError):
    """Kafka Connect was reached and responded, but rejected the connector configuration
    itself (400/409) — e.g. "Replication slot already exists", an invalid transform reference.
    422, not retryable: the pipeline's generated config (or something about the connector's
    existing state on the worker) needs to change first, not just be resent unchanged."""

    code = "KAFKA_CONNECT_CONFIG_INVALID"
    status_code = 422
    retryable = False


class TopicProvisioningError(DataConnectError):
    """The Kafka Admin API itself couldn't be reached or failed while
    creating/verifying a topic (see app/services/kafka_admin.py) — a
    broker-connectivity/infrastructure problem, not a bad request. 502,
    retryable: the broker may recover on its own, same reasoning as
    KafkaConnectError above."""

    code = "TOPIC_CREATE_FAILED"
    status_code = 502
    retryable = True


class TopicReplicationFactorError(DataConnectError):
    """A resolved TopicPolicy's replication_factor exceeds the number of
    brokers the live Kafka cluster actually reports (GAP-06) — caught in
    app/services/kafka_admin.py before ever issuing CreateTopics, rather
    than learned from Kafka's own opaque InvalidReplicationFactorError
    after the fact. 422, not retryable: the policy (or the cluster's
    broker count) has to change first — retrying unchanged never helps."""

    code = "TOPIC_POLICY_INVALID"
    status_code = 422
    retryable = False


class ConnectorCompatibilityError(DataConnectError):
    """A pipeline's connector template is not actually deployable against the
    live Kafka Connect worker — its connector class, an SMT, a converter, or
    a referenced ConfigProvider isn't installed/configured there, its plugin
    version isn't qualified against the worker's Kafka Connect version, or
    the pipeline's mode isn't one the template supports (GAP-05). Raised by
    app/services/deploy_preflight.py and caught before PipelineService.deploy()
    ever calls Kafka Connect's own deploy endpoint — 422, not retryable: the
    template/pipeline/worker combination itself needs to change first, not
    just be retried. FE-BUG-04: named for the dominant real-world case (a
    connector class/SMT/converter/ConfigProvider genuinely missing from the
    worker) even though two of its six preflight checks (Kafka Connect
    version, pipeline mode) are compatibility mismatches rather than a
    literal missing plugin — `details.failures[].check` always carries the
    exact per-check reason, so nothing is lost by the shared top-level code."""

    code = "KAFKA_CONNECT_PLUGIN_MISSING"
    status_code = 422
    retryable = False


class SchemaConflictError(DataConnectError):
    """A schema version was rejected because it's incompatible with the compatibility mode
    already configured for that schema (BACKWARD/FORWARD/FULL) — a real conflict the caller must
    resolve (change the schema or the mode), not something retrying unchanged fixes."""

    code = "SCHEMA_INCOMPATIBLE"
    status_code = 409
    retryable = False


class PipelineVersionConflictError(DataConnectError):
    """FE-BUG-14: a PATCH /pipelines/{id} carried an `expected_revision` that
    no longer matches the row — someone else's edit landed first. 409, not
    retryable: resending the same request unchanged would just conflict
    again: the caller has to look at what actually changed and either
    re-apply their edit on top of it or discard it, not just retry.
    `details.current` carries the pipeline's latest state so the frontend
    can render "what changed"/let the user reload without a second request."""

    code = "PIPELINE_VERSION_CONFLICT"
    status_code = 409
    retryable = False


class QualityPolicyInUseError(DataConnectError):
    """A quality policy still attached to a pipeline can't be deleted: the pipeline would be left pointing at a policy that is gone. 409,
    not retryable — detach it from those pipelines (or delete them) first. `details.pipelines` names them."""

    code = "QUALITY_POLICY_IN_USE"
    status_code = 409
    retryable = False


class QualityRuleError(DataConnectError):
    """A quality policy's rule definitions are malformed — an unknown rule type, a rule missing
    a parameter its type requires (e.g. `regex` with no pattern)."""

    code = "QUALITY_POLICY_INVALID"
    status_code = 400
    retryable = False


class BackendUnavailableError(DataConnectError):
    """A backend dependency this request needed (PostgreSQL, primarily) is unreachable — not the
    customer's own source system (that's SourceConnectivityError) or Kafka Connect (that's
    KafkaConnectError), but DataConnect's own control-plane infrastructure."""

    code = "DATABASE_UNAVAILABLE"
    status_code = 503
    retryable = True


class RequestTimeoutError(DataConnectError):
    """An outbound call this request depended on (a source probe, Kafka Connect, ...) exceeded
    its deadline. Distinct from SourceConnectivityError/KafkaConnectError so a UI can show
    "this is taking too long" rather than "unreachable" — the two call for different next steps
    (wait and retry vs. check the connection's configuration)."""

    code = "OPERATION_TIMEOUT"
    status_code = 504
    retryable = True
