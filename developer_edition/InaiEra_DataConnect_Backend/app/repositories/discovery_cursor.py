"""
Cursor for paging through a discovery run's objects (GAP-10).

Objects are listed in a stable, human-readable order — schema, then name, then id as the tiebreaker — so a browser lists
tables the way a user scans them, and paging visits every matching row exactly once. The cursor is an opaque token for
the last row returned; the database compares the same three-part key, so a page never depends on an OFFSET scan.

`schema_name` is NULL for schema rows and an empty string for sources with no schema notion; both sort as '' (the
database side uses COALESCE(schema_name, '') for exactly this reason).
"""

import base64
import json
from typing import Any
from uuid import UUID


class InvalidCursorError(ValueError):
    """The token isn't one this service issued."""


def sort_key(row: Any) -> tuple[str, str, str]:
    return (getattr(row, "schema_name", None) or "", row.name, row.id)


def encode_cursor(row: Any) -> str:
    schema, name, row_id = sort_key(row)
    raw = json.dumps({"s": schema, "n": name, "i": row_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> tuple[str, str, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        UUID(str(payload["i"]))  # the key's last part is a UUID column; a forged non-UUID must not reach the query
        return str(payload["s"]), str(payload["n"]), str(payload["i"])
    except (ValueError, KeyError, TypeError) as exc:  # bad base64, bad JSON, missing/foreign keys
        raise InvalidCursorError("Invalid cursor.") from exc
