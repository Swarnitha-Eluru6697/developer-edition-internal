# InAiEra Agents — User Guide

Audience: everyone who uses the platform — builders, platform admins, and
security admins. This guide is task-oriented: find what you want to do,
follow the steps.

InAiEra Agents is where you build, publish, and operate Data Agents and App
Agents — a visual Workflow IDE for the build side, and Runs/Traces/Alerts/
Monitoring for the operate side. Governance (entitlements, audit) lives in
UCS Catalog, a separate console — this app doesn't duplicate it.

---

## 1. Personas & what each can do

| Persona | Purpose | Sees |
|---|---|---|
| **Builder** | Build and consume agents | Overview, Agents, Workflow IDE, Templates, Integrations, Runs, Traces, Monitoring |
| **Platform Admin** | Build + operate + configure | Everything Builder sees, plus Alerts, Settings |
| **Security Admin** | Monitor and respond to alerts | Overview, Agents, Runs, Traces, Monitoring, Alerts (read-only on build screens) |

Switch persona from the segmented control in the top bar. In production this
would be driven by your real login role; here it's a UI convenience for
trying out each view.

---

## 2. The console at a glance

Left sidebar is grouped by intent:
- **Build** — Overview, Agents, Workflow IDE, Templates, Integrations
- **Operate** — Runs, Traces, Monitoring, Alerts
- **Settings**

The **Overview** page shows platform posture at a glance: active agent
count, runs today, open alerts, recent activity, and runtime plane health.

---

## 3. Working with agents

### Find an agent
1. **Agents** in the sidebar → browse the table (filter mentally by type,
   plane, status — columns show all of it at a glance).
2. Click a row to open its **Agent Profile**.

### Read an Agent Profile
The profile has nine tabs:

| Tab | Shows |
|---|---|
| Overview | Type, plane, owner, version, status, model, mode |
| Quality | Health score gauge + eval pass rate / run success trend / drift |
| Lineage | Upstream (data this agent reads) and downstream (what it produces) |
| Entitlements | Read-only view of bound roles — actual bindings are managed in UCS Catalog |
| Usage | Runs (7d), cost (7d), recent invocations |
| Logs | Structured log lines, correlated to trace ID — click a line to jump to its trace |
| Monitoring | The full metrics catalog (run/harness/UCS-governance layers), scoped to this one agent |
| Traces | This agent's traces only, with a shortcut to the platform-wide Traces page pre-filtered |
| Live jobs | Currently-executing runs (empty in this environment — no real runtime, see note below) |

If an agent is still a **draft** (never published), the Usage/Logs/
Monitoring/Traces/Live jobs tabs show an empty state instead of misleading
zeros — there's genuinely nothing there yet.

**A note on real execution:** clicking **Test** in the Workflow IDE now
genuinely runs the agent's flow — walks the actual node graph, checks real
entitlements (an agent with no bound role is genuinely denied, not just
displayed as if it might be), and calls a real or mock LLM depending on
whether an API key is configured. The resulting Run/Trace/Log entries you
see afterward are real, not seed data. The historical rows you see *before*
running anything are still the original seeded fixtures. "Live jobs" stays
empty because there's no concept of a long-running in-progress job yet —
every execution here completes synchronously (or via one real Temporal
workflow for flows with a Human Input node). See the Developer Guide's §9
for exactly what's real vs. still simplified in execution itself.

### Create a new agent
1. **Agents** → **New agent**.
2. Choose **Data Agent** (runs on the Data plane — Analysis, Engineer, ERD,
   Scientist) or **App Agent** (App plane — Conversational, Task-Automation).
3. Name it, pick a starting template (or **Blank canvas**).
4. **Create** — opens straight into the Workflow IDE.

---

## 4. Using the Workflow IDE (builder)

The canvas has three parts: the node **palette** (left), the **canvas**
(center), and the **configuration panel** (right, shows whichever node is
selected).

