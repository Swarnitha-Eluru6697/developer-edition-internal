# Execution durability: cancel intent and worker ownership live in Postgres (W-017, W-018)

An execution row is the source of truth for everything that decides its outcome. Redis is used for wake-ups and live
events only; losing it (restart, flush, outage) must never change what happens to a run.

## What is stored where

| Fact | Where | Written by |
|---|---|---|
| The command (code, target, limits) | `executions` row, immutable | `submit()` |
| Status (`pending` → `running` → terminal) | `executions.status`, single-winner conditional `UPDATE` | `transition()` |
| Who owns a running row and until when | `executions.lease_owner`, `lease_expires_at` | the **claim** itself, then the worker's heartbeat |
| A user asked to cancel a running run | `executions.cancel_requested_at` | `cancel()` |
| "Look at the row now" hint after a cancel | Redis key `exec-cancel:<id>` (best effort) | `cancel()` |
| Submit wake-up, live output and terminal events | Redis pub/sub | submit / worker |

## The claim (W-018)

`pending → running` is one `UPDATE` that also sets `lease_owner` (this worker: `host:pid:random`) and
`lease_expires_at` (now + `EXECUTION_LEASE_SECONDS`, default 30). A row can therefore never be `running` without a
durable owner, whatever Redis does between the claim and the first heartbeat. The `execution.started` event is best
effort: a failed publish does not strand the claimed row.

While the run is active the worker extends the lease every `EXECUTION_LEASE_SECONDS / 3`:
`UPDATE … SET lease_expires_at = now + lease WHERE id = … AND status = 'running' AND lease_owner = <me> RETURNING cancel_requested_at`.
If that matches no row, the row is no longer this worker's (the reconciler gave up on it, e.g. after a long database
outage) and the worker stops the run instead of carrying on unobserved.

## Cancel (W-017)

* Queued (`pending`): moved to `cancelled` at once (unchanged).
* Running: `cancel_requested_at` is set (conditional on the row still running, kept at the first request if repeated),
  then the API returns `cancelling`. The Redis key is only a hint that makes the worker look at the row immediately;
  without it the worker sees the request on its next heartbeat, so within about `EXECUTION_LEASE_SECONDS / 3`
  (10 s by default). If Redis is down, the call still succeeds.
* The worker stops the engine (lakehouse cancel / kernel interrupt / in-process query) and the row ends `cancelled`.

## Reconciliation (every `LIFECYCLE_RECONCILE_INTERVAL_SECONDS` or lease, whichever is shorter, on every worker)

A `running` row whose `lease_expires_at` has passed belongs to a dead worker. It ends:

* `cancelled` if `cancel_requested_at` is set (the user's cancel is honoured even though the worker is gone),
* otherwise `failed` with `INTERRUPTED`.

A running row **without** a lease (claimed by a worker from before this change) is recovered only once it has overrun its
own `timeout_seconds` + `EXECUTION_STALE_GRACE_SECONDS`. Redis is not consulted at all.

## Operating notes

* **Clock:** lease times are written and compared with each worker's clock, as `started_at` always was. Keep node
  clocks NTP-synchronised; skew of a few seconds is harmless against a 30 s lease.
* **Rolling deploy:** deploy the workers together. During a mixed rollout a *previous-version* reconciler looks for the
  Redis lease, finds none for a row claimed by a new worker, and recovers it as `INTERRUPTED` after two lease periods.
* **Redis down:** submitting, claiming, heartbeating, cancelling and recovering all work. Streaming a run's output goes
  through Redis, so a run that produces output while Redis is down ends `failed` (`INTERNAL_ERROR`) rather than being
  stranded or left running.
* Look at a run: `SELECT status, error_code, cancel_requested_at, lease_owner, lease_expires_at FROM executions WHERE id = '…';`

Tests: `tests/test_execution_state_machine.py` (the cancel/lease section). Migration `0011_execution_cancel_lease`.
