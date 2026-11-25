"""
BUG-11: app/services/safe_error.py — the classifier both background
runners (operation_runner.py, quality_job_runner.py) now go through instead
of persisting `str(exc)[:2000]` directly.
"""

import httpx
import pytest
from kafka.errors import KafkaError
from structlog.testing import capture_logs

from app.core.errors import ConnectorConfigError, KafkaConnectError
from app.core.network_policy import DestinationBlockedError
from app.services.safe_error import SafeError, manual_safe_error, to_safe_error

LEAKY_MESSAGE = "could not connect to postgres://dataconnect:hunter2@db.internal.example.com:5432/erp"


def test_timeout_error_classifies_as_source_connection_timeout() -> None:
    safe = to_safe_error(TimeoutError("timed out"), event="test.failed")
    assert safe.code == "SOURCE_CONNECTION_TIMEOUT"
    assert safe.message == "The source did not respond within the configured timeout."
    assert safe.retryable is True


def test_httpx_timeout_classifies_as_source_connection_timeout() -> None:
    safe = to_safe_error(httpx.ConnectTimeout("connect timed out"), event="test.failed")
    assert safe.code == "SOURCE_CONNECTION_TIMEOUT"


def test_destination_blocked_error_is_not_retryable() -> None:
    safe = to_safe_error(DestinationBlockedError(LEAKY_MESSAGE), event="test.failed")
    assert safe.code == "DESTINATION_BLOCKED"
    assert safe.retryable is False
    assert "hunter2" not in safe.message
    assert "db.internal.example.com" not in safe.message


def test_connection_error_classifies_as_source_connection_refused() -> None:
    safe = to_safe_error(ConnectionRefusedError(LEAKY_MESSAGE), event="test.failed")
    assert safe.code == "SOURCE_CONNECTION_REFUSED"
    assert safe.retryable is True
    assert "hunter2" not in safe.message


def test_httpx_http_error_classifies_as_source_http_error() -> None:
    safe = to_safe_error(httpx.HTTPError("500 Internal Server Error"), event="test.failed")
    assert safe.code == "SOURCE_HTTP_ERROR"
    assert safe.retryable is True


def test_kafka_error_classifies_as_kafka_unavailable() -> None:
    safe = to_safe_error(KafkaError("bootstrap.servers=kafka-1.internal:9092 unreachable"), event="test.failed")
    assert safe.code == "KAFKA_UNAVAILABLE"
    assert safe.retryable is True
    assert "kafka-1.internal" not in safe.message


def test_module_not_found_error_is_not_retryable() -> None:
    safe = to_safe_error(ModuleNotFoundError("No module named 'asyncpg'"), event="test.failed")
    assert safe.code == "DRIVER_NOT_INSTALLED"
    assert safe.retryable is False


def test_unclassified_exception_falls_back_to_internal_error_and_is_not_retryable() -> None:
    """A programming bug (KeyError, AttributeError, ...) must never be
    treated as retryable by default — retrying a bug in a loop is worse
    than surfacing it once."""
    safe = to_safe_error(KeyError("connection_config"), event="test.failed")
    assert safe.code == "OPERATION_FAILED"
    assert safe.retryable is False
    assert "connection_config" not in safe.message


def test_datacconnect_error_reuses_its_own_code_and_retryable() -> None:
    exc = KafkaConnectError("Kafka Connect error (502): unavailable")
    safe = to_safe_error(exc, event="test.failed")
    assert safe.code == "KAFKA_CONNECT_UNAVAILABLE"
    assert safe.retryable is exc.retryable is True


def test_datacconnect_error_message_is_still_credential_scrubbed_as_defense_in_depth() -> None:
    exc = ConnectorConfigError(f"invalid config: {LEAKY_MESSAGE}")
    safe = to_safe_error(exc, event="test.failed")
    assert "hunter2" not in safe.message
    assert safe.retryable is False


def test_message_never_contains_anything_from_the_original_exception_text() -> None:
    """The generic buckets' messages are fixed sentences — never
    interpolated from the exception at all, regardless of type."""
    for exc in (TimeoutError(LEAKY_MESSAGE), ConnectionError(LEAKY_MESSAGE), KafkaError(LEAKY_MESSAGE)):
        safe = to_safe_error(exc, event="test.failed")
        assert "hunter2" not in safe.message
        assert "db.internal" not in safe.message


def test_each_call_gets_a_fresh_correlation_id() -> None:
    a = to_safe_error(TimeoutError(), event="test.failed")
    b = to_safe_error(TimeoutError(), event="test.failed")
    assert a.correlation_id != b.correlation_id


def test_diagnostic_log_carries_the_real_scrubbed_detail_tagged_with_the_correlation_id() -> None:
    """The safe, generic message is what's persisted; the (credential-
    scrubbed but otherwise detailed) real exception goes to structlog under
    the same correlation_id, so an operator can cross-reference one to the
    other without the detail ever reaching a caller."""
    with capture_logs() as logs:
        safe = to_safe_error(ConnectionError(LEAKY_MESSAGE), event="my.event", job_id="job-1")

    matching = [entry for entry in logs if entry.get("event") == "my.event"]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["correlation_id"] == safe.correlation_id
    assert entry["job_id"] == "job-1"
    assert "hunter2" not in entry["diagnostic"]
    assert "db.internal.example.com" in entry["diagnostic"]  # host itself is fine to keep for debugging


def test_diagnostic_is_truncated_past_the_max_length() -> None:
    huge = "x" * 5000
    with capture_logs() as logs:
        to_safe_error(RuntimeError(huge), event="my.event")
    assert len(logs[0]["diagnostic"]) <= 2001  # cap + ellipsis


def test_manual_safe_error_has_the_same_shape_as_a_classified_one() -> None:
    with capture_logs() as logs:
        safe = manual_safe_error(
            "ATTEMPTS_EXHAUSTED", "Gave up after 5 attempts.", retryable=False, event="op.exhausted", op_id="op-1",
        )
    assert isinstance(safe, SafeError)
    assert safe.code == "ATTEMPTS_EXHAUSTED"
    assert safe.retryable is False
    assert logs[0]["correlation_id"] == safe.correlation_id


@pytest.mark.asyncio
async def test_to_safe_error_works_from_an_async_context_too() -> None:
    # Sanity check only — to_safe_error/manual_safe_error are plain sync
    # functions, called from async runner code; nothing here should require
    # an event loop, but confirm it's harmless either way.
    safe = to_safe_error(TimeoutError(), event="test.failed")
    assert safe.code == "SOURCE_CONNECTION_TIMEOUT"
