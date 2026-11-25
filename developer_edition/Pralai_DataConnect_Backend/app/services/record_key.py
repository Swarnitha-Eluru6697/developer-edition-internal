"""The Kafka message key for a record that a quality step routes somewhere (quarantine or prepared)."""

from typing import Any


def record_key(record: dict[str, Any]) -> str | None:
    """The Kafka message key for a record being routed (quarantine, prepared): its `id`, else its `customer_id`, as TEXT — or None when it has
    neither. Same-entity records keep landing on one partition, so they stay ordered.

    The producer's key serializer only accepts text (`key.encode`), but a record's id is very often a NUMBER (a database primary key). Passing it
    through unchanged made the produce raise, so a failing record with an integer id was never quarantined — it was dead-lettered after its
    retries, and the quarantine topic stayed empty. `0` is a valid id (the old `or` skipped it), an empty string is not, and a bool is not an id."""
    for field in ("id", "customer_id"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            continue
        if value != "":
            return str(value)
    return None
