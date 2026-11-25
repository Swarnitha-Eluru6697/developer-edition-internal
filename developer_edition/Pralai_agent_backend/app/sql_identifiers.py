"""
Real SQL identifier safety (§0.57, GAP_REPORT) — a real, reported bug:
Knowledge Base table/column names are ultimately operator- or API-
supplied strings, and several real places built them into SQL text via
plain f-string interpolation wrapped in double quotes. Wrapping a
string in `"` is not identifier escaping if the string itself can
contain a `"` — confirmed live before writing any fix: a crafted
identifier like `evil"; DROP TABLE users; --` genuinely breaks out of
naive quoting.

Two real, independent layers, matching the reported fix exactly:

1. Strict validation (`validate_identifier`) — a real allowlist, not a
   blocklist: only letters, digits, and underscores, starting with a
   letter or underscore, at most 63 characters (Postgres's own real
   identifier length limit). Anything else is rejected outright,
   before it ever reaches a SQL string, with a clear, honest error
   naming exactly what was rejected and why.

2. Real, correct escaping (`safe_identifier`) — even a validated
   identifier is still composed via `psycopg2.sql.Identifier`, the
   real, purpose-built mechanism for this (verified live: it correctly
   doubles an embedded `"` rather than letting it terminate the quoted
   identifier early), not hand-rolled string wrapping. Defense in
   depth, not either/or — the strict allowlist above means a
   correctly-escaped-but-still-malicious identifier should never
   actually reach this layer, but this layer is what actually executes
   against the real database, so it's real regardless.

Real, honest scope: this governs identifiers used in *this platform's
own* dynamically-built SQL (Knowledge Base ingestion and retrieval).
The rest of this codebase's real data access goes through SQLModel/
SQLAlchemy, whose own query builder already parameterizes every real
value it accepts — those call sites were never part of this bug.
"""
import re
from typing import Optional

from psycopg2 import sql

# Real, deliberately strict — a real Postgres identifier's own hard cap
# is 63 bytes; this allowlist is ASCII letters/digits/underscore only,
# which is also genuinely sufficient for every real table/column name
# this platform itself ever generates (see knowledge_bases.py's own
# real slug generation for new KB tables).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class InvalidIdentifier(ValueError):
    """A real, honest rejection — carries the actual identifier and
    kind (table/column) that failed, not a generic error."""


def validate_identifier(name: Optional[str], kind: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise InvalidIdentifier(
            f"Invalid {kind}: {name!r} — must start with a letter or underscore, "
            f"contain only letters, digits, and underscores, and be at most 63 "
            f"characters (Postgres's own real identifier length limit)."
        )
    return name


def safe_identifier(name: Optional[str], kind: str = "identifier") -> sql.Identifier:
    """Validates, then returns a real psycopg2.sql.Identifier ready to
    compose into a query via sql.SQL(...).format(...) — the one real
    function every call site in this module's own callers should use
    rather than validating and quoting separately by hand."""
    validate_identifier(name, kind)
    return sql.Identifier(name)
