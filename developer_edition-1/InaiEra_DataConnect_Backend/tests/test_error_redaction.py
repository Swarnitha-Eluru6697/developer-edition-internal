"""
BUG-11: background failures must not persist or return raw exception text, and the detailed diagnostic log must be
redacted. A driver's exception text can embed a user name, host, URL, SQL, file path or request payload.

Covers: the redaction functions; the SafeError classification and the shape it stores; every place a background
failure used to be stored raw (reconciler runtime error, discovery run, schema-introspection status, skipped
quality results); and that the log entry is redacted.
"""

import errno
import re
import socket
import ssl
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import app.services.connection_discovery as connection_discovery
import app.services.discovery_service as discovery_service_module
import app.services.runtime_reconciler as reconciler_module
from app.core.errors import KafkaConnectConfigError, KafkaConnectError, TopicProvisioningError
from app.core.logging import redact_error_text
from app.core.redaction import redact_diagnostic, redact_secrets
from app.services.discovery_service import discovery_service
from app.services.runtime_reconciler import RuntimeReconciler
from app.services.safe_error import _classify, to_safe_error
from app.services.secrets import sanitize_probe_message

# ── redaction ──────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("connect failed password=hunter2 host=db.internal", ["hunter2"]),
        ('{"password": "hunter2", "note": "ok"}', ["hunter2"]),
        ("jdbc:postgresql://db.internal:5432/erp?user=app&password=hunter2", ["hunter2"]),
        ("postgresql://app:hunter2@db.internal/erp", ["hunter2"]),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", ["eyJhbGciOiJIUzI1NiJ9"]),
        ("api_key=sk_live_abcdef123456 rejected", ["sk_live_abcdef123456"]),
        ("secret_access_key: wJalrXUtnFEMI", ["wJalrXUtnFEMI"]),
        ("client_secret=abc123XYZ", ["abc123XYZ"]),
    ],
)
def test_credentials_are_masked_even_in_the_user_facing_level(text, forbidden) -> None:
    redacted = redact_secrets(text)

    for secret in forbidden:
        assert secret not in redacted


def test_the_user_facing_level_keeps_the_host_so_a_probe_message_stays_useful() -> None:
    redacted = sanitize_probe_message("connect to db.internal:5432 failed: password=hunter2")

    assert "db.internal:5432" in redacted
    assert "hunter2" not in redacted


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ('FATAL: password authentication failed for user "dataconnect"', ["dataconnect"]),
        ("login with user=app failed", ["app failed"]),
        ("SELECT * FROM salary WHERE ssn='123-45-6789' failed", ["123-45-6789"]),
        ("DELETE FROM t WHERE name = 'Alice' AND id = 42", ["Alice", "42"]),
        ("No such file /var/data/exports/q3-payroll.csv", ["/var/data/exports"]),
        ("cannot read C:\\finance\\exports\\q3.csv", ["finance"]),
        ("notify admin@corp.example", ["admin@corp.example"]),
    ],
)
def test_the_diagnostic_level_also_masks_users_sql_values_paths_and_emails(text, forbidden) -> None:
    redacted = redact_diagnostic(text)

    for sensitive in forbidden:
        assert sensitive not in redacted


def test_the_diagnostic_level_leaves_an_ordinary_message_readable() -> None:
    text = 'relation "orders" does not exist while calling http://kafka-connect:8083/connectors'

    assert redact_diagnostic(text) == text


def test_a_very_long_message_is_capped() -> None:
    assert len(redact_diagnostic("x" * 10_000)) <= 2001


def test_the_log_processor_redacts_error_text_keys_whichever_call_site_logged_them() -> None:
    event = {"event": "kafka.produce_failed", "error": "auth failed password=hunter2", "topic": "orders"}

    redacted = redact_error_text(None, "error", dict(event))

    assert "hunter2" not in redacted["error"]
    assert redacted["topic"] == "orders"  # other fields untouched


def test_the_log_processor_redacts_a_formatted_traceback() -> None:
    event = {"event": "x", "exception": 'Traceback ...\nConnectionError: dsn=postgresql://app:hunter2@db/x password=hunter2'}

    assert "hunter2" not in redact_error_text(None, "error", event)["exception"]


# ── classification ─────────────────────────────────────────────────────────────────────────────────────────────────


