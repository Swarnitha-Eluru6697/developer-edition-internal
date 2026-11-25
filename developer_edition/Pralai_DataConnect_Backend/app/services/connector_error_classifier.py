"""FE-BUG-17: sorts a Kafka Connect dead-letter-queue record into the kind of
ingestion failure it is, from the error headers Connect itself stamps on it
(errors.deadletterqueue.context.headers.enable=true — see
app/services/pipeline_config.py): `__connect.errors.stage`,
`__connect.errors.exception.class.name` and `__connect.errors.exception.message`.

Only the headers are looked at — never the record's key/value, which is source
data and may be personal.

Three categories, matching the Quality screen's "Ingestion failures" group:
  serialization      the bytes weren't readable as the configured format
                     (malformed JSON, unknown Avro magic byte, ...)
  schema_conversion  the bytes were readable but didn't fit the expected
                     schema/envelope, or a schema couldn't be applied
  connector          anything else — the connector, a transform or the sink
                     itself erroring on that record

This is a heuristic over Connect's free-text messages, not a contract: the
connector category is the deliberate catch-all, so an unrecognized message is
never mislabelled as one of the two specific causes.
"""

from typing import Final

SERIALIZATION: Final = "serialization"
SCHEMA_CONVERSION: Final = "schema_conversion"
CONNECTOR: Final = "connector"

CATEGORY_LABELS: Final[dict[str, str]] = {
    SERIALIZATION: "Serialization",
    SCHEMA_CONVERSION: "Schema conversion",
    CONNECTOR: "Connector errors",
}

STAGE_HEADER: Final = "__connect.errors.stage"
EXCEPTION_CLASS_HEADER: Final = "__connect.errors.exception.class.name"
EXCEPTION_MESSAGE_HEADER: Final = "__connect.errors.exception.message"

_CONVERTER_STAGES: Final = {"KEY_CONVERTER", "VALUE_CONVERTER", "HEADER_CONVERTER"}

_SERIALIZATION_HINTS: Final = (
    "serializ", "deserializ", "unknown magic byte", "unrecognized token",
    "jsonparse", "malformed", "unexpected character", "invalid utf",
    "end-of-input", "not a valid json", "failed to deserialize",
)
# Checked BEFORE the serialization hints: Connect's envelope error ("JsonConverter
# with schemas.enable requires "schema" and "payload" fields ... If you are trying
# to deserialize plain JSON data ...") contains the word "deserialize" but is a
# schema/envelope mismatch, not unreadable bytes.
_SCHEMA_STRONG_HINTS: Final = (
    "schemaprojector", "schemas.enable", "\"schema\" and \"payload\"", "schema being registered",
)
_SCHEMA_HINTS: Final = (
    "schema", "schemaprojector", "payload' fields", "payload\" fields",
    "cannot convert", "type mismatch", "incompatible", "no field", "missing required field",
)


def classify_connector_error(headers: dict[str, str]) -> str:
    exception_class = (headers.get(EXCEPTION_CLASS_HEADER) or "").lower()
    message = (headers.get(EXCEPTION_MESSAGE_HEADER) or "").lower()
    stage = (headers.get(STAGE_HEADER) or "").upper()
    text = f"{exception_class} {message}"

    if any(hint in text for hint in _SCHEMA_STRONG_HINTS):
        return SCHEMA_CONVERSION
    if any(hint in text for hint in _SERIALIZATION_HINTS):
        return SERIALIZATION
    if any(hint in text for hint in _SCHEMA_HINTS):
        return SCHEMA_CONVERSION
    # A failure inside a converter that matched no specific hint is still a
    # conversion problem, not the connector's own logic.
    if stage in _CONVERTER_STAGES:
        return SCHEMA_CONVERSION
    return CONNECTOR