### Add a node
Drag any node from the palette onto the canvas. It drops where you release
it. **Trigger**, the **UCS Check** (dashed border), and **Output** are
already on every canvas and can't be dragged in separately — Trigger/Output
are fixed endpoints, and UCS Check is inserted automatically as a governance
step, not something you configure.

### Move or delete a node
Drag any non-locked node to reposition it. Click it, then use **Delete
node** in the config panel on the right. Locked nodes (Trigger/UCS
Check/Output) can't be moved or deleted.

### Configure a node
Click any node — its settings appear on the right. Every field you change is
saved immediately (or on blur, for text fields) — there's no separate Save
button for node config.

The palette covers 16 node types across five groups:

| Group | Nodes |
|---|---|
| Reasoning | LLM, Knowledge Retrieval, Agent |
| Question Understand | Question Classifier |
| Logic | IF/ELSE, Human Input, Iteration, Loop |
| Transform | Code, Template, Variable Aggregator, Doc Extractor |
| Connect | Tool/Connector, HTTP Request |
| InAiEra platform | Sub-Agent, Memory/State |

The **LLM** node has a Fixed/Agentic mode toggle — Agentic runs a reasoning
loop that decides which tools to call; Fixed is a single deterministic call.

### Publish
Click **Publish** in the top-right of the builder. This resolves the run's
entitlement plan (which catalog objects the flow touches) before the new
version goes live — you'll see a short "Publishing…" state, then a
confirmation. This check is real: an agent with no entitlement bindings will
be blocked from publishing rather than silently allowed through.

---

### The Agent node's Configure panel

Select an Agent node to configure it in six parts.

- **Model.** The chat model the agent runs on. Pick a recent model that is strong at reasoning and tool calling. A model that is missing from the catalog, unavailable, or not a chat model is marked **Incompatible**.
- **Prompt.** The agent's role and goal, how it should approach the work, when to use tools and files, and what the output looks like. `{{var}}` and `{{env.KEY}}` are replaced at run time. (Older flows that used *Instructions* keep working; the field now shows as Prompt.)
- **Skills.** **+ Add → Upload skill.zip.** A skill is a `.zip` or `.skill` file, up to 50 MB, with a `SKILL.md` (up to 256 KB) at its root or inside one top-level folder. `SKILL.md` starts with `name` (lowercase letters, digits, hyphens) and `description` frontmatter. **Every skill attached to the node is included in the agent's instructions on every run**, so attach only the ones this node needs.
- **Files.** **+ Add** opens *Upload file*. Limits: documents 15 MB, images 10 MB, audio 50 MB, video 100 MB. The agent reads text files, PDF and DOCX; they are given to it as reference material with the request. Excel, PowerPoint, `.doc`, EPUB and RTF files are refused: save them as PDF, DOCX, TXT, CSV or Markdown first. Images, audio and video are stored but not sent to the model, and show *Not readable by the agent*.
- **Tools.** **+ Add** opens a searchable list with All / Tools / MCP / Workflows tabs, taken from Integrations. Only Tools can be called today; MCP servers and workflows are shown disabled with the reason. Tools are only used by the *Function calling* strategy: if you pick another strategy the panel warns you and offers to switch.
- **Advanced settings.** Strategy, Query, Max iterations, Memory, and an **Env** editor (KEY / VALUE rows, **Import .env**). Use `{{env.KEY}}` in the prompt or query. Keys and values are saved together in one step, so the panel always matches what the node can use. Values are stored encrypted for this node, are never shown again after you save them (type a new value to replace one), and are never copied into templates or published flow versions. If you remove a key or delete the node while a published version still uses it, that version keeps working: the value is kept until no published version needs it.

