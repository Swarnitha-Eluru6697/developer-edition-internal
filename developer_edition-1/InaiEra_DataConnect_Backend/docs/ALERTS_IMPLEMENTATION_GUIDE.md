# Alerts for DataConnect: implementation guide

Ported from the Workspace implementation (built and verified on 2026-10-29). This guide tells you what to build in DataConnect, in what
order, which Workspace files to copy from, what changes for DataConnect, and which mistakes we already made and fixed so you do not repeat them.

**Status:** nothing of this exists in DataConnect yet. Everything below about Workspace is verified (tests, mutation checks, real Teams and real
Slack). Everything about DataConnect was read from this repository on 2026-10-30; where a name could not be confirmed it says **verify**.

**Reference code (read these first):** `InaiEra_workspace_backend/app/services/alerts/*`, `app/api/alerts.py`, `app/api/alert_actions.py`,
`app/schemas/alerts.py`, `migrations/versions/0015_alerts.py`, the six `Alert*` models in `app/models/__init__.py`, and
`InaiEra_workspace_frontend/src/features/alerts/*`. The full decision log is the `FEAT-ALERTS*` / `FIX-ALERT*` entries of
`InaiEra_workspace_backend/docs/WORKSPACE_BUG_LOG.md`.

---

## 1. What the user gets

A new **Alerts** page (sidebar entry, with a badge showing the number of open alerts) with five tabs:

| Tab | Shows |
|---|---|
| **Open** | Alerts that were **sent to a channel** and nobody has acknowledged yet. A note above the list counts alerts no channel has received yet ("N alerts reached no channel yet"). |
| **Acknowledged** | Someone said "I am looking at it". |
| **Resolved** | Resolved by a person, or **cleared automatically** when the failure stopped. |
| **Rules** | What raises an alert: kind, severity, threshold, window, one pipeline or all. A few default rules exist. On/off switch, edit, delete. Buttons: *Run rules now*, *New rule*. |
| **Channels** | Slack or Microsoft Teams webhooks. Card per channel: name, type, last send ok/failed, severity floor, pipeline scope, *Send test*, edit, remove. Button: *Add channel*. |

The flow:

1. A rule breaches for a pipeline, an alert is raised once (state `open`).
2. Every matching channel gets a card: heading with a severity icon, Severity / Pipeline / Kind / State / Raised, the sentence saying what
   happened, and **Acknowledge** and **Resolve** buttons.
3. A person clicks **Acknowledge** (Slack/Teams button, or the Alerts page). The alert moves to *Acknowledged*; the channels get a short
   **"acknowledged by ..." follow-up that carries a Resolve button**.
4. When it is fixed the person clicks **Resolve** (on the follow-up, on the original card, or on the page). Resolve straight from *open* also
   works. The alert moves to *Resolved*; the channels get a "resolved" follow-up (no buttons).
5. If the failure simply stops (pipeline running again), the alert is **cleared automatically** and the channels are told.

Everything that adds or edits something opens as a **right-hand drawer**, never a centred dialog (an explicit product requirement; the only
centred box is the small "are you sure?" confirm for delete).

---

## 2. What carries over unchanged, and what is DataConnect-specific

| Part | Workspace | DataConnect |
|---|---|---|
| Channels (Slack/Teams webhooks), message builders, URL checks, escaping | `services/alerts/webhooks.py` | **Copy as is**; swap the host/egress check (section 6.6). |
| Signed Acknowledge/Resolve links + confirm pages | `links.py`, `api/alert_actions.py` | **Copy as is**; the *exposure* of the route needs a platform decision (section 3, D2). |
| Rule engine (pure functions) and open/keep/clear bookkeeping | `engine.py` | **Copy the structure and `reconcile`**; the rule kinds and observation shape change. |
| Sweep, lease, delivery, retries, status follow-ups, atomic transitions | `service.py`, `sweeper.py` | **Copy**; DataConnect already has the pieces (lease pattern, lifespan tasks). |
| What is watched | dbt/SQL/Python runs, Flink jobs, Dagster refusals (read over HTTP from other services) | **Pipelines and connectors**, read straight from DataConnect's own database (section 4). No cross-service tokens needed. |
| Identity | gateway headers, `Principal` | `get_org_id`, `get_actor`, `require("dataconnect:alerts:...")` (section 7). |
| Secret storage | secrets backend (`get_secrets_backend`) | **Encrypted column** using `services/secret_crypto.py` (section 5). |
| Persistence | Postgres + Alembic | Same, `dc_` table prefix, next migration number (latest seen: `0054`). |
| UI | Workspace React feature | DataConnect React app (Redux Toolkit + RTK Query + Module Federation); follow its own conventions (section 8). |

---

## 3. Decisions to take before writing code

### D1. Which failures raise alerts (recommended rule kinds)

Start with the first three; they cover most real incidents and need no new instrumentation.

