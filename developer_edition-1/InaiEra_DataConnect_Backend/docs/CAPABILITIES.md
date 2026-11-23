# Capabilities: what can actually be used

Some choices can be offered but cannot be executed: a connector whose plugin is not installed, a quarantine destination reserved for a future
backend, a failure action nobody implemented. A user could not tell "this product does not do that" from "your environment is not set up for
it". Every such choice now carries a machine-readable **availability**.

## The three states

| State | Meaning | What the API does | What the UI does |
|---|---|---|---|
| `supported` | It works as offered, right now | Accepts it | Offers it normally |
| `requires_configuration` | It is implemented, but this deployment is not set up for it, **or that could not be checked** | Accepts it (choosing it is not wrong; it will not run until configured) | Offers it, **flagged**, with the message and what to do |
| `unavailable` | It cannot be executed here, and may not exist yet | **Refuses** it (see below) | **Quality options:** shows it **disabled**, with the reason. **Connectors:** stays selectable, with a heads-up (see "In the UI"). Never hidden |

Each availability also has `code` (a stable identifier to switch on; it never changes with the wording), `message` (for people), `remediation` (what
to do about it, when anything can be done) and `verified` (`false` when it could not be checked live: the state is then never `supported`, because
nobody confirmed it).

## Reason codes

| Code | State | Meaning |
|---|---|---|
| `NOT_IMPLEMENTED` | unavailable | The capability does not exist (yet): quarantine to object storage, the `drop` and `block` failure actions |
| `TEMPLATE_INACTIVE` | unavailable | The connector template has been switched off |
| `NO_CONNECTOR_CLASS` | unavailable | The catalog entry is a placeholder ("Contact Onibex for connector class"); there is nothing to install |
| `PLUGIN_NOT_INSTALLED` | unavailable | The connector class is not installed on the Kafka Connect worker |
| `PLUGIN_WRONG_KIND` | unavailable | Installed, but as a source when the template is a sink (or the reverse) |
| `PLUGIN_VERSION_INCOMPATIBLE` | unavailable | Installed, but a different major version, or older than the template was qualified for |
| `WORKER_VERSION_UNSUPPORTED` | unavailable | The Kafka Connect worker's version is not one the template was qualified for |
| `UNKNOWN_POOL` | unavailable | The template names a worker pool that does not exist |
| `RUNTIME_UNREACHABLE` | requires_configuration (`verified: false`) | Kafka Connect could not be reached, so nothing about its plugins could be confirmed |
| `KAFKA_BOOTSTRAP_MISSING` | requires_configuration | Quarantining to Kafka needs a bootstrap server and none is set (`DATACONNECT_KAFKA_BOOTSTRAP`) |
| `KAFKA_REST_PROXY_URL_MISSING` | requires_configuration | The REST-proxy producer is selected and its URL is not set (`DATACONNECT_KAFKA_REST_PROXY_URL`) |

## Connectors

`GET /connector-templates` and `GET /connector-templates/{id}` include an `availability` on every template. It is decided from what the Kafka
Connect worker that would run the template reports: its installed plugins and its version.

- **The same checks as a deploy.** The catalog and the deploy preflight both call `connector_plugin_finding` / `worker_version_finding`
  (`app/services/deploy_preflight.py`), so a connector the catalog calls `supported` is one a deploy will not reject for its plugin or version, and
  the reverse. A test asserts this for every combination.
- **Each distinct Kafka Connect cluster is asked once**, concurrently, however many templates run on it, and the answer is reused for
  `DATACONNECT_CONNECTOR_AVAILABILITY_TTL_SECONDS` (default 30). A cluster that does not answer within
  `DATACONNECT_CONNECTOR_AVAILABILITY_TIMEOUT_SECONDS` (default 5) is `RUNTIME_UNREACHABLE`: the catalog still answers, and never claims `supported`.
  After installing a plugin, a connector turns `supported` within the TTL.
- **Creating a pipeline from an unavailable template is not blocked by the API** (a draft can be prepared before the plugin is installed; the deploy
  preflight is the gate that stops a deploy). The UI does not offer it.

On a typical development stack most of the 128 catalog entries are `unavailable` (`PLUGIN_NOT_INSTALLED`); that is the point of this page.

## Quality policies

`GET /quality-policies/capabilities` returns every option a policy offers, with its availability:

- `quarantine_sinks`: `kafka` (`supported`, or `requires_configuration` when no producer is configured) and `object_storage` (`unavailable`, `NOT_IMPLEMENTED`).
- `failure_actions`: `route_to_quarantine` (exactly as available as the Kafka destination it uses), `log` (`supported`: failures are counted and logged,
  not copied anywhere), `drop` and `block` (`unavailable`, `NOT_IMPLEMENTED`: DataConnect never silently discards a failing record, and stopping a stream on a
  failure is a separate policy with different consequences, not a variant of quarantine).

Creating or updating a policy with an `unavailable` option is refused (`400 QUALITY_POLICY_INVALID`) and the matching entry in `field_errors`
carries the code:

```json
{ "code": "QUALITY_POLICY_INVALID",
  "field_errors": [{ "field": "on_failure", "message": "on_failure='block' is not available: ...", "code": "NOT_IMPLEMENTED" }] }
```

A value that is not an option at all (a typo) is `Unsupported on_failure: ...` with **no** code: unsupported and unavailable are different things.
`field_errors[].code` appears only when there is one, so existing responses are unchanged.

**Existing policies.** A policy that already holds `drop` or `block` (neither was ever implemented, so both behaved as `log`) keeps running as before, but it is
now reported honestly: `POST /quality-policies/{id}/validate` says it is invalid, it cannot be saved again until a supported action is chosen, and a pipeline
that uses it fails the **"Quality check valid"** deploy-readiness check with the explanation above, so Deploy stays disabled until the policy is changed. Nothing
else about the policy is touched.

## In the UI

**Connectors are never disabled, hidden or reordered.** The pipeline form lists every connector template alphabetically and lets you pick any of them. The same
build can be deployed to servers whose Kafka Connect has different plugins installed, so the UI does not decide what is allowed from this environment. When the
chosen connector is `unavailable` or `requires_configuration`, a heads-up under the picker shows the backend's message and what to do; Create stays enabled, and
the **deploy** preflight (the real gate) is what refuses a connector whose plugin is not installed. The form still starts on a connector the backend reports as
`supported`, when there is one.

**Quality policy options are different:** the "On failure" and "Quarantine destination" selects are built from `GET /quality-policies/capabilities`, and an option that
is not implemented (`unavailable`) is disabled and labelled ("— unavailable"), because the API refuses it. A flagged one shows the backend's message and remediation.
If the quality capabilities cannot be loaded, only the options that have always worked (Kafka, route to quarantine, log) are offered. A block that is missing or
unreadable means "no information": the choice stays selectable, because the API still refuses what it cannot run.

## Adding an option

Add it to the catalogue in `app/services/quality_capabilities.py` (or, for a connector, install its plugin; nothing else is needed). The validator, the
endpoint and the UI follow automatically. Keep the database constraint `ck_dc_quality_policies_quarantine_sink_implemented` in step with the destinations
that are not `unavailable` (a test checks it).

## Tests

`tests/test_connector_availability.py`, `tests/test_quality_capabilities.py` (backend); `tests/unit/availability.spec.ts`, `tests/e2e/availability.spec.ts`
(frontend: unavailable quality options are disabled, every connector is selectable with a heads-up, the form sends the chosen values, and a failed capability response falls back safely).
