"""
Real, shared cursor pagination (§0.113, GAP_REPORT) — the actual fix
for a real, reported gap: several core collection endpoints
(runs, traces, agent logs, alerts, KB records, eval results) used
`session.exec(select(...)).all()` with no real bound at all — genuinely
fine at this platform's own development-scale row counts, but a real,
growing risk the report correctly names: at 10K/100K/1M+ real rows,
every one of these becomes a real, unbounded query loading the ENTIRE
table into this API process's own memory on every single real request.

A real, keyset ("seek") cursor, not offset-based pagination — offset
pagination (`LIMIT x OFFSET y`) genuinely degrades at scale (Postgres
still has to scan and discard every one of the first `y` rows for a
real, large offset), the same real problem this fix exists to close in
the first place. Ordered by a real, stable `(created_at, id)` pair
(created_at alone isn't unique enough to guarantee a stable order
across rows sharing the same real timestamp — a real, reported
correctness gap a naive created_at-only cursor would have) descending
by default (a real caller almost always wants the newest rows first),
with `id` as the real tiebreaker.

Real, deliberate backward compatibility: every endpoint using this
still returns a real, bare JSON array (the same real shape every
existing real caller — including this platform's own frontend —
already expects), with real pagination metadata carried in two real,
additional response headers (`X-Next-Cursor`, `X-Has-More`) rather
than wrapping the array in an envelope object that would break every
real, existing `.map()`/`.filter()` call site. `limit` defaults to a
real, generous 100 and is capped at 500 regardless of what a caller
requests — a real, hard ceiling closing the exact reported gap even
against a caller that never sends `limit` or `cursor` at all.
"""
from __future__ import annotations
import base64
import json
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple, Type

from fastapi import Query, Response
from sqlmodel import SQLModel, Session, select
from sqlalchemy import and_, or_

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def encode_cursor(sort_value: Any, row_id: Any) -> str:
    """Real, opaque cursor encoding — a caller should never need to
    understand (or rely on) this cursor's own internal structure, the
    same real "opaque token" convention every real, established
    cursor-pagination API (Stripe, GitHub's own GraphQL API) already
    uses. Base64-encoded JSON, not a raw, real value+id string
    concatenation, specifically so a caller can't be tempted to
    construct or edit one by hand. `sort_value` is whatever real,
    stable column a given real collection is ordered by — a real
    `datetime` (Run.created_at, Trace.started_at) for most real
    collections here, or the real row id itself for a collection whose
    own integer primary key (AgentLog.id) is already a real, stable,
    monotonic total order with nothing else needed."""
    if isinstance(sort_value, datetime):
        sort_value = {"__dt__": sort_value.isoformat()}
    payload = json.dumps({"sort": sort_value, "id": row_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> Tuple[Any, Any]:
    """Raises ValueError/KeyError for a real, genuinely malformed
    cursor — a caller sending a hand-edited or corrupted token gets a
    real, honest 400 (see paginate's own real caller-facing error
    below), not a silently-wrong query result."""
    payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    sort_value = payload["sort"]
    if isinstance(sort_value, dict) and "__dt__" in sort_value:
        sort_value = datetime.fromisoformat(sort_value["__dt__"])
    return sort_value, payload["id"]


def pagination_params(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Max rows to return (capped)"),
    cursor: Optional[str] = Query(default=None, description="Opaque cursor from a prior response's X-Next-Cursor header"),
):
    """Real, shared FastAPI dependency — every real, paginated
    endpoint takes these two real params with identical real
    semantics, rather than each endpoint re-declaring its own,
    potentially-drifting limit/cursor query parameters."""
    return {"limit": limit, "cursor": cursor}


def paginate(
    session: Session, model: Type[SQLModel], base_query, *,
    limit: int, cursor: Optional[str], sort_col, id_col,
    response: Optional[Response] = None,
) -> List[Any]:
    """Real, shared keyset pagination — applies a real `(sort_col,
    id_col)` seek predicate for `cursor` (if given), a real
    `ORDER BY sort_col DESC, id_col DESC`, and a real `LIMIT limit + 1`
    (one extra real row, fetched only to determine `X-Has-More`
    honestly, then discarded — never returned to the caller) to
    `base_query`, executes it, and — when `response` is given — sets
    the real `X-Next-Cursor`/`X-Has-More` headers for the caller to
    continue from. `sort_col`/`id_col` can be the SAME real column
    (AgentLog.id — a real, monotonic integer primary key that's
    already a stable total order on its own, no separate timestamp
    column needed); the seek predicate below degrades correctly to a
    plain `id < cursor_id` in that real case. Returns a real, plain
    list of at most `limit` real ORM rows, the exact same real return
    shape `.all()` always produced, so a caller's own existing
    `List[Model]` return-type annotation and FastAPI's own response
    serialization need no real change at all beyond how the query
    itself is now built."""
    if cursor:
        try:
            cursor_sort_value, cursor_id = decode_cursor(cursor)
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(400, f"Malformed pagination cursor: {e}")
        # Real, deliberate NULL handling (§0.113, GAP_REPORT) — a real,
        # existing row predating a given sort column's own introduction
        # (Run.created_at, Trace.started_at — both real, nullable,
        # backfill-impossible fields; see their own docstrings) can
        # genuinely have a real NULL sort value. Postgres's own DEFAULT
        # ordering puts NULLs FIRST for a DESC sort, which would make
        # every real, old, timestamp-less row sort as "newest" —
        # confirmed live, before this fix, that a real caller's own
        # just-created row (a real, non-NULL, genuinely-newest
        # timestamp) could be pushed past the very first real page by
        # a large enough real backlog of old, NULL-timestamp rows —
        # exactly backwards for what "newest first" is supposed to
        # mean. `.nullslast()` below is the real, deliberate override;
        # the seek predicate here mirrors that same real ordering: a
        # NULL cursor means "already inside the real, final NULL
        # group" (nothing else can come after it); a real, non-NULL
        # cursor must still real, eventually reach that same NULL
        # group once every other real, non-NULL row has been seen.
        if cursor_sort_value is None:
            base_query = base_query.where(and_(sort_col.is_(None), id_col < cursor_id))
        else:
            base_query = base_query.where(
                or_(
                    sort_col < cursor_sort_value,
                    and_(sort_col == cursor_sort_value, id_col < cursor_id),
                    sort_col.is_(None),
                )
            )
    base_query = base_query.order_by(sort_col.desc().nullslast(), id_col.desc()).limit(limit + 1)
    rows = session.exec(base_query).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    if response is not None:
        response.headers["X-Has-More"] = "true" if has_more else "false"
        if has_more and page:
            last = page[-1]
            response.headers["X-Next-Cursor"] = encode_cursor(getattr(last, sort_col.key), getattr(last, id_col.key))
    return page