| Kind | Fires when | Default severity | Data already in DataConnect |
|---|---|---|---|
| `pipeline_failed` | A pipeline's connector is `failed` (connector or task state FAILED/UNASSIGNED). | high | `dc_pipeline_runtime_state.observed_state == "failed"`; reason in the runtime error (`connect_failure_reason`: "Kafka Connect reports ..."). |
| `pipeline_degraded` | Some tasks failed but the connector is up. | medium | `observed_state == "degraded"` (also used for a stalled quality worker, see `quality_worker_health.quality_stall`). |
| `deploy_failed` | A deploy/pause/resume/restart operation ended `failed`. | high | `dc_operations` (`operation_type`: `pipeline_deploy` / `pipeline_pause` / `pipeline_resume` / `pipeline_restart`). |
| `error_threshold_exceeded` | The reconciler paused a pipeline because its dead-letter volume passed its limit. | high | The reconciler writes an audit row `paused_threshold_exceeded` (actor `system:error-threshold`, `after_state` has count/max/window). |
| `dlq_errors` | At least N records on a sink's dead-letter topic in the window. | medium | `dc_failure_counts`, `kind = ingestion`, one-minute buckets (`docs/FAILURE_COUNTERS.md`). |
| `quality_violations` | A quality rule's failures exceed a share or count in the window. | by rule severity | `dc_failure_counts` `kind = validation`, and `dc_quality_results` (`rule_name`, `severity`, `failed_count`, `total_count`, `measured_at`). |
| `schema_drift_pending` | A drift approval has been `pending` for more than N hours. | low | `dc_schema_drift_approvals.status == "pending"`. |
| `runtime_unreachable` | A pipeline's observed state has been `unknown` (Kafka Connect not reachable) for longer than N minutes. | high | `observed_state == "unknown"` and `last_observed_at` (use `last_observed_at`, never `last_reconciled_at`: see the model docstring, FE-BUG-07). |

Default rules created once per organization (editable, deletable, they do not come back after a delete): `pipeline-failed` (high),
`pipeline-degraded` (medium), `deploy-failed` (high), `error-threshold-paused` (high), `high-dlq-errors` (medium), `quality-rule-failing`
(medium). Add the rest when their source is verified.

### D2. How the Acknowledge/Resolve buttons reach DataConnect (this is the one real design risk)

A button in Slack/Teams is only a **link**; the person clicking is not signed in to DataConnect. In Workspace the link opens
`ALERTS_PUBLIC_URL/api/v1/alerts/actions/<signed token>` (no login; the signed token is the credential). DataConnect is different: its
backend is behind **Gate 1 / Envoy** (`app/core/gateway.py`, 403s anything that did not come through the gateway) and the UI is reached through
the Control Portal BFF tunnel (`/control_plane/api/dataplane/<dpId>/api/v1/dataconnect`). So an unauthenticated link needs one of:

| Option | What it needs | Trade-off |
|---|---|---|
| **A. Signed links (as Workspace)** | The platform/gateway team exposes ONE path without a login, e.g. `/dataconnect-alert-actions/*`, to the DataConnect backend. | Real one-click buttons, like Workspace and the Agents app. Needs a security review of that exposure. Anyone who can see the channel message can use the button; history records "Teams channel <name>", not a person. |
| **B. Deep link into the logged-in UI** | A public URL of the portal and a route in the DataConnect UI such as `.../alerts?alert=<id>&action=resolve`. | No unauthenticated endpoint; the click records the real signed-in user. The user has to sign in; slower; needs the host app to forward the query to the federated remote. |
| **C. Buttons only link to the Alerts page** | Only a public portal URL. | Simplest and safest; no Acknowledge/Resolve inside Slack/Teams. |

