# Per-connector Kafka Connect metrics (Monitoring page)

The Monitoring page's **Connector metrics** panel shows Prometheus-sourced metrics for one connector at a time:
pick a connector (dropdown, or click a pipeline in the table / a name in *Connector status*) and a range (15m · 1h · 6h · 24h).

## Data path

```
Connect worker JVM ──JMX──▶ JMX exporter javaagent (:9404/metrics) ◀──scrape── Prometheus ◀──PromQL── DataConnect backend ◀── UI
```

| Piece | Where |
|---|---|
| Exporter jar + `KAFKA_OPTS` javaagent (every pool image) | `infra/kafka-connect.Dockerfile` |
| Exporter rules (`connector` / `task` labels) | `infra/jmx-exporter/kafka-connect.yml` |
| Scrape config (shared worker + six pools, `pool` label) | `infra/prometheus/prometheus.yml`, `prometheus` service in compose (host :9099) |
| Backend setting | `DATACONNECT_PROMETHEUS_URL` (empty = feature off, UI says "not configured") |
| Query service | `app/services/connector_prometheus_metrics.py` |
| API | `GET /api/v1/dataconnect/runtime/connectors/{connector_name}/metrics?range=15m\|1h\|6h\|24h` |
| Bulk API | `GET /api/v1/dataconnect/runtime/connectors/metrics/summary` — the current rate, lag and task counts of every connector the org owns, from one Prometheus query batch (5 grouped queries, however many connectors) plus the sinks' backlog from Kafka. Feeds the Dashboard and the Monitoring connector list. Same honesty rules: `prometheus_enabled: false` / `reachable: false` + `error` leave every value `null`, never 0. |
| UI | `src/components/ConnectorMetricsPanel.tsx` (frontend repo) |

## Metrics

Source connectors: records polled/s, records written to Kafka/s, poll batch time.
Sink connectors: records read from Kafka/s, records written to sink/s, consumer lag, put batch time, records sent to DLQ.
Both: offset commit time, running ratio, tasks running, record errors/s, failed records, offset commit failure %.
Plus a per-task table (status from JMX, worker pool, per-task rate/ratio/failures).

### Where three of them come from (changed after the monitoring test run)

* **Consumer lag** is the sink's real backlog: the end offset of its topics minus its consumer group's committed offset, summed over
  their partitions, read **live from Kafka** (`runtime_metrics.sink_backlogs`, cached 5 s) — the same numbers the Pipelines page shows.
  Connect's JMX `records-lag-max` was used before; it is measured at fetch time, so it read 0 whenever the consumer had fetched
  everything (even while those records were still being written) and it stopped changing while the connector was paused. The test run
  saw 250 records waiting while the tile said 0. The value has **no history** (`history: false` on that metric, an empty series), so
  its tile says "Live from Kafka offsets · no history" instead of a chart, and it is still shown while the sink is paused. With one
  task the task row shows it too; with several, the per-task split is not known from offsets, so that column stays empty.
* **Record errors** is the rate of Connect's `total-record-failures`. Its `total-record-errors` counter does not count records a
  sink reports as failed during put, so a sink's failed writes never showed (21 in the test run, tile at 0/s throughout).
* **Ranges wider than a scrape** (1h, 6h, 24h — steps of 60 s, 5 min, 15 min) summarise each bucket instead of sampling one
  instant: `avg_over_time` for rates and ratios, `max_over_time` for latencies, lag, failure % and cumulative counts,
  `min_over_time` for tasks running. Before, a one-minute bulk read fell between 5- and 15-minute points and the 6h/24h charts read 0
  for 1150 records. On those ranges the tile's headline value comes from an instant query, so it is still the latest reading
  (`settings.prometheus_scrape_interval_seconds`, default 15, must match the scrape interval in `infra/prometheus/prometheus.yml`).

## Behaviour

- **Org-scoped**: only connectors of the caller's own pipelines; anything else 404s and is never sent to Prometheus.
- **Honest values**: Prometheus unconfigured → `prometheus_enabled:false`; unreachable / query error → `reachable:false`
  with `error`; no samples → `current: null`. The UI renders "—", never 0.
- Results cached per (connector, range) for `DATACONNECT_PROMETHEUS_METRICS_CACHE_TTL_SECONDS` (10s); the UI polls every 15s.
- The exporter rules and `QUERIED_SERIES` are one contract — `tests/test_connector_prometheus_metrics.py` fails if they drift.

## Production notes

- Point `DATACONNECT_PROMETHEUS_URL` at whatever Prometheus (or Prometheus-compatible API: Thanos, Mimir, VictoriaMetrics)
  scrapes the Connect workers' `:9404`. In Kubernetes, scrape via a ServiceMonitor/PodMonitor on port 9404.
- `KAFKA_OPTS` is inherited by `docker exec`: run Kafka CLI tools in a worker container as `KAFKA_OPTS= kafka-topics …`,
  otherwise the second JVM can't bind :9404.
- Existing worker images must be **rebuilt** (`docker compose up -d --build`) to pick up the exporter.

## Where it shows in the UI

* **Dashboard** — running / failed connectors (state from Kafka Connect), records read and written per second, per-pipeline read/written and sink lag (Prometheus, via the bulk call), the worker pools, and the validation / ingestion failure counts. A value a source couldn't give is a dash, never 0.
* **Monitoring** — a searchable connector list (state badge and current rate) beside the selected connector's metrics: double-width throughput and lag tiles, click a tile for a large chart with an axis and exact value on hover, the per-task table with *Restart task* for a failed task, the real Kafka Connect failure trace, and *Show API response*.
* **Pipelines** — each row shows its source, connector class and topic, *Deploy* for a draft and *View metrics* (opens Monitoring on that connector) once deployed.
* **Live / Pause / Refresh** in the page header controls polling for all three pages. Pause stops polling; Refresh always works.
* **Demo controls** (top bar) — preview how the pages look when Prometheus is not configured, unreachable, or the DataConnect API is failing, and follow the selected connector's data path (worker → JMX exporter → Prometheus → API → page). It works on real data only: a preview hides real values behind the same messages the app shows when that really happens; it never invents a number.