class InvalidPasswordError(Exception):  # the name asyncpg uses
    pass


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (TimeoutError(), "SOURCE_CONNECTION_TIMEOUT", True),
        (InvalidPasswordError('password authentication failed for user "app"'), "SOURCE_AUTH_FAILED", False),
        (Exception("Access denied for user 'root'@'10.0.0.9'"), "SOURCE_AUTH_FAILED", False),
        (ssl.SSLError(1, "CERTIFICATE_VERIFY_FAILED"), "SOURCE_TLS_ERROR", False),
        (socket.gaierror(-2, "Name or service not known"), "SOURCE_HOST_NOT_FOUND", False),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), "SOURCE_CONNECTION_REFUSED", True),
        (ConnectionRefusedError("[Errno 111] Connect call failed ('10.0.0.5', 5432)"), "SOURCE_CONNECTION_REFUSED", True),
        (FileNotFoundError("/var/data/x.csv"), "SOURCE_NOT_FOUND", False),
        (ModuleNotFoundError("No module named 'oracledb'"), "DRIVER_NOT_INSTALLED", False),
        (KafkaConnectError("Kafka Connect error (500): host kafka-connect:8083"), "KAFKA_CONNECT_UNAVAILABLE", True),
        (TopicProvisioningError("Could not provision topic 'x': NoBrokersAvailable 10.0.0.5:9092"), "TOPIC_CREATE_FAILED", True),
        (KeyError("boom"), "OPERATION_FAILED", False),
    ],
)
def test_each_failure_family_gets_its_own_code_and_retry_advice(exc, code, retryable) -> None:
    got_code, _, got_retryable = _classify(exc)

    assert (got_code, got_retryable) == (code, retryable)


def test_a_timeout_says_how_long_the_caller_waited() -> None:
    _, message, _ = _classify(TimeoutError(), timeout_seconds=10)

    assert message == "The source did not respond within 10 seconds."


def test_a_network_failure_while_talking_to_kafka_connect_is_not_blamed_on_the_source() -> None:
    code, message, _ = _classify(ConnectionError("refused"), target="kafka_connect")

    assert code == "KAFKA_CONNECT_UNAVAILABLE"
    assert "source" not in message


def test_a_rejected_connector_config_keeps_the_setting_names_but_never_the_values() -> None:
    exc = KafkaConnectConfigError(
        "Kafka Connect error (400): Connector configuration is invalid and contains the following 2 error(s):\n"
        "Invalid value jdbc:postgresql://db.internal:5432/erp?user=app&password=hunter2 for configuration "
        "connection.url: could not connect\nMissing required configuration \"topics\" which has no default value."
    )

    _, message, _ = _classify(exc)

    assert "connection.url" in message
    assert "topics" in message
    for leaked in ("hunter2", "db.internal", "jdbc:", "5432", "app"):
        assert leaked not in message.replace("Kafka Connect rejected the connector configuration", "")


NASTY = [
    ConnectionError("connection to server at \"10.0.0.5\", port 5432 failed: password=hunter2"),
    InvalidPasswordError('password authentication failed for user "dataconnect" host=db.internal'),
    RuntimeError("SELECT * FROM salary WHERE ssn='123-45-6789' failed at /srv/app/queries/pay.sql"),
    KafkaConnectError("Kafka Connect error (500): http://kafka-connect:8083/connectors/orders-7 refused password=hunter2"),
    TimeoutError("read timed out contacting https://api.partner.example/v1/orders?token=abc123SECRET"),
]


@pytest.mark.parametrize("exc", NASTY)
def test_what_is_stored_never_contains_the_exceptions_own_text(exc) -> None:
    safe = to_safe_error(exc, event="test.failure")

    stored = safe.describe()
    for sensitive in ("hunter2", "10.0.0.5", "db.internal", "dataconnect", "123-45-6789", "/srv/app", "abc123SECRET",
                      "kafka-connect:8083", "partner.example", "salary"):
        assert sensitive not in stored
    assert safe.code in stored
    assert safe.correlation_id in stored