**Remove** on a skill or file only takes it off this node. It stays in the agent's library, where you can attach it again or delete it. Delete is refused while a draft node still uses the asset; if a published version used it, it is hidden but kept so that version keeps running unchanged. Duplicating the agent copies its skills and files (not Env values). Each file has an **In templates** setting. The default, *Require upload on install*, copies nothing from the file: whoever installs the template uploads their own and the panel asks for it, and the agent can't be published until they do. *Embed content* copies the text into the template (only choose this for material that is safe to share with everyone who can use the template), and *Exclude* leaves it out. Skills are always included. If a node points at a skill or file that no longer exists, saving the template stops and tells you which node and which item.

## 5. Templates

**Templates** shows certified starting points — four Data Agent templates
(Analysis, Engineer, ERD Designer, Scientist) and two App Agent templates
(Conversational & Knowledge, Task-Automation), plus a blank canvas. Click
**Use template** to open it in the builder.

---

## 6. Operating agents

### Runs
**Runs** lists every execution with status, failure class (if any), hop
count, duration, and cost. Filter by status (All/Success/Failed/Denied).
Click a row for the full trace waterfall and principal chain for that run.

### Traces
**Traces** is the technical view — one row per OpenTelemetry-style trace,
filterable by agent. Click a row to see the span-by-span waterfall: which
step took how long, and where a failure happened (orchestration, harness, or
UCS denial — each color-coded).

### Monitoring
**Monitoring** shows the full metrics catalog: run-level (success rate,
cost, retries), harness-level (loop iterations, step-limit hits, guardrail
triggers), UCS/governance-level (entitlement pass rate, chain depth, cache
hit rate), cost & business, agent health, and a golden-test-set evaluations
table. Use the **Filter by agent** dropdown to scope everything to one agent
instead of the platform aggregate.

### Alerts
**Alerts** (Platform Admin / Security Admin) lists open and resolved alerts
— quality-breach, denied-burst, priv-escalation, and harness-level alerts
(step-limit, guardrail block). Click one to see full details and its
**History** — every acknowledge/resolve action is recorded with who did it
and when, using whichever persona you're currently viewing as.

- **Acknowledge** — logs that you've seen it; the alert stays open.
- **Resolve** — closes it out; moves it to the Resolved tab.

---

## 7. Integrations

**Integrations** has four tabs:

- **Model Providers** — shows which providers are using platform AI credits
  vs. need your own API key. Click **Add API Key** to configure — this opens
  the full authorization form (API key, organization, base URL, protocol).
  Saved keys register the provider into the workspace's model catalog.
- **Tools** — click any tool card to see its included actions; click an
  action to see its parameters (name, type, required/optional, description).
- **MCP** — connected MCP servers and their action counts. **Add MCP
  Server** opens a form with Server URL/Name/Identifier and an
  Authentication/Headers/Configurations tab set.
- **Data Sources** — external sources for Knowledge Retrieval nodes.
  **Configure** opens either an OAuth flow (Google Drive, OneDrive,
  SharePoint) or an API-key flow (Google Cloud Storage, Notion, GitHub),
  depending on the source.

---

## 8. Settings

**Settings** (Platform Admin) shows runtime plane health (Data/App), infra
connection status (Temporal, Kafka, Postgres+pgvector), and the model
catalog with entitlement status per model.

---

## 9. FAQ

- **"Where do I manage who can edit or run an agent?"** → Not here — that's
  UCS Catalog's RBAC & policy console. The Agent Profile's Entitlements tab
  shows bindings read-only and links out.
- **"How do I see why a run failed?"** → Runs or Traces → click the row →
  the span waterfall is color-coded by failure class (orchestration/
  harness/UCS denial).
- **"How do I know if an alert has already been looked at?"** → Open it —
  the History section shows every past action with who did it and when. On
  the list page, an "acknowledged" chip appears on open alerts that have
  been acked.
- **"Why is a tab in Agent Profile empty?"** → Either the agent hasn't been
  published yet (no runs to show), or — for Live jobs specifically — this
  environment doesn't have real execution wired up (see the note in §3).
- **"How do I add a node type that isn't in the palette?"** → That's a
  developer task, not something done from the UI — see the Developer Guide.
