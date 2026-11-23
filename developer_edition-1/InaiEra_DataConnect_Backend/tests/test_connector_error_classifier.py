"""FE-BUG-17: DLQ records are sorted into serialization / schema conversion /
connector errors from Connect's own error headers, never from the record body."""
import pytest

from app.services.connector_error_classifier import (
    CONNECTOR,
    EXCEPTION_CLASS_HEADER,
    EXCEPTION_MESSAGE_HEADER,
    SCHEMA_CONVERSION,
    SERIALIZATION,
    STAGE_HEADER,
    classify_connector_error,
)


def _headers(cls: str = "", message: str = "", stage: str = "") -> dict[str, str]:
    return {EXCEPTION_CLASS_HEADER: cls, EXCEPTION_MESSAGE_HEADER: message, STAGE_HEADER: stage}


@pytest.mark.parametrize(("headers", "expected"), [
    (_headers("org.apache.kafka.connect.errors.DataException",
              "Converting byte[] to Kafka Connect data failed due to serialization error of topic t", "VALUE_CONVERTER"),
     SERIALIZATION),
    (_headers("org.apache.kafka.common.errors.SerializationException", "Unknown magic byte!", "VALUE_CONVERTER"), SERIALIZATION),
    (_headers("com.fasterxml.jackson.core.JsonParseException", "Unrecognized token 'abc'", "VALUE_CONVERTER"), SERIALIZATION),
    (_headers("org.apache.kafka.connect.errors.DataException",
              "JsonConverter with schemas.enable requires \"schema\" and \"payload\" fields", "VALUE_CONVERTER"),
     SCHEMA_CONVERSION),
    # The exact message Connect emits for plain JSON sent to a schemas.enable=true converter —
    # it mentions "deserialize" in its advice but is an envelope/schema mismatch.
    (_headers("org.apache.kafka.connect.errors.DataException",
              "JsonConverter with schemas.enable requires \"schema\" and \"payload\" fields and may not contain additional fields. "
              "If you are trying to deserialize plain JSON data, set schemas.enable=false in your converter configuration.",
              "VALUE_CONVERTER"),
     SCHEMA_CONVERSION),
    (_headers("org.apache.kafka.connect.errors.SchemaProjectorException", "Schema version mismatch", "TRANSFORMATION"),
     SCHEMA_CONVERSION),
    # A converter failure with no recognizable hint is still a conversion problem.
    (_headers("org.apache.kafka.connect.errors.DataException", "boom", "KEY_CONVERTER"), SCHEMA_CONVERSION),
    (_headers("org.apache.kafka.connect.errors.ConnectException", "Tolerance exceeded in error handler", "TASK_PUT"), CONNECTOR),
    (_headers("java.sql.SQLException", "connection reset", "TASK_PUT"), CONNECTOR),
    ({}, CONNECTOR),
])
def test_classifies_from_error_headers(headers, expected) -> None:
    assert classify_connector_error(headers) == expected