@pytest.mark.parametrize("exc", NASTY)
def test_the_detailed_log_entry_is_redacted_and_carries_the_same_reference(exc) -> None:
    with capture_logs() as logs:
        safe = to_safe_error(exc, event="test.failure")

    entry = next(e for e in logs if e["event"] == "test.failure")
    assert entry["correlation_id"] == safe.correlation_id
    assert entry["diagnostic"]  # detail is kept...
    for secret in ("hunter2", "123-45-6789", "abc123SECRET", "/srv/app", '"dataconnect"'):
        assert secret not in entry["diagnostic"]  # ...but redacted


# ── the places that used to store raw text ─────────────────────────────────────────────────────────────────────────


def test_a_persistent_reconciler_error_is_logged_once_and_keeps_its_reference() -> None:
    reconciler = RuntimeReconciler(client_factory=lambda: None, worker_id="w")
    exc = ConnectionError("Connect refused at kafka-connect:8083")

    with capture_logs() as logs:
        first = reconciler._safe_error_text("pipe-1", exc)
        second = reconciler._safe_error_text("pipe-1", ConnectionError("Connect refused at kafka-connect:8083"))

    assert first == second
    assert len([e for e in logs if e["event"] == "reconciler.runtime_error"]) == 1
    assert "kafka-connect:8083" not in first


def test_a_different_reconciler_error_gets_a_new_reference_and_log_entry() -> None:
    reconciler = RuntimeReconciler(client_factory=lambda: None, worker_id="w")

    with capture_logs() as logs:
        first = reconciler._safe_error_text("pipe-1", ConnectionError("refused"))
        second = reconciler._safe_error_text("pipe-1", KafkaConnectError("Kafka Connect error (500): x"))

    assert first != second
    assert len([e for e in logs if e["event"] == "reconciler.runtime_error"]) == 2


@pytest.mark.asyncio
async def test_the_reconcilers_stored_runtime_error_is_the_safe_text(monkeypatch) -> None:
    from tests.test_runtime_reconciler_convergence import (
        _FakeRepo,
        _FakeSession,
        _install_fakes,
        _row,
    )

    _install_fakes(monkeypatch, status_sequence=["RUNNING"])

    async def _status_fails(client, connector_name):
        raise KafkaConnectError("Kafka Connect error (500): jdbc:postgresql://db.internal/erp?password=hunter2")

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _status_fails)
    row = _row(desired_state="running", desired_generation=1)
    reconciler = RuntimeReconciler(worker_id="w")

    await reconciler._reconcile_row(
        _session := _FakeSession(1), _FakeRepo(_session), client=None, existing={"conn-1"}, row=row,
    )

    assert "KAFKA_CONNECT_UNAVAILABLE" in row.last_runtime_error
    assert "hunter2" not in row.last_runtime_error
    assert "db.internal" not in row.last_runtime_error


@pytest.mark.asyncio
async def test_a_pipeline_that_cannot_be_built_keeps_its_wording_but_loses_secrets(monkeypatch) -> None:
    from tests.test_runtime_reconciler_convergence import (
        _FakeRepo,
        _FakeSession,
        _install_fakes,
        _row,
    )

    _install_fakes(monkeypatch, status_sequence=["RUNNING"])

    async def _cannot_build(pipeline):
        raise ValueError("Connection 'erp' has no hostname (password=hunter2 was supplied)")

    monkeypatch.setattr(reconciler_module, "_resolve_config", _cannot_build)
    row = _row(desired_state="running", desired_generation=1)

    await RuntimeReconciler(worker_id="w")._reconcile_row(
        _session := _FakeSession(1), _FakeRepo(_session), client=None, existing={"conn-1"}, row=row,
    )

    assert "has no hostname" in row.last_runtime_error  # authored by the builder: actionable, so kept
    assert "hunter2" not in row.last_runtime_error


@pytest.mark.asyncio
async def test_a_failed_discovery_run_stores_the_safe_text_not_the_exception(monkeypatch, fake_postgres) -> None:
    run = await discovery_service.create_run("org-1", "conn-1")

    async def _discover(connection, timeout=8.0, *, org_id=None):
        return {"connected": True, "message": "ok", "schemas": [], "objects": [],
                "tables": [{"schema": "public", "name": "t"}]}

    class _FailingObjects:
        def __init__(self, session):
            pass

        async def delete_for_runs(self, run_ids):
            return None

        async def bulk_create(self, rows):
            raise RuntimeError("could not write to postgresql://app:hunter2@db.internal:5432/dc password=hunter2")

    monkeypatch.setattr(discovery_service_module, "discover", _discover)
    monkeypatch.setattr(discovery_service_module, "DiscoveryObjectRepository", _FailingObjects)

    with pytest.raises(RuntimeError):
        await discovery_service.run_discovery(
            "org-1", run["id"], {"id": "conn-1", "org_id": "org-1", "source_type": "postgres", "connection_config": {}},
        )

    failed = await discovery_service.get_run_or_404("org-1", run["id"])
    assert failed["status"] == "failed"
    assert "OPERATION_FAILED" in failed["error_message"]
    assert "hunter2" not in failed["error_message"]
    assert "db.internal" not in failed["error_message"]