**Recommendation:** build A's backend exactly as Workspace did (it is small and well tested) but make the buttons **configuration-driven**:
`ALERTS_PUBLIC_URL` + `ALERTS_ACTION_SECRET` set -> signed buttons; not set -> a single "Open in DataConnect" link (B/C) or none. Ask the
platform team which public path the gateway can expose for A. The Agents app already offers these buttons, so the platform very likely supports
a public route; **ask how Agents does it** (we did not have access to that code and designed Workspace's version ourselves).

Whatever you choose, keep the **confirm page** (a GET only shows a page; the button on it POSTs). Link scanners and previews fetch URLs with GET.

### D3. Where the sweep runs

Inside the backend process as another lifespan task, exactly like `runtime_reconciler.run_forever()` / `quality_stream_manager` /
`dlq_counter_manager` in `app/main.py` (`asyncio.create_task(...)`), behind a settings flag, with a **database lease** so two replicas never sweep
at once (the reconciler already does this with `leased_by` / `lease_expires_at`; use the same style, one lease row per organization). Interval
default 60 s. Do **not** make it a Kafka Connect or Prometheus alert-manager dependency: alerts must keep working when those are down.

### D4. Behaviour questions we left open in Workspace (decide, then write it down)

1. **Resolve while the cause is still there.** In Workspace a manual Resolve while the rule still breaches makes the next sweep raise a **new**
   alert (a fresh card). That surprised the user. Recommended for DataConnect: after a manual Resolve, raise again **only on new evidence** (a
   failure observed after the resolve time). Implement by giving each breach an `evidence_at` and comparing with `resolved_at` in `reconcile`.
2. **Who may manage channels.** Webhook URLs are credentials: recommend `Admin` only for create/edit/delete channel and rule; `Admin` +
   `Integrator` for acknowledge/resolve; everyone for view.
3. **Severity icon / colour mapping** is fixed in the message builders (high red, medium amber, low grey).

---

## 4. Observations and rules (the engine)

Workspace's engine is a pure module: `Obs` (what happened to one job) in, `Breach` out, then `reconcile(breaches, live_alerts)` returns
`raise_new / keep / clear`. Keep that shape; it is what makes the rules testable without a database or a network.

DataConnect version of the observation:

```python
@dataclass(frozen=True)
class Obs:
    pipeline_id: str            # the "job key"
    pipeline_name: str
    direction: str              # "SOURCE" | "SINK"  (the "job kind")
    category: str               # "state" | "operation" | "dlq" | "quality" | "drift"
    outcome: str                # failed | degraded | running | paused | unknown | succeeded | pending ...
    occurred_at: datetime       # UTC, timezone-aware
    ref_id: str                 # unique per source event (operation id, "<pipeline>:<state>:<ts>", bucket id)
    detail: str | None          # the reason, shown to people
```

Where observations come from (this is **much simpler than Workspace**: it is all in DataConnect's own database):

* **State** (`pipeline_failed`, `pipeline_degraded`, `runtime_unreachable`): read `PipelineRuntimeState` rows. Record a new observation only
  when `(observed_state, last_runtime_error)` changed (`ref_id = f"{pipeline_id}:{observed_state}:{observed_at}"`), like Workspace's Flink state.
* **Operations** (`deploy_failed`): read `dc_operations` that reached a terminal `failed` since the last sweep (`ref_id = operation id`).
* **DLQ / validation** (`dlq_errors`, `quality_violations`): evaluate directly over `dc_failure_counts` for the window instead of storing
  observations (the counters are already time-bucketed). Keep the *rule* semantics identical (threshold within window).
* **Error threshold pause**: observe the audit row, or add a tiny hook in `_enforce_error_threshold` that records an observation.

Rule semantics to keep exactly (they were all debugged against real data):

* A state rule breaches **while the latest state is bad**, and **clears when the state is good again** (`running`). A cancelled/paused state is
  not a failure.
* A rule scoped to one pipeline looks only at that pipeline.
* An alert is identified by `fingerprint = "<rule id>:<pipeline id>"`; at most one **live** (open/acknowledged) alert per fingerprint, enforced by
  a **partial unique index** (`WHERE state IN ('open','acknowledged')`), so two racing sweeps cannot raise it twice.
* Alerts of rules that were **not evaluated** (disabled, or the source could not be read) are **never cleared** by the sweep.
* A source that is down raises **nothing** by itself and is reported on "Run rules now" ("the dbt Runner could not be reached").
* Reasons are shown **in full on one line** (collapse whitespace, cut with "..." only after 1000 characters). Workspace first cut at 240 and
  showed "...has a recent succe..." to the user. `engine.reason_text` is the helper to copy.

---

## 5. Data model

Six tables, all `org_id`-scoped (`ForeignKey("dc_organizations.org_id")` like the other `dc_*` tables), `UuidPk`, `TimestampMixin`.
Next Alembic revision after `0054_transform_policy_origin`. Copy the Workspace columns; the differences are noted.

| Table | Purpose | Notes |
|---|---|---|
| `dc_alert_channels` | A Slack/Teams webhook | `kind IN ('slack','teams')`, `min_severity`, `scope_pipeline_id` + `scope_pipeline_name` (nullable = all pipelines), `enabled`, `last_attempt_at`, `last_ok`, `last_detail`, `url_hint` (display only: `hooks.slack.com/…/x7Q2`). **The URL is NOT here**: it goes in the encrypted table below. Unique `(org_id, lower(name))`. |
| `dc_alert_channel_secrets` | The webhook URL, encrypted | `channel_id` (PK, FK ON DELETE CASCADE), `envelope JSONB`. Use `secret_crypto.encrypt_fields({"url": url}, org_id=..., connection_id=str(channel_id))` and `decrypt_fields` (the second argument is only the additional-authenticated-data id: pass the channel id). Never log, never return. |
| `dc_alert_rules` | What raises an alert | `kind`, `severity`, `threshold`, `window_seconds` (0 = default 24 h), `min_runs` (use for rate rules), scope, `enabled`. Unique `(org_id, lower(name))`. CHECKs: severity in (low, medium, high), `threshold > 0`, `min_runs >= 1`. |
| `dc_alert_observations` | What the sweep saw (state changes, failed operations) | Unique `(org_id, source, ref_id)`, index `(org_id, pipeline_id, occurred_at)`; pruned after `ALERTS_RETENTION_DAYS` (30). |
| `dc_alerts` | One alert | `rule_id` (FK SET NULL) + `rule_name` copy, `kind`, `severity`, pipeline id/name/direction, `summary`, `fingerprint`, `state` (open/acknowledged/resolved), `delivery` (pending/sent/no_channel/failed), `delivery_attempts`, `raised_at`, `sent_at`, `resolved_at`. Partial unique index on `(org_id, fingerprint) WHERE state IN ('open','acknowledged')` (both `postgresql_where` and `sqlite_where` if you test on SQLite). |
| `dc_alert_events` | History | `action`, `actor`, `note`, `at`. Actions: `raised`, `sent to <channel>`, `delivery to <channel> failed`, `acknowledged`, `resolved`, `cleared automatically`, `status update to <channel> failed`. |
| `dc_alert_state` | Per organization | `defaults_seeded`, `leased_until`, `last_sweep_at`, `last_sweep_ok`, `last_sweep_detail`. The lease is one conditional `UPDATE ... WHERE leased_until IS NULL OR leased_until < now`. |

Register every new unique constraint in whatever table DataConnect uses to turn a unique violation into a clean 409 (Workspace:
`CONSTRAINT_CONFLICTS` in `app/core/errors.py`; **verify** the DataConnect equivalent in `app/core/errors.py`).

---

## 6. Backend, step by step

Build in this order; each step is testable on its own. Package: `app/services/alerts/` (mirror Workspace's file names).

### 6.1 `kinds.py`
Rule kinds with label, description, threshold hint, which numbers they use, and the default rules (section 3, D1).

### 6.2 `engine.py` (pure)
`Obs`, `RuleSpec`, `Breach`, `evaluate_job`, `evaluate`, `reconcile`, `reason_text`. No imports of the database, httpx or settings. Unit-test it
first (Workspace: `tests/test_alerts_engine.py`, 26 cases; copy and adapt them).

### 6.3 `sources.py`
Functions that return `list[Obs]` from the database (section 4). They **never raise**: return an empty result plus a problem string, so one bad
source does not stop the sweep.

### 6.4 `webhooks.py`
Copy. What it contains and why:

* **Provider host allow-list.** Slack: `hooks.slack.com`, `hooks.slack-gov.com`. Teams: `*.webhook.office.com`, `outlook.office.com`,
  `outlook.office365.com`, `*.logic.azure.com`, `*.powerplatform.com` (the last two are the **Workflows** webhooks that replaced the retired
  connector). `https` only (plain `http` only in development and only for hosts listed in `ALERTS_WEBHOOK_EXTRA_HOSTS`, for a local receiver).
  No credentials in the URL. After the allow-list, call DataConnect's own SSRF guard (`app/core/network_policy.validate_destination`, **verify
  its signature**) so a host that resolves to a private/loopback/link-local address is refused; do not follow redirects.
* **Messages.** One `Message` dataclass (title, severity, summary, facts, heading, tone, buttons) rendered three ways:
  Slack = Block Kit sections inside a coloured attachment (+ an `actions` block of URL buttons); Teams Workflows = Adaptive Card 1.4 inside
  `{"type":"message","attachments":[...]}` (`Action.OpenUrl` buttons); classic Teams connector = `MessageCard` with `potentialAction`.
  A host ending `.office.com` gets the MessageCard, anything else the Adaptive Card.
* **Slack escaping is mandatory.** Slack treats `<...>` as links and mentions; an error text containing `<!channel>` would ping the whole
  channel. Escape only `&`, `<`, `>` in the heading, facts, sentence and fallback text (`slack_escape`). Clip the sentence at 2900 characters
  (Slack drops the whole message above 3000). Do not escape Teams text.
* **Result handling.** Slack answers `200 ok`; Teams Workflows `200/202`. Treat 2xx as delivered, everything else as failed with
  `"<status> <first 160 chars of body>"`. A timeout or network error is a failure whose text never contains the URL.
* **Never log the URL.** httpx logs the full URL of every request at INFO. Add the Slack/Teams webhook patterns to
  `app/core/redaction.py` (the secret-shaped patterns list), with a test.

### 6.5 `links.py` and the confirm page
Copy `links.py` and `api/alert_actions.py` (section 3, D2 for exposure). Rules that must stay:

* Token = base64url JSON `{alert, action, channel kind, channel name, expiry}` + HMAC-SHA256 with `ALERTS_ACTION_SECRET`; constant-time
  compare; **no secret configured -> refuse everything** (a token signed with an empty secret is trivially forgeable; there is a test for this).
* Only two actions exist: `acknowledge`, `resolve`. Default expiry 7 days.
* **GET shows a confirm page and changes nothing; POST does it.** Page text is HTML-escaped; headers: `Cache-Control: no-store`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, CSP `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`.
* The action is recorded as done **by the channel**: `"Teams channel <name>"` / `"Slack channel <name>"`, plus an audit record
  (`record_audit(..., actor=...)`).
* A repeated click or an already-resolved alert says "Already acknowledged/resolved" and changes nothing (and writes no second audit row).
* The route is not in the OpenAPI document and needs no permission (decision D2); it is one of the gateway's public paths
  (`app/core/gateway.py` `public_paths()`), so `protect_app` lets it through without a principal.

### 6.6 `service.py`
The class that owns the database work. Copy Workspace's `AlertService` and adapt `_collect`. The parts with traps:

* **Atomic state changes.** `acknowledge`/`resolve` change the state with **one conditional UPDATE** (`WHERE id=:id AND state IN (...)`) and only the
  request that changed it writes the history entry and posts the follow-up. Without this, a double click on the confirm button produced two
  history entries and **two identical Teams cards** (seen in production-like testing). Test it with two sessions and `asyncio.gather`.
* **Sweep order** (each step commits before the network): take lease -> seed default rules once -> collect observations -> store new ones ->
  evaluate enabled rules -> raise new / keep (refresh the summary, tell nobody) / clear (after the commit, announce) -> deliver -> prune ->
  release lease. A failed sweep records the problem and never kills the loop.
* **Delivery.** For each alert not yet `sent`, find matching channels (enabled, alert severity >= channel floor, scope matches). No channel ->
  `delivery = no_channel` (retried every sweep, costs no network). Send per channel; record `sent to <name>` or `delivery to <name> failed` with the
  reason; any success -> `sent`; all failed -> `failed`, retried up to `ALERTS_DELIVERY_MAX_ATTEMPTS` (5). Each channel gets its **own** buttons
  (token carries the channel name).
* **Status follow-ups** (`announce`). On acknowledge / resolve / auto-clear, post a short card ("👀 rule: acknowledged", "✅ rule: resolved",
  "✅ rule: cleared automatically") to the channels that **received** the alert (those in its `sent to <name>` history; never a channel added
  later, disabled, or filtered out). The **acknowledged** follow-up carries a **Resolve** button (only Resolve); the resolved one has none.
  Reason: a card posted through a webhook cannot be edited, so without a follow-up the channel keeps saying "open" with live buttons. Best
  effort: an unreachable channel never fails the action; it is written to the history. Switch: `ALERTS_STATUS_UPDATES`.
* **Open tab semantics.** Open = `state='open' AND delivery='sent'`; `undelivered` = `state='open' AND delivery != 'sent'`.
* `updated_at` columns with `onupdate` are expired after an UPDATE: call `session.refresh(obj)` before serialising (async SQLAlchemy raises
  `MissingGreenlet` otherwise).
* Compare datetimes timezone-aware (`aware()` helper) if you test on SQLite.

### 6.7 `sweeper.py` and wiring
`AlertSweeper.start/stop` creating one `asyncio` task, started from `app/main.py` lifespan next to the reconciler tasks when
`ALERTS_SWEEP_ENABLED` and not in a test run. **Disable it in the test configuration**: a background loop against the in-memory test database
corrupts unrelated tests (this happened in Workspace). Also start it in any separate worker entrypoint DataConnect has.

---

## 7. API and access control

Prefix: under DataConnect's existing router (`app/api/router.py`), e.g. `/alerts`. All routes take `org_id = Depends(get_org_id)`.

| Route | Permission |
|---|---|
| `GET /alerts?state=&from=&to=&limit=&offset=` | `dataconnect:alerts:view` |
| `GET /alerts/counts` (`open`, `acknowledged`, `resolved`, `undelivered`) | `dataconnect:alerts:view` |
| `GET /alerts/{id}` (with history) | `dataconnect:alerts:view` |
| `POST /alerts/{id}/acknowledge`, `/resolve` (optional note) | `dataconnect:alerts:respond` |
| `GET /alerts/rules`, `/rules/kinds`, `/rules/breaching` | `dataconnect:alerts:view` |
| `POST /alerts/rules`, `PATCH`/`DELETE /alerts/rules/{id}`, `POST /alerts/rules/run` ("Run rules now") | `dataconnect:alerts:manage` |
| `GET /alerts/channels` | `dataconnect:alerts:view` |
| `POST /alerts/channels`, `PATCH`/`DELETE /alerts/channels/{id}`, `POST /alerts/channels/{id}/test` | `dataconnect:alerts:manage` |
| `GET/POST /alerts/actions/{token}` | none (signed token; decision D2) |

* The three permissions are declared in `permissions.json` (the file the Admin Portal imports) and mapped to the seeded roles in
  `docs/auth/PERMISSIONS.md` ("Seed delta": view for everyone, respond for admin + integrator, manage for admin). **Do not edit a
  frontend copy**: regenerate it (`npm run gen:access-contract` in the frontend writes `src/types/permissions.generated.ts`).
* Validation: names unique per organization, case-insensitive (409 with a stable code such as `ALERT_CHANNEL_NAME_TAKEN`); webhook URL errors
  are a 400 naming the field (`webhook_url`) so the form can show the message under the field; rule numbers validated per kind (percent <= 100,
  allowed windows only).
* A channel's `webhook_url` is accepted on create/edit and **never returned** (response has only `url_hint`); editing with a blank URL keeps
  the stored one; changing the channel *type* requires a new URL.
* Audit every create/update/delete/test/acknowledge/resolve with `record_audit`.
* Regenerate the OpenAPI types for the frontend (`npm run gen:api-types`).

---

## 8. Frontend

Follow DataConnect's own structure (`src/pages/*.tsx`, `src/components/*`, `src/store/apiSlice.ts`, `src/permissions.ts`, `notificationsSlice` for
toasts, `usePersistedChoice` for remembering the tab). The product requirement is that DataConnect UI matches the Workspace pattern (the apps are
bundled into one product): same action icons, search/filter styling, tab look (underlined tab with count chips), card look. **Copy the visual
structure from `InaiEra_workspace_frontend/src/features/alerts/`** and rebuild it with DataConnect's components.

| Workspace file | Becomes in DataConnect |
|---|---|
| `AlertsPanel.tsx` (header, five tabs, dates, pager, drawers) | `src/pages/Alerts.tsx` + `PageId "alerts"` in `DataConnectApp.tsx` (`TITLES`, `PAGES`) + a nav entry and icon in `NavIcons.tsx`. |
| `AlertList.tsx` (cards + "no channel yet" note) | components in the page folder. |
| `AlertDetailDrawer.tsx` | right-hand drawer (see below). |
| `RulesTab.tsx`, `RuleDrawer.tsx` | Rules table with switch/edit/delete; "New rule" drawer. Button label is **"New rule"** (the user asked for exactly this). **No** "Currently breaching" list under the table (removed on request: the Open tab already shows it). |
| `ChannelsTab.tsx`, `ChannelDrawer.tsx` | Channel cards; "Add channel" drawer. |
| `api.ts`, `kinds.ts`, `format.ts`, `ui.tsx` | RTK Query endpoints in `apiSlice.ts`; kind labels; formatting helpers; chips and the on/off switch. |

UI rules that were requirements, not taste:

1. **Every form and the alert detail is a right-hand drawer** (`position: fixed; right: 0; top: 0; bottom: 0`), including *Add channel*. DataConnect
   has `Modal.tsx`; do not use it for these. Add a test asserting the drawer is pinned right.
2. **Send test** button uses the platform's **test icon** (the plug-connected glyph, the same "test connection" icon used elsewhere), not a
   generic send icon. Show the result as a toast and on the card ("last send ok" / "last send failed" + reason).
3. Channel type chooser: two buttons (Slack, Microsoft Teams) with a one-line hint and a placeholder URL for each. The URL field says "Stored
   encrypted and never returned by the API"; when editing it shows only the hint and "leave blank to keep the current one".
4. A rule form shows only the fields its kind uses (Window hidden for a state rule; Minimum runs only for a rate rule); a numeric threshold
   is validated inline (whole number, 1..max for the kind).
5. Open tab: show the "N alerts reached no channel yet" note with a link to Channels. Counts on the tabs and a badge on the sidebar entry
   (poll `/alerts/counts` about once a minute; use `pollingPolicy.ts`).
6. Pager: 10 per page, Previous/Next, "Showing 11-20 of 34 alerts"; changing tab or the date filter goes back to page 1. Date filters: From/To +
   "Clear dates" (only visible when set).
7. The page is gated by `dataconnect:alerts:view`; without `dataconnect:alerts:manage` there are no add/edit/delete/test buttons and the switches are disabled; Acknowledge/Resolve only with `dataconnect:alerts:respond`.
8. The tab you were on survives a refresh but arriving from the sidebar opens **Open** (same rule as Workspace's "refresh keeps your place").
9. Do not leave sample/demo data in the UI. (Workspace once shipped a dev-only sample-notification seeder that showed a fake "Run queued" toast
   on every page load until it was removed.)

---

## 9. Message anatomy (for reference)

Teams Workflows (Adaptive Card 1.4), what the user sees: a red heading "🚨 pipeline-failed", a fact table, the sentence, then **Acknowledge** and
**Resolve** buttons.

```json
{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","contentUrl":null,"content":{
  "$schema":"http://adaptivecards.io/schemas/adaptive-card.json","type":"AdaptiveCard","version":"1.4",
  "body":[
    {"type":"TextBlock","weight":"Bolder","size":"ExtraLarge","wrap":true,"color":"Attention","text":"🚨 pipeline-failed"},
    {"type":"FactSet","facts":[{"title":"Severity","value":"HIGH"},{"title":"Pipeline","value":"orders-cdc"},
      {"title":"Kind","value":"source"},{"title":"State","value":"open"},{"title":"Raised","value":"2026-10-30 09:12 UTC"}]},
    {"type":"TextBlock","wrap":true,"text":"Pipeline 'orders-cdc' failed: Kafka Connect reports ... ConfigException: ..."}],
  "actions":[{"type":"Action.OpenUrl","title":"Acknowledge","url":"https://…/alerts/actions/<token>"},
             {"type":"Action.OpenUrl","title":"Resolve","url":"https://…/alerts/actions/<token>","style":"positive"}]}}]}
```

Slack: `{"text":"[HIGH] ...","attachments":[{"color":"#DC2626","blocks":[section(heading), section(fields), section(sentence), actions(url buttons)]}]}`.
Follow-ups use the same builders with `tone = "ack"` (👀, blue) or `"resolved"` (✅, green).

---

## 10. Configuration

| Setting (env, `DATACONNECT_` prefix if that is how DataConnect names them: **verify** `app/core/config.py`) | Default | Meaning |
|---|---|---|
| `ALERTS_SWEEP_ENABLED` | true (false in tests) | Run the sweep loop in this process. |
| `ALERTS_SWEEP_INTERVAL_SECONDS` | 60 | Sweep period. |
| `ALERTS_WEBHOOK_TIMEOUT_SECONDS` | 10 | Per post. |
| `ALERTS_DELIVERY_MAX_ATTEMPTS` | 5 | Then the alert stays `failed`. |
| `ALERTS_RETENTION_DAYS` | 30 | Observations kept. |
| `ALERTS_WEBHOOK_EXTRA_HOSTS` | `[]` | Extra allowed webhook hosts (corporate relay; dev local receiver, the only case where `http://` is accepted, and only when the environment is development). |
| `ALERTS_PUBLIC_URL` | empty | Address people reading Slack/Teams can open. With the secret below, buttons appear. |
| `ALERTS_ACTION_SECRET` | none | Long random value signing the links. In Helm take it from a Secret (`secretKeyRef`), never a plain value. |
| `ALERTS_ACTION_LINK_TTL_SECONDS` | 604800 | Link lifetime. |
| `ALERTS_STATUS_UPDATES` | true | Post the "acknowledged/resolved/cleared" follow-ups. |

Production start-up validation (`app/startup.py` `validate_configuration`): refuse to start with `ALERTS_PUBLIC_URL` set but no
`ALERTS_ACTION_SECRET`, and refuse a secret shorter than 32 characters. Add the settings to the Helm values/configmap and the compose file.
The outbound call to Slack/Teams needs HTTPS egress to the internet; in Kubernetes that means the namespace's network policy must allow it.

---

## 11. Test plan (copy Workspace's tests, then adapt)

Workspace test files and what they prove; port each and keep the **mutation check** habit (break the code on purpose, confirm a test fails,
restore, verify the restore). All 70+ mutations in Workspace were killed; four first survived and each one exposed a missing test.

| Workspace file | Covers |
|---|---|
| `test_alerts_engine.py` | each rule kind, windows, scope, latest-state logic, reconcile (raise/keep/clear, disabled rules never cleared), the reason is never cut short |
| `test_alerts_webhooks.py` | allowed/refused URLs (suffix tricks, http, userinfo, private hosts), message bodies for Slack/Teams/classic, buttons, **Slack escaping of `<!channel>`**, Slack clip, send results (2xx, 4xx/5xx, redirect, timeout, no URL in errors), **log redaction of webhook URLs** |
| `test_alerts_links.py` | signature, expiry, tampering, wrong secret, **empty secret**, unknown action |
| `test_alerts_sources.py` | each reader; a down source reports and returns nothing |
| `test_alerts_service.py` (real Postgres) | default rules once, validation, names unique case-insensitively, secrets never in the database, sweep raise/send/never-resend/clear, severity floor and scope, retries then stop, disabled rule leaves its alert, lease, pruning, no-channel then channel added |
| `test_alerts_api.py` (real Postgres) | who may do what, URL never returned, 400/409 shapes, test-send, full alert lifecycle |
| `test_alerts_actions.py` (real Postgres) | buttons per alert and channel, GET changes nothing, POST acts as the channel, forged/expired/unknown links, escaping, headers, audit row, not in OpenAPI, **Resolve button on the acknowledged follow-up** |
| `test_alerts_status_updates.py` (real Postgres) | follow-ups to the right channels only, no repeat, unreachable channel noted, **two simultaneous clicks make one change and one post** |

Use a real, migrated Postgres for the service/API tests (a scratch database on the dev Postgres; run `alembic upgrade head` into it; drop it
afterwards). They also prove the migration on a fresh database. In Workspace they run in a throwaway container from the service image with
`INTEGRATION_DATABASE_URL`.

**Never mutate code while the live stack is up.** In Workspace the container bind-mounts `app/` with `--reload`, so a mutation check briefly ran
the *mutated* sweep on live data and posted two false alerts to the user's real channels. Stop the service (or set
`ALERTS_SWEEP_ENABLED=false`) before running mutation checks.

Frontend: tests for tab order and counts, remembered tab, lists, empty states, the "no channel yet" note, pager and dates, drawers pinned right,
channel and rule forms (validation, error under the right field, payloads), Send test icon, who sees which buttons, sidebar badge, and "arriving
from the sidebar opens Open".

### Verifying for real (do this once, with the user)

1. Create a Teams channel and a Slack channel (steps in section 12), add each in DataConnect, press **Send test**.
2. Cause a **real** failure: stop the source database of a running pipeline (or point a connector at a bad credential). The connector goes
   FAILED -> `pipeline_failed` alert within about a minute -> cards in Teams and Slack.
3. Press **Acknowledge** on a card (confirm page -> "Alert acknowledged") -> a 👀 follow-up with a Resolve button appears in both channels ->
   press **Resolve** -> a ✅ follow-up appears. Check the Alerts page tabs and the alert history ("acknowledged by Teams channel ...").
4. Start the database again and restart the connector: a still-open alert is **cleared automatically** and the channels are told.
5. Press a button twice quickly (double click): exactly one history entry and one follow-up.

---

## 12. Setting up the channels (for whoever tests it)

**Teams** (webhook through the Workflows app; the old "Incoming Webhook" connector is retired): in the channel, `...` -> **Workflows** -> template
**"Send webhook alerts to a channel"** -> Next -> Add workflow -> copy the URL (`https://prod-..logic.azure.com/...` or
`https://...environment.api.powerplatform.com/...`). If Workflows is missing, the Microsoft 365 admin has to allow it. If a test says "ok" but
nothing appears, open Workflows -> the flow -> **Run history**.

**Slack:** create the channel, then https://api.slack.com/apps -> Create New App -> **From scratch** (or "Blank app") -> Incoming Webhooks ->
Activate -> **Add New Webhook to Workspace** -> pick the channel -> Allow -> copy `https://hooks.slack.com/services/T../B../...`. **Do not enable
Interactivity** (URL buttons need none; with it on Slack expects a reply to each click).

Then in DataConnect: **Alerts -> Channels -> Add channel**, choose the type, name, paste the URL, **Add channel**, **Send test**.

---

## 13. Known limits (say these to users up front)

* A card posted through a webhook **cannot be edited**; after a click the original card still shows "open". The follow-up message is how the
  channel learns the new state.
* Slack/Teams cannot send a click back through a webhook; the buttons are links to DataConnect (decision D2).
* Anyone who can see the message can use its buttons (the token is the credential); history records the channel, not the person.
* A link to `http://localhost` only works on the machine running DataConnect; others need a reachable `ALERTS_PUBLIC_URL`. Whether Teams opens an
  `http://` link from a card was not verifiable without a Teams tenant at the time (it worked from the user's own Teams desktop on the same Mac).
* The sweep notices a change about a minute later (Kafka Connect state is itself polled by the reconciler on its own cadence).
* Resolved alerts keep the text they had; open/acknowledged alerts refresh their text on each sweep.

## 14. Suggested phases

1. **Core + channels:** tables, engine, `pipeline_failed` / `pipeline_degraded` / `deploy_failed`, channels with Send test, delivery, Alerts page
   (all five tabs), badge. No buttons yet (or an "Open in DataConnect" link).
2. **Buttons + follow-ups:** links, confirm page (after decision D2), follow-ups with the Resolve button, atomic transitions, status updates.
3. **More sources:** `error_threshold_exceeded`, `dlq_errors`, `quality_violations`, `schema_drift_pending`, `runtime_unreachable`.
4. **Polish:** "re-alert only on new evidence" (D4.1), Helm/compose settings, production start-up validation, documentation for users.

## 15. Questions to ask before starting

1. Which public path can the gateway expose for the Acknowledge/Resolve links, and how does the Agents app do it today?
2. Is the **Admin-only** rule for managing channels acceptable, and may Integrators acknowledge/resolve?
3. Which of the D1 kinds does the product owner want in the first release?
4. Should a manual Resolve silence the alert until a **new** failure (D4.1), or re-alert while the cause persists?
5. Is the sweep allowed inside the API process, or does DataConnect run background work in a separate worker deployment?
