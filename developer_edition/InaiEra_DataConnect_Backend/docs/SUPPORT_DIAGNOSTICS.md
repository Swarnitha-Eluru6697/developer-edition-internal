# Support diagnostics bundle

One JSON document an operator can attach to a ticket: enough to understand a deployment, safe to send. Built by `app/services/diagnostics.py`.

```bash
# the running service, for YOUR organization (Admin only):
curl .../api/v1/dataconnect/diagnostics/bundle -o diagnostics.json   # through Envoy, as a user holding dataconnect:settings:edit

# the whole deployment, aggregated, from a shell — works even when the API is down:
python -m app.diagnostics > diagnostics.json
```

## What is in it

| Section | Contents |
|---|---|
| `service` | version, environment, Python/OS, uptime, scope (an organization, or the whole deployment) |
| `configuration` | every setting; a secret-shaped one is only `<set>` / `<unset>`; URLs keep their host but lose `user:password@` |
| `readiness` | Postgres, secret store, Kafka Connect pools — ok/not ok and the error **class** |
| `database` | migration `current` vs `head` (`up_to_date`), counts by status (pipelines, operations, quality jobs) |
| `workers` | which workers are alive, quality-stream stall (blocked partitions, oldest uncommitted age) |
| `metrics` | the `dataconnect_*` series values (low-cardinality by policy: `docs/METRICS_POLICY.md`) |
| `recent_failures` | the last failed/stale operations and quality jobs: id, type, **error code**, retryable, **correlation id** |
| `dependencies` | versions of the packages support asks about |

A **correlation id** is the link to the real cause: grep the (redacted) service log for it.

## What is never in it

* Secrets — and not just "masked": configuration shows only whether one is set. Every string in the finished bundle also goes through a final
  deep-redaction pass, so a collector with a bug still cannot put a credential in.
* Customer content — connection or connector configs, pipeline/connection/organization **names**, record data, free-text error messages.
* Other tenants — the endpoint is scoped to the caller's organization; only process-level facts (versions, worker health) are shared.

## Behaviour

* **Resilient**: each section has its own deadline and its own failure: a section that breaks reports `{"error": "<ExceptionClass>"}` and is listed
  in `failed_sections`; the rest still arrive. It is built to work when parts of the system are broken.
* **Bounded**: capped at 1 MB; over that, the longest lists are trimmed and `truncated` is `true`.
* **CLI honesty**: `workers` and `metrics` describe a *running process*; from the CLI that is the CLI itself, so those sections say so instead of
  reporting "worker not running". Fetch them from the endpoint.

Tests: `tests/test_diagnostics_bundle.py` (redaction, isolation, bound, access) and `tests/test_diagnostics_real_db.py` (tenant scoping, planted
secrets, CLI — against a scratch database).