_LEAKY = Exception(
    "connection to server at \"10.0.0.5\", port 5432 failed: FATAL: password authentication failed for user "
    "\"app\" (password=hunter2)"
)


def _driver_raises(monkeypatch, table: str, error: Exception) -> None:
    async def _no_policy(cfg, org_id, purpose):
        return None

    async def _driver(*args, **kwargs):
        raise error

    monkeypatch.setattr(connection_discovery, "_guard_destination", _no_policy)
    monkeypatch.setitem(getattr(connection_discovery, table), "postgres", _driver)


_CONNECTION = {"source_type": "postgres", "connection_config": {"hostname": "db.example.test"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "call"),
    [
        ("_DB", lambda: connection_discovery.discover(_CONNECTION)),
        ("_COL", lambda: connection_discovery.introspect_columns(_CONNECTION, "orders")),
        ("_ROWS", lambda: connection_discovery.sample_rows(_CONNECTION, "orders")),
    ],
)
async def test_a_failed_probe_reports_a_safe_message_and_never_the_drivers_text(monkeypatch, table, call) -> None:
    _driver_raises(monkeypatch, table, _LEAKY)

    result = await call()

    assert result["connected"] in (False, None)
    message = result["message"]
    assert "SOURCE_AUTH_FAILED" in message
    for sensitive in ("hunter2", "10.0.0.5", '"app"', "5432"):
        assert sensitive not in message


# ── regression guard ───────────────────────────────────────────────────────────────────────────────────────────────

_RAW_ASSIGNMENT = re.compile(
    r"^\s*(error|error_message|last_runtime_error|message)\s*=\s*(error or )?str\((exc|e)\)", re.MULTILINE,
)  # an assignment, not a log keyword argument (logs are redacted by app/core/logging.py)


@pytest.mark.parametrize("module", [
    "app/services/runtime_reconciler.py", "app/services/discovery_service.py", "app/services/operation_runner.py",
    "app/services/quality_job_runner.py",
])
def test_no_background_module_stores_raw_exception_text(module) -> None:
    source = (Path(__file__).resolve().parent.parent / module).read_text()

    assert not _RAW_ASSIGNMENT.search(source), f"{module} assigns raw str(exc) to a stored error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "call"),
    [
        ("_DB", lambda: connection_discovery.discover(_CONNECTION)),
        ("_COL", lambda: connection_discovery.introspect_columns(_CONNECTION, "orders")),
        ("_ROWS", lambda: connection_discovery.sample_rows(_CONNECTION, "orders")),
    ],
)
async def test_an_unresolvable_host_is_a_failure_with_no_host_name_in_the_result(monkeypatch, table, call) -> None:
    """network_policy reports a DNS failure as a DestinationBlockedError; it is a mistyped host, not a policy decision."""
    from app.core.network_policy import DestinationBlockedError

    async def _cannot_resolve(cfg, org_id, purpose):
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as cause:
            raise DestinationBlockedError("Could not resolve destination 'erp-db.corp.example': [Errno -2]") from cause

    monkeypatch.setattr(connection_discovery, "_guard_destination", _cannot_resolve)

    result = await call()

    assert "SOURCE_HOST_NOT_FOUND" in result["message"]
    assert "erp-db.corp.example" not in result["message"]
    if table == "_COL":
        assert result["failed"] is True  # recorded as a failure, not a skip


def test_a_destination_blocked_by_policy_is_still_a_skip_with_its_explanation() -> None:
    from app.core.network_policy import DestinationBlockedError

    assert connection_discovery._is_policy_block(DestinationBlockedError("Destination '127.0.0.1' does not permit loopback"))
