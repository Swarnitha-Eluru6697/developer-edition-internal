# Metrics policy: low cardinality, always

Prometheus keeps **one time series per distinct combination of label values**, forever, and remote-write backends bill per series. A label whose
value is different for every pipeline, topic, connector or error message turns "a few hundred series" into "one new series for everything this
deployment has ever seen" — and it never shrinks back. This policy exists so that cannot happen by accident.

## The rules

1. **Only five label names exist:** `service`, `runtime_pool`, `connector_type`, `status`, `operation`. A metric may use a subset. Nothing else —
   never a pipeline id/name, connector name, topic, org id, user, URL, host, or error text.
2. **Every label value comes from a fixed vocabulary** (`app/core/metrics.py`):

   | Label | Allowed values |
   |---|---|
   | `service` | `dataconnect` |
   | `runtime_pool` | the worker pools in `app/core/connect_pools.py`, `none`, `other` |
   | `connector_type` | `source`, `sink`, `unknown`, `other` |
   | `status` | `running`, `paused`, `degraded`, `failed`, `absent`, `unknown`, `success`, `other` |
   | `operation` | `reconcile`, `kafka_produce`, `stats`, `quarantine`, `prepared`, a *registered* operation type (e.g. `pipeline_deploy`), `other` |

3. **A value outside its vocabulary is recorded as `other`** (and logged once, rate-limited). It is never a new series. So a typo, or a caller
   that passes something derived from user data, costs one visible `other` bucket — which an alert can watch — instead of unbounded growth.
4. **A series budget:** no metric may exceed `MAX_SERIES_PER_METRIC` (500) distinct label combinations. `tests/test_metrics_policy.py` fuzzes
   thousands of hostile values against it.
5. **Naming:** `dataconnect_<noun>_<unit>`; counters end in `_total`; gauges that reflect live state use `set_function` (computed at scrape time),
   so a stopped component cannot leave a stale value.
6. **Per-entity detail goes elsewhere.** "Which pipeline is stuck?" belongs in structured logs (`correlation_id`), the pipeline's own status in the
   API, or the database — not in a label. Metrics answer "is anything wrong, how many, for how long".

## Adding a metric

1. Define it in `app/core/metrics.py` with labels from the five names. Add a `record_*` helper that routes every value through `_bounded(...)`.
2. If it needs a new value in a vocabulary, add it to the set **and** to the table above — the test checks the call sites against the sets.
3. Run `pytest tests/test_metrics_policy.py`. If your metric is rejected, the answer is almost always "put that detail in a log line".
