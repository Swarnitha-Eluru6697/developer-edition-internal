# InAiEra Agents — Manual Testing Guide

Audience: anyone verifying a build before merge/release. There is no
automated test suite yet (see Developer Guide §10) — this is the procedure
to follow in its place.

---

## 1. Environment setup

```bash
docker compose down -v && docker compose up -d      # clean Postgres
cd backend && uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev                            # http://localhost:5173
```

Confirm a clean seeded state before starting:
```bash
curl -s http://127.0.0.1:8000/api/agents | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
# → 8
```

**Persistence-check convention used throughout this guide:** wherever a step
mutates data, verify it with a *second, separate* request/page-load — not
just by checking that the immediate response/UI looks right. This is not
optional pedantry: it's exactly the class of bug this app has already
shipped once (see Developer Guide §5, the JSONB mutation gotcha) and is the
single most important habit for testing this codebase.

---

## 2. Navigation & personas

| ID | Steps | Expected |
|---|---|---|
| NAV-01 | Load the app | Overview page loads, sidebar shows Build/Operate groups + Settings |
| NAV-02 | Switch persona to Builder | Alerts and Settings disappear from sidebar |
| NAV-03 | Switch persona to Security Admin | Workflow IDE, Templates, Integrations disappear; Alerts appears |
| NAV-04 | Switch persona to Platform Admin | All nav items visible |
| NAV-05 | Click each sidebar item in turn | Each route loads without console errors |
| NAV-06 | Reload the page (F5) mid-session | App reloads to Overview (route state doesn't persist — hash routing, expected) |

---

## 3. Agents & Agent Profile

| ID | Steps | Expected |
|---|---|---|
| AGT-01 | Agents page | Table shows 8 seeded agents with correct type/plane/status/health chips |
| AGT-02 | Click a published agent (e.g. Revenue Analysis Agent) | Agent Profile drawer opens, Overview tab active by default |
| AGT-03 | Click through all 9 tabs | Each renders without error; Quality shows a health gauge; Lineage shows upstream/downstream chain |
| AGT-04 | Open the **Onboarding Concierge** (draft status) agent, check Usage/Logs/Monitoring/Traces/Live jobs tabs | Each shows the "hasn't been published yet" empty state, not blank tables or fake zeros |
| AGT-05 | On Entitlements tab, click "Manage in UCS Catalog" | Toast appears ("managed in UCS Catalog — a separate console"); no navigation, no error |
| AGT-06 | On Monitoring tab, click "Open platform Monitoring" | Drawer closes, navigates to platform Monitoring page |
| AGT-07 | On Traces tab, click a trace row | Switches to Trace Detail drawer for that trace |
| AGT-08 | Close drawer (X, or click outside) | Drawer closes cleanly, no leftover overlay |
| AGT-09 | **New agent** → fill Data Agent / name / template → Create | Toast "Agent created", navigates to Builder with the new agent loaded |
| AGT-10 | Refresh Agents list after AGT-09 | New agent appears in the table (9 total) — confirms real persistence, not just optimistic UI |

---

## 4. Workflow IDE (builder)

Use the default agent (`?agent=a1`, Revenue Analysis Agent) unless noted.

| ID | Steps | Expected |
|---|---|---|
| BLD-01 | Open builder | 6 nodes visible: Trigger, UCS Check (dashed), Retrieval, LLM, Tool, Output, connected left-to-right |
| BLD-02 | Click the LLM node | Config panel shows Agentic/Fixed toggle, model select, etc. |
| BLD-03 | Drag a **Question Classifier** node from the palette onto empty canvas space | New node appears at drop position, auto-selected, config panel updates, toast "Question Classifier node added" |
| BLD-04 | **Reload the page** (or refetch `/api/agents/a1`) | The node from BLD-03 is still present — **this is the critical persistence check**; if it's gone, the JSONB bug has regressed |
| BLD-05 | Drag the new node to a different position | Node moves, position updates live |
| BLD-06 | Reload / refetch | New position persisted |
| BLD-07 | Change a config field on the new node (e.g. a Class name) and click elsewhere (blur) | Value saved without needing an explicit save button |
| BLD-08 | Reload / refetch | Config change persisted |
| BLD-09 | Select the new node → Delete node | Node removed from canvas immediately |
| BLD-10 | Reload / refetch | Node stays deleted (not resurrected) |
| BLD-11 | Try to drag the **Trigger** node | Nothing happens — locked nodes reject drag (native `dragstart` prevented) |
| BLD-12 | Select the **UCS Check** node | Config panel shows read-only info (cache TTL, max chain depth), no delete button |
| BLD-13 | Repeat BLD-03–10 for at least 3 other node types (e.g. Code, HTTP Request, Human Input) spot-checking each has a distinct, working config form | All persist correctly |
| BLD-14 | Click **Publish** | Modal opens, shows Draft→Staged→Published steps and an info banner; click **Publish** button |
| BLD-15 | Wait for publish to complete | Toast "Agent published — vN is now live"; modal closes |
| BLD-16 | Reload Agents list, check the agent's version/status | Version incremented, status = published |
| BLD-17 | Click **Cancel** on the Publish modal *before* clicking Publish | Modal closes, agent version/status unchanged (confirms publish doesn't fire until confirmed) |
| BLD-18 | **Publish an agent with no bound roles** (e.g. Schema Migration Designer) | Real HTTP 403 — publish is genuinely blocked by the entitlement check, not a canned success |
| BLD-19 | Click **Test** on agent a1 (Revenue Analysis Agent — has role bindings) | Toast shows `SUCCESS via sync (direct harness call, no Temporal) — [MOCK ...]` (or a real model response if `ANTHROPIC_API_KEY` is set) |
| BLD-20 | Open Runs/Traces pages after BLD-19 | A new `r-live-...` run and `tr-live-...` trace appear — confirms this was a real execution, not a UI-only toast |
| BLD-21 | Open the agent's Logs tab after BLD-19 | A new log line appears: `Live run (sync ...) · status=success` |
| BLD-22 | **Test** an agent with no role bindings | Toast shows `DENIED` — the entitlement gate genuinely blocked execution, matching BLD-18's publish behavior |

---

## 5. Templates

| ID | Steps | Expected |
|---|---|---|
| TPL-01 | Templates page | 7 template cards render (4 Data, 2 App, 1 Blank canvas) with correct type chips |
| TPL-02 | Click "Use template" on any card | Toast fires, navigates to Builder |

---

## 6. Runs & Traces

| ID | Steps | Expected |
|---|---|---|
| RUN-01 | Runs page, default filter | All 7 seeded runs shown |
| RUN-02 | Filter by Failed | Only failed-status runs shown (Pipeline Engineer Agent, Churn Model Scientist) |
| RUN-03 | Filter by Denied | Only the Vendor Onboarding Automation denied run shown |
| RUN-04 | Click a run row | Run Detail drawer opens with trace waterfall + principal chain |
| RUN-05 | Open the denied run specifically | Principal chain shows a "deny" (red X) gate at the failing hop, not all green checks |
| TRC-01 | Traces page, no filter | All 6 seeded traces shown |
| TRC-02 | Filter by a specific agent | Only that agent's traces shown |
| TRC-03 | Click a trace with `status=ucs_denial` (Vendor Onboarding) | Waterfall shows the UCS span in red/fail styling with a "DENIED" note |
| TRC-04 | Click a trace with `status=harness_fail` (Pipeline Engineer) | Harness span shows fail styling with "STEP LIMIT" note |

---

## 7. Alerts

| ID | Steps | Expected |
|---|---|---|
| ALT-01 | Switch to Platform Admin or Security Admin, open Alerts | Open tab shows 3 alerts by default |
| ALT-02 | Switch to Resolved tab | Shows the 1 pre-resolved alert (Support Knowledge Bot, priv-escalation) with a 2-entry history already visible |
| ALT-03 | Open an open alert → **Acknowledge** | Toast "Alert acknowledged"; drawer stays open; History section gains a new "Acknowledged by \<persona\>" entry with a real timestamp; alert stays in Open tab but now shows an "acknowledged" chip |
| ALT-04 | Close and reopen the same alert (or refresh the list) | Acknowledged state and history entry persisted |
| ALT-05 | Switch persona (e.g. Builder → can't see Alerts, so switch to the *other* admin persona) then acknowledge/resolve again | The `by` field in the new history entry matches the *currently active* persona, not a hardcoded value |
| ALT-06 | On the same alert, click **Resolve** | Toast "Alert resolved"; drawer closes; alert moves to Resolved tab with 2 history entries total |
| ALT-07 | On the Alerts list page (Open tab), find an acked-but-unresolved alert | "Last: Acknowledged by \<persona\> · \<timestamp\>" line shown under the summary |

---

## 8. Monitoring

| ID | Steps | Expected |
|---|---|---|
| MON-01 | Monitoring page, no filter | All 5 metric-category sections render with platform-aggregate numbers; Evaluations table shows all 5 seeded eval rows |
| MON-02 | Filter by **Churn Model Scientist** | Run/Harness/UCS stat cards change to that agent's numbers (visibly worse than platform average — 71.2% success, higher step-limit/guardrail rates); Evaluations table narrows to just that agent's row |
| MON-03 | Filter by **Onboarding Concierge** (draft, no metrics) | Stat cards show "—" placeholders, not blank or crashing |
| MON-04 | Clear filter back to "All agents" | Returns to platform aggregate |

---

## 9. Integrations

| ID | Steps | Expected |
|---|---|---|
| INT-01 | Integrations → Model Providers tab | "Connected" section shows OpenAI/Anthropic/Gemini; "Configuration required" shows the rest with correct badges |
| INT-02 | Click **Add API Key** on Cohere | Modal opens with Authorization Name/API Key/Organization/Base URL/Validate Model/Protocol fields |
| INT-03 | Submit with API Key blank | Toast error "API key is required"; modal stays open |
| INT-04 | Fill API Key, Save | Toast "Cohere API key saved"; modal closes |
| INT-05 | Reload Model Providers tab | Cohere now shows under "Connected" (status flipped to credits) — confirms real persistence |
| INT-06 | Tools tab → click JSON Process | Drawer opens showing 4 actions (Parse/Delete/Replace/Insert) |
| INT-07 | Click "JSON Parse" action | Drawer shows Back button, parameters `json_data` and `json_filter`, both marked Required, type `string` |
| INT-08 | Click Back | Returns to the 4-action list, not closed |
| INT-09 | Close and reopen a *different* tool | Confirms no stale `toolAction` state leaks between tools |
| INT-10 | MCP tab | Shows Figma MCP and Linear MCP with action counts |
| INT-11 | **Add MCP Server** → fill Server URL/Name/Identifier, leave on Authentication tab, submit | New server appears in the list on reload |
| INT-12 | Reopen Add MCP Server, click Headers tab, then Configurations tab | Both render distinct content; switching tabs doesn't lose the Server URL/Name already typed |
| INT-13 | Submit with Name or Identifier blank | Toast error, no server created |
| INT-14 | Data Sources tab → Configure **Google Drive** | OAuth-style modal: redirect URI banner, Client ID/Secret fields |
| INT-15 | Configure **SharePoint** specifically | Same OAuth modal *plus* SharePoint Subdomain and Tenant ID fields |
| INT-16 | Configure **Google Cloud Storage** | API-key-style modal: Authorization Name, Credentials, **Bucket Names** field present |
| INT-17 | Configure **Notion** | API-key-style modal, but *no* Bucket Names field (only GCS gets that field) |
| INT-18 | Complete any data source config, reload the tab | Status flips to "Connected" — persisted |

---

## 10. Settings

| ID | Steps | Expected |
|---|---|---|
| SET-01 | Settings page (Platform Admin) | Both runtime plane cards show "healthy"; infra connections show Temporal/Kafka/Postgres all green/confirmed; Model catalog list matches Integrations → Model Providers data |

---

## 9a. Config panel field parity (regression check)

This class of bug already shipped once: 9 of 16 node types were missing
config fields present in the approved mockup, because the mockup-to-React
port silently dropped them. Whenever `NodeConfigPanel.tsx` changes, re-run
this check rather than trusting that "it compiles" means "it's complete."

| ID | Steps | Expected |
|---|---|---|
| PARITY-01 | Extract the mockup's `cfgPanel()` JS function; list every `<label>` per node type | Reference list of expected fields per type |
| PARITY-02 | Extract every `<Field label=` and toggle `<span className="small">` per `case` block in `NodeConfigPanel.tsx` | Actual list of implemented fields per type |
| PARITY-03 | Diff the two lists per node type | Zero fields present in the mockup but absent from React, for all 16 types |
| PARITY-04 | For any field added to close a gap, set it via the UI, then re-fetch the agent via a **separate** request | Field value persists — not just reflected in the immediate response |
| PARITY-05 | For fields that are supposed to affect real execution (not just persist), trigger a run and confirm the effect shows up in `harness.py`'s actual behavior (e.g. HTTP headers appear in the request; a custom Instruction appears in the model call) | Confirmed via direct function call with a mocked `call_llm`/inspectable output, not assumed from the UI alone |

---

## 10a. Real execution (backend-level — needs Temporal running)

These exercise paths the UI alone can't fully confirm. Start the full stack
per the README's "Running real execution" section (Postgres, `temporal
server start-dev`, `python -m app.worker`, `uvicorn`) before running these.

| ID | Steps | Expected |
|---|---|---|
| EXE-01 | `curl -X POST /api/agents/a1/run -d '{"input":"test","invoking_user":"user:maria"}'` | `200`, `status: success`, spans for all 6 default nodes, `execution_mode: "sync ..."` |
| EXE-02 | Same against `a2` (Schema Migration Designer — no role bindings) | `status: denied`, `fail_class: "ucs"`, spans stop at the `gov` node |
| EXE-03 | Add a `hitl` node to an entitled agent (e.g. `a5`), then `run` it | `execution_mode: "durable (real Temporal workflow)"` |
| EXE-04 | `temporal workflow list --address 127.0.0.1:7233` after EXE-03 | Shows a `Completed` `AgentRunWorkflow` — **this is the real confirmation**, independent of the API's own response |
| EXE-05 | Stop the worker process, retry EXE-03 | Request hangs/times out (workflow queued but no worker to process it) — expected Temporal behavior, not a bug; restart the worker to confirm it then completes |
| EXE-06 | Add a `code` node with `print(2+2)` to an entitled agent, run it | Span for the `code` node shows `status: ok` with a real non-zero `duration_ms` |
| EXE-07 | Add a `code` node with an infinite loop (`while True: pass`), run it | Execution returns within ~5s with a timeout result, not hanging forever — confirms the sandbox's time limit is real |
| EXE-08 | Set `ANTHROPIC_API_KEY` to an invalid value, restart the API, run any entitled agent | LLM output text starts with `[LLM call failed: ...]` — a real call was attempted and failed loudly, not silently swapped for a mock |
| EXE-09 | Unset `ANTHROPIC_API_KEY`, restart the API, run again | LLM output starts with `[MOCK — no ANTHROPIC_API_KEY set]` |

---

## 11. Cross-cutting regression checks

Run these whenever `models.py`, `db.py`, or any router in `agents.py` changes:

| ID | Steps | Expected |
|---|---|---|
| REG-01 | Add a node, then immediately hit `GET /api/agents/{id}` via curl (not the UI) | New node present in the raw JSON — bypasses any UI-side optimistic-update masking |
| REG-02 | Restart the backend process (not the DB) mid-session, re-fetch an agent | Data unchanged — confirms it's in Postgres, not in-process memory |
| REG-03 | `docker compose down -v && up -d`, restart backend | Fresh seed data returns (8 agents, 3 open alerts, etc.) — confirms seed script runs cleanly against an empty DB |
| REG-04 | `psql` into the DB, `\d agent` | `flow` and `metrics` columns show type `jsonb`, not `json` or `text` |

---

## 12. Bug report template

```
**ID / Area:** (e.g. BLD-04)
**Steps to reproduce:**
**Expected:**
**Actual:**
**Persisted across a fresh request/reload?** yes/no — if no, treat as high
  priority; it's likely the JSONB aliasing bug class (Developer Guide §5)
**Console errors (frontend):**
**Backend log excerpt:**
```
