# Quality outbox: how results get to UCS (FL-004, FL-005)

Every polled quality record is first written to `quality_result_outbox` (before its Kafka offset is committed), then a
background drain delivers it to UCS. This page is about the second half.

## Delivery never holds the database (FL-004)

A drain runs in three steps:

1. **Claim** (one short transaction): `SELECT … WHERE (status = 'pending' AND next_attempt_at <= now) OR (status =
   'processing' AND lease_expires_at < now) ORDER BY created_at LIMIT <batch> FOR UPDATE SKIP LOCKED`, then set
   `status = 'processing'`, `lease_owner`, `lease_expires_at = now + QUALITY_OUTBOX_LEASE_SECONDS`, commit.
2. **Deliver** each claimed row to UCS (and run a `fail_stop`'s parent-job stop) with **no transaction or row lock
   open**. Before each later row the worker extends the lease on the rest of its batch.
3. **Finalize** each row in a second short transaction, **only if it still owns the lease** (a lapsed lease may have been
   re-claimed and that worker's outcome stands): `delivered`, `rejected`, `skipped`, or back to `pending` with a retry
   time (below).

A slow or unavailable UCS therefore costs wall-clock time, never pooled connections or locks. A worker that dies mid-delivery
leaves `processing` rows whose lease lapses; any replica claims them again and re-delivers with the **same
idempotency key** (UCS collapses the repeat). A worker that stops cleanly hands unfinished rows back untouched.
Keep `QUALITY_OUTBOX_LEASE_SECONDS` longer than one delivery (3 tries × 10 s plus back-off ≈ 40 s; the default is 300 s).

## A failing event cannot spin (FL-005)

A failed delivery (network error, 5xx, 408/429, or an unexpected error building the request) counts an attempt and is
retried after `QUALITY_OUTBOX_RETRY_BASE_SECONDS × 2^(attempts−1)` seconds, capped at `QUALITY_OUTBOX_RETRY_MAX_SECONDS`
(defaults 30 s doubling to 15 min). The time is stored in `next_attempt_at`, so a restart does not reset it, and the
claim only takes rows that are due: a poison event or an outage does not occupy a slot at the head of every flush. If UCS
is unreachable the drain also stops after the first failing row (the rest go back unpenalised) instead of hammering it.

After `QUALITY_OUTBOX_MAX_ATTEMPTS` (default 20, roughly four hours of continuous failure) the row is **parked**:
`status = 'parked'`, `last_error = 'parked after N attempts: …'`, logged at `error` (`quality_outbox.parked`). It is kept,
not retried, and not lost. A 4xx other than 408/429 is not retried at all (`rejected`).

### Finding and requeueing parked events

```sql
SELECT kind, attempts, last_error, created_at, payload->>'check_id' AS check_id
FROM quality_result_outbox WHERE status = 'parked' ORDER BY created_at;

-- after UCS (or the event) is fixed:
UPDATE quality_result_outbox
SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error = NULL
WHERE status = 'parked';            -- add AND id = '…' to requeue one
```

A parked backlog during a long UCS outage is expected to be requeued once UCS is back; alert on `parked` rows.

## Settings

| Env | Default | Helm value (`quality.`) |
|---|---|---|
| `QUALITY_OUTBOX_LEASE_SECONDS` | 300 | `outboxLeaseSeconds` |
| `QUALITY_OUTBOX_RETRY_BASE_SECONDS` | 30 | `outboxRetryBaseSeconds` |
| `QUALITY_OUTBOX_RETRY_MAX_SECONDS` | 900 | `outboxRetryMaxSeconds` |
| `QUALITY_OUTBOX_MAX_ATTEMPTS` | 20 | `outboxMaxAttempts` |

Migration `0009_quality_outbox_backoff` (additive; existing `pending` rows are due at once). Tests:
`tests/test_quality_outbox_pg.py` (needs `TEST_DATABASE_URL`), `tests/test_migrations.py`, `tests/test_helm_chart.py`.
