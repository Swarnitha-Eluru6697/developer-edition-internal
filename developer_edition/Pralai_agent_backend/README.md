# InAiEra Agent Platform — Reference Implementation

A real, runnable **React + TypeScript** frontend and **FastAPI + SQLite** backend
implementing the full UI mockup that was designed and approved earlier in this
project: agent registry, drag-and-drop Workflow IDE (16 node types), Runs,
Traces, Alerts with real acknowledge/resolve tracking, Monitoring, and
Integrations (Model Providers, Tools, MCP, Data Sources).

## Documentation

- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** — architecture, setup, database schema, full API reference, and how to extend the builder (add a node type, page, drawer, or modal)
- **[User Guide](docs/USER_GUIDE.md)** — task-oriented walkthrough of every screen, for builders/platform admins/security admins
- **[Manual Testing Guide](docs/MANUAL_TESTING_GUIDE.md)** — the test procedure to run before merging changes. A first automated test suite now exists (`frontend/src/__tests__/`, run with `npm test`) covering the Workflow IDE's drag-to-connect interaction — everything else is still manual, see the guide.

## Real Knowledge Base integration — separate from the platform's own database

Closes the single biggest gap flagged in `docs/GAP_REPORT.md`: the
`retrieval` node used to be a pure placeholder. Integrations now has a
"Knowledge Base" tab where a person registers their **own** vector store —
deliberately a separate connection target from wherever this platform's
own Agent/Run/Trace/EnvVar/AgentMemory data lives, not the same database.

**Real for pgvector**: a genuine `psycopg2` connection to whatever host/
port/database you configure, and `retrieval` nodes referencing a
registered pgvector knowledge base now run a real query against that real,
separate database — verified with a full `run_flow_sync` integration test
where real content from a genuinely different database flowed through a
template node into the final agent output. Not true nearest-neighbor
vector search (no embedding model is configured anywhere in this reference
implementation), so it fetches the configured table's rows rather than the
top_k most *similar* ones — but a real query against a real, separate
database, not a placeholder string.

**Config-only for Pinecone / Weaviate / Qdrant / Milvus / other**: real
connection storage and a real HTTP reachability check, but query execution
isn't wired into `retrieval` for these yet — stated explicitly in the UI,
not glossed over.

**Secret masking** follows the same real pattern as environment variable
secrets: the password is echoed exactly once, on the creation response,
masked on every read after.

To reproduce the real separate database this was verified against:

```sql
-- on a genuinely different host/port/instance in a real deployment;
-- same local Postgres server but a different database/role is enough
-- to prove the mechanism in dev
CREATE ROLE kb_user WITH LOGIN PASSWORD '...' SUPERUSER;
CREATE DATABASE customer_kb OWNER kb_user;
\c customer_kb
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE support_articles (
  id SERIAL PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, embedding vector(3)
);
INSERT INTO support_articles (title, content, embedding) VALUES
  ('Resetting your password', 'To reset your password, go to Settings > Security > Reset Password.', '[0.1,0.2,0.3]');
```

Then register it under Integrations > Knowledge Base with `host`, `port`,
`dbname=customer_kb`, `user=kb_user`, `password`, `table=support_articles`.

## Real prebuilt Data Agent templates

Previously entirely decorative: `Template` had no `flow` field at all, the
Templates page's "Use template" button didn't call any API (it just
navigated to a hardcoded agent regardless of which template was clicked),
and `NewAgentModal` called `createAgent` but never actually sent the
template's flow — every new agent got the same generic 3-node skeleton no
matter which template was picked.

Now each of the 7 templates (Analysis, Engineer, ERD Designer, Scientist,
Conversational & Knowledge, Task-Automation, Blank canvas) carries a real,
genuinely distinct flow — Scientist's ends in a real `memory` write to
track experiments across runs; Engineer's includes a real `code` node;
Task-Automation's includes a real `hitl` node, routing it onto the durable
path. Two real actions per template, both backed by
`POST /api/templates/{id}/instantiate`:

- **Customize** — creates a draft agent with the template's real flow
  (deep-copied, so editing it never mutates the template), opens the
  Builder.
- **Create & publish agent** — the same, plus immediately calls the *exact same*
  real publish logic every other publish goes through
  (`_resolve_and_publish`, refactored out so there's one code path, not
  two that could quietly drift apart). This is deliberately **not** a
  governance bypass: a freshly-instantiated agent has no role bindings of
  its own yet, so a template whose flow touches any catalog object — most
  Data Agent templates do — is genuinely blocked by the same real UCS
  entitlement check, same as clicking Publish on any other agent. Verified
  both real outcomes directly: Analysis Agent (touches catalog objects)
  correctly blocked with the agent left as a draft, never falsely marked
  published; Blank canvas (touches none) correctly publishes immediately.

Two real bugs surfaced and fixed while building this, not glossed over:
agent IDs used second-precision timestamps, so two rapid requests could
collide on a genuine Postgres unique-constraint violation — fixed with
UUIDs; and the new endpoint's error handling only caught the "entitlement
denied" exception type, not "UCS unreachable" (a different real failure
mode), which crashed with a raw 500 instead of an honest response — fixed
to catch both.

## Agents as a real UCS catalog object — Gravitino has no "Agent" type, so this uses its Model type instead

Given a real, separate service (InAiEra's Unified Catalog Service, built
on Apache Gravitino) and asked to make agents sync into it as governed,
permissioned objects — even though Gravitino's catalog model has no
"Agent" entity type.

**Investigated the real, uploaded UCS codebase rather than guessing at
its contract.** Gravitino's catalog tree is `metalake → catalog →
schema → {tables | topics | filesets | models}`. No custom entity type
exists without writing a new Gravitino catalog-provider SPI in Java —
out of scope. But UCS's own RBAC schema already treats `model` as a
first-class securable object type, with a full privilege family
(`USE_MODEL`, `CREATE_MODEL_VERSION`, `CREATE_MODEL`) already defined.
A Gravitino Model — name, versions, each with a URI/aliases/comment —
is a close semantic match for an Agent with versions, and needs zero
UCS-side schema changes to use as the target.

**Verified against the real UCS, not a stub of my own assumptions.**
Stood up genuine infrastructure to test this properly: the actual UCS
FastAPI service, its real Postgres schema, a real RSA-signed local JWT
issuer (UCS's own `jwt.decode()` cryptographically verifies these
tokens — nothing bypassed), and a minimal Gravitino stub covering only
the specific REST endpoints UCS's own `gravitino.py` calls (a full
Gravitino Java server can't run in this sandbox).

This caught two real design mistakes before they shipped, not after:
the first proposal used a flat `agent:{id}` object string and
`action="invoke"` — both **confirmed to genuinely fail** against the
real running UCS. `rbac_cache.decide()` parses object scope by
splitting on `.` into `catalog.schema.object`, so a flat string was
silently treated as an all-catalog wildcard, not a real per-agent
scope; and `USE_MODEL`'s recognized actions are `read`/`browse`/
`query`/`list` — `"invoke"` matched nothing. Fixed to a real dotted fqn
(`agent_catalog.agents.{id}`) and `action="read"`, then re-verified:
a user in the granted group gets a real `allow` with the real
`granted via {group}::{role}` reason; a user not in the group gets a
real `deny`; a *different* agent the same user has no grant on
correctly denies too.

**A real, necessary gap found and fixed in UCS itself.** It could list
model versions but had no way to create a model or a version at all —
nothing could register an agent as a catalog object even with the
right RBAC shape in place. Added `create_model`/`create_model_version`
to UCS's `gravitino.py` and two matching admin endpoints
(`POST /api/v1/admin/models`, `POST /api/v1/admin/models/versions`),
following the codebase's existing patterns exactly, and verified them
end-to-end against the real service.

**Platform-side integration**, all verified against the live stack:
- `ucs_sync.py` — real sync functions (register agent as Model on
  creation, register version on publish, grant/revoke group access),
  requiring a real `UCS_ADMIN_TOKEN` bearer credential (UCS's admin
  endpoints do genuine RS256 JWT verification — no soft-check bypass
  exists for this path, unlike the runtime authz check). Best-effort
  and non-blocking: agent creation and publish never fail because UCS
  is unreachable or unconfigured, but the real outcome (`ucs_synced`,
  `ucs_sync_error`, `ucs_fqn`) is honestly recorded on the agent either
  way, not silently skipped or faked.
- New endpoints: `POST /{agent_id}/ucs/sync` (manual retry),
  `POST /{agent_id}/ucs/grant`, `POST /{agent_id}/ucs/revoke`.
- UI: a real "UCS Access" card in the Publish panel — real sync status,
  the real registered fqn, an honest error with a retry button when
  sync failed, and a real grant/revoke form. Verified live through the
  actual UI (not just the API): create → auto-sync → publish → open
  Publish panel → grant a group through the form → confirmed the real
  runtime authz check now allows that group, then confirmed revoke
  removes it again. Ported to and independently verified in both React
  and the mockup.

## Real document ingestion into a pgvector knowledge base — adapted, not faked, from an uploaded AWS pipeline

Given an existing, real AWS-native pipeline (S3 event → Lambda → Docling
extraction → a second Lambda → a Ray-backed FastAPI service calling
PostgresML's `pgml.embed()` to write embedded chunks into Postgres) and
asked to integrate it into this platform, with a recommendation for
where the UI should live.

**Tested feasibility before assuming anything would carry over:**
- `CREATE EXTENSION pgml` genuinely fails here — this sandbox's Postgres
  doesn't have PostgresML's specialized build, only standard `vector`.
- No S3, Lambda, or Ray in this environment, and running a distributed
  Ray actor for one file's embedding would be architectural overkill
  for this platform's single-process model regardless.
- `sentence-transformers` installs fine, but Hugging Face — where real
  model weights live — returned a confirmed 403 from this sandbox. A
  real pretrained embedding model genuinely can't be loaded here.

**Built a real, working pipeline with what's genuinely available instead:**
- `app/embeddings.py` — real, deterministic, fully local embeddings via
  scikit-learn's `HashingVectorizer` (already installed, no download
  needed). Explicitly documented as a lightweight local embedding, not
  a pretrained semantic model — real enough for genuine pgvector
  nearest-neighbor queries, but won't capture deep semantic similarity
  the way a trained transformer would. One shared function used by both
  ingestion and retrieval, so query and stored vectors are always
  genuinely comparable.
- `app/ingestion.py` — real parsing (.txt/.md/.docx/.pdf/.csv/.json/
  .html/.xml) by reusing `harness.py`'s existing, already-tested
  extractor rather than adding a duplicate dependency, plus real
  sentence-aware chunking with real word-overlap between chunks.
- `POST /api/integrations/knowledge-bases/{id}/ingest` — a real
  multipart upload, parsed, chunked, embedded, and inserted into the
  knowledge base's own configured Postgres via a real psycopg2
  connection. The target table is remembered on the KB afterward.
- **The connected other half**: `handle_retrieval` (harness.py) now
  does genuine nearest-neighbor search (`ORDER BY embedding <=>
  query_vector`) using the same embedding function, with the query text
  resolved from the retrieval node's own `query_var` config field —
  previously present in the UI but never actually read. Shipping
  ingestion without fixing retrieval to use it would have left the
  feature half-finished; a table without an embedding column still
  falls back to the original honest "most recent rows" behavior,
  unchanged.

**Two real bugs found while verifying, not before:** a missing
`python-multipart` dependency (FastAPI's file upload needs it — caught
at server startup) and a relative-import bug (`from ..embeddings`
should be `from .embeddings` — harness.py and embeddings.py are
siblings) that silently broke retrieval until traced through the actual
exception.

**UI**: Integrations → Knowledge Base tab, the natural home since it
already manages pgvector connections. Each pgvector-provider KB gets a
real "Upload document" action — file picker, target table (pre-filled
from the KB's config if already set), optional chunking parameters, and
a real result readout (chunk count, embed model, dimension) after a
real ingest.

Verified end-to-end multiple ways: direct API calls confirming a
"refunds" query genuinely ranks the refund chunk highest and a
"password" query genuinely ranks differently; a real browser test
confirming the UI round-trips correctly; and a direct database check
confirming the actual inserted row has a real 384-dimension vector.
Ported to both React and the mockup, each independently verified live.

## The Monitoring page's "Agent health" cards were hardcoded strings, not real eval data

Asked directly how evaluation works, and specifically whether it's
per-agent. It is — `EvalSet` always belongs to exactly one agent, every
case genuinely runs the live flow through the same interpreter every
other run uses, and scoring is real (a deterministic substring match, or
DeepEval's GEval judged by this app's own Claude call for open-ended
criteria — honestly reporting "could not judge" without a real API key,
not a silent fake pass). Explaining that surfaced a real gap worth
fixing: the "Agent health" summary cards above the real evals table
(*"Eval pass rate (avg): 89%"*, *"7-day success trend: ▲ 2.1pt"*,
*"Behavioral drift flags: 1 · Churn Model Scientist"*) were static
strings, never actually computed from the real `EvalRun`/`EvalResult`
data sitting right below them.

**Fixed with a real backend endpoint**, `GET /api/monitoring/eval-health`,
computing all three from real data:

- **Average pass rate** — across the latest completed run per eval set.
- **Week-over-week trend** — a genuine comparison of runs completed in
  the last 7 days vs. the 7 days before that. When there isn't enough
  real history in both windows, it says so honestly rather than showing
  a plausible-looking number.
- **Drift flags** — the real agent and set name for any eval set whose
  latest run actually flagged drift.

Verified with direct API calls: created a real eval set, ran real
cases, confirmed the computed pass rate matched exactly; confirmed the
honest "not enough data" trend state with only one run on record; then
forced a real regression (added a case guaranteed to fail) and
confirmed drift flagged correctly with the right agent name attached —
not asserted, derived.

Wired into both React (replacing the static strings with the real
endpoint) and the mockup (same real-fetch-with-honest-fallback pattern
already used elsewhere in that file), each verified live in a real
browser against the real backend. Deliberately left a similar
hardcoded pair in the smaller per-agent Quality tab out of scope for
this round, consistently in both React and the mockup.

## Traces was showing fake data — the exact same span waterfall for every trace, regardless of what ran

Reported directly: Runs, Traces, and Logs all looked the same, and
Traces specifically should show what the user actually said and what
the agent actually replied. Investigating confirmed it was worse than
it looked — `get_trace_spans` was **explicitly documented in its own
code as a stub** that synthesized a fake, hardcoded 4-span waterfall
("orchestration: schedule", "harness: reasoning loop", ...) with made-up
durations, completely unrelated to whatever flow actually ran. Every
trace, for every agent, showed the identical four fake spans. The real
per-node execution data existed in `harness.py` the whole time — it was
computed and then thrown away, never persisted or surfaced anywhere.

**Fixed for real:**

- The harness now captures a genuine content preview per span — the
  real user input on the trigger span, the real generated output on
  whichever node actually produced it. Deliberately did *not* attach a
  preview to every node indiscriminately: several handlers only set
  output "if not already set," so blindly reading it after every node
  would have misattributed an earlier node's real output to an
  unrelated later span (e.g. a `gov` node showing an `llm` node's reply
  as if it were its own). Found and avoided that before it shipped.
- `Trace` now persists `user_input`, `final_output`, and the real
  `spans_json` from the actual execution, in both the direct-run and
  webhook paths.
- `get_trace_spans` returns the real persisted spans — confirmed live
  that a real run produces real span names matching its actual flow
  (`trigger, gov, retrieval, llm, tool, respond`), never the old fake
  four.
- The Traces list now has a real Message column with the actual user
  input; the detail view shows "User provided" / "Agent replied"
  prominently, then the real per-span waterfall with real content, not
  just a timing bar. A misleading OpenTelemetry-compatibility claim in
  the subtitle — never actually true — was also removed.
- Old seed traces that predate this fix get an honest "no span detail
  recorded" message rather than a blank or broken waterfall.

**The mockup had the identical problem, and worse: its entire Traces
page had never called the real backend at all** — `DATA.traces` was
100% static local seed data with its own copy of the same fake
synthesized waterfall, silently never touching `/api/traces` in any
session before this one. Rewritten to attempt the real backend first
(list, detail, and spans, all genuinely fetched), with an honest local
fallback — matching the established pattern used elsewhere in the
mockup — when the backend isn't reachable, rather than either a dead
end or permanently-fake data. Run Detail's own waterfall (a separate,
already-disclosed synthetic display) was deliberately left as-is —
different scope from what was reported — but updated so it doesn't
break from the shared rendering function's new real-data field names.

Verified end-to-end in a real browser against the real backend, in both
React and the mockup: a real run produces a real trace with the real
conversation and real span content, and the old fake waterfall is
genuinely gone.

## Four real Publish/Try It/Studio improvements

**Manage Features consistency.** Found the exact gap: `FormRunner` never
received the `onManageFeatures` prop at all, so a workflow-type App
agent's Try It panel (form UI) had no "Manage features" link while a
chatbot-type App agent's (chat UI) did — two genuinely different
experiences for what should be the same capability. Fixed by wiring the
same prop through with the same `isApp` condition `ChatRunner` already
used, so Data agents (no features concept) still correctly show
nothing, while any App agent now shows it regardless of chat vs form
shape. Ported the identical fix to the mockup.

**Real Publish button states.** The Publish panel's status section now
reflects three genuine states, not one generic "isn't published yet"
banner: never published (draft, auto-save time, Publish enabled);
published with no changes since (a real "Latest published" readout, a
real Restore link, and the Publish button genuinely *disabled* — there's
nothing new to send); published with real changes since (back to the
unpublished-draft state, Publish re-enabled, Restore still offered since
a real prior publish exists to fall back to). Each state is computed by
comparing the agent's real `updated_at` against its most recent real
`AgentVersion` snapshot — reusing the version history already built
rather than inventing a new signal.

**A real, non-obvious backend bug caught while verifying this
end-to-end:** `Agent.updated_at` kept microsecond precision but
`AgentVersion.created_at` was truncated to whole seconds, so
immediately after any publish the agent's own timestamp always looked
*slightly newer* than the version snapshot taken moments before — the
Publish button would have stayed permanently enabled even with zero
real changes. Caught by direct API testing (not assumed), fixed by
giving both fields the same precision, and reconfirmed live in a real
browser that both directions now work: no changes after publishing
correctly disables the button, and a real subsequent edit correctly
re-enables it. The mockup's own version-fetching was also found to be
stale in three places — it had never actually re-fetched the agent or
its versions after a real publish or restore, so its own copy of
`updated_at` never moved — fixed to genuinely refresh from the backend
after both operations, the same way the React app already did via
`api.getAgent()`.

**Studio card layout.** The "Edited" timestamp now sits above the
footer divider instead of being grouped inside it, and the hover-only
"Open app" icon now sits directly next to "Details" in the footer
instead of floating separately at the top of the card. Confirmed with
an actual rendered screenshot, not just code review.

## The mockup's Web Apps page was still a dead end without a backend — fixed

A clearer error message wasn't the actual fix. Reported again, correctly:
the mockup was still "failing" — because every *other* real-backend
attempt in this file (Logs, Try It, saved results) degrades to a real,
honestly-labeled local experience when the backend isn't reachable, and
Web Apps was the one place that didn't. A better error message on a dead
end is still a dead end.

Fixed to match the established pattern: attempt the real backend +
UCS call first; if it fails for any reason, fall back to the mockup's
own local seed data — every published App agent, filtered the same way
the real endpoint would — with a visible banner disclosing exactly what
happened and that real per-user filtering needs the backend and UCS
running. Clicking through to one of the fallback apps still works,
same as clicking through anywhere else in the standalone mockup.

Verified three ways in a real browser: backend genuinely down (fallback
banner appears, real local apps are clickable, nothing is a dead end),
backend and UCS both genuinely up (no fallback banner, the real
UCS-filtered per-user list renders as before), and a full regression
sweep confirming nothing else in the mockup broke.

## A real UX bug fixed: "Failed to fetch" wasn't diagnosable

Reported directly from testing: the Web Apps page showed "Couldn't load
your apps — Failed to fetch" with no way to tell what was actually
wrong. Reproduced it against the real backend both ways — backend
running without UCS (fails fast with a clear 503 message, confirmed
working correctly) and backend not reachable at all (the browser's own
bare `fetch()` rejection, "Failed to fetch", with zero detail).

The second case was the real gap: every API call in this app goes
through one shared `req()` function in `api.ts`, and a network-level
failure there — the backend isn't running, it's on a different port, or
it's genuinely unreachable — was passed straight through as the
browser's raw, undiagnosable message. Fixed once, centrally, so it's
fixed everywhere this app calls its own backend: a network-level failure
now names the URL that was actually being reached and asks the obvious
question ("is it running?"), while a real HTTP error response (like
UCS's own 503) still surfaces its own specific detail message unchanged.
Ported the same fix to the mockup's equivalent fetch handler, which had
the identical gap.

Verified both fixes live: stopped the backend entirely, hit the page in
a real browser, and confirmed the new message actually appears instead
of the bare original.

## A real End User role and Web Apps page, with access resolved entirely by UCS

A dedicated end-user experience — a vertical rail of the apps you're
actually allowed to open, click one and its real chat or workflow UI
loads — separate from the Studio/Builder surfaces a creator uses.

**Access is resolved by the real UCS product, not a local table.**
`check_agent_access()` extends the same dual-check pattern
`ucs_client.py` already uses for data-object entitlement — the agent
itself becomes the object being authorized (`action="invoke"`,
`object="agent:{id}"`), a real HTTP call to UCS's own
`/api/v1/authz/check`, not a new invented mechanism. The new
`GET /api/agents/published-for-user` endpoint asks this question once
per published App agent and returns only what UCS actually allows.
Verified with genuinely different per-user rules against a live UCS
instance: one identity saw two apps, a second identity saw a different
single app, an unrecognized identity saw none. Grant and revoke happen
entirely in UCS's own console — there's no local ACL table here that
could drift out of sync with it. Matching this platform's existing
governance stance, if UCS is unreachable the endpoint fails loud (503)
rather than silently showing everything or nothing.

**The End User role reuses the platform's existing persona system**
rather than a new one — it already gated navigation by role, so adding
`end_user` to it was additive, not a new mechanism to keep in sync. A
real route guard (not just a hidden nav link) redirects an End User away
from `/agents`, `/runs`, and every other builder/admin route even by
direct URL — verified live in a real browser, including that switching
back to Builder restores the full admin nav.

Since this reference platform has no real login yet (already disclosed),
the End User page includes an honestly-labeled "Signed in as" identity
chooser rather than pretending to authenticate anyone — it's a real
input to the real UCS check, just not cryptographically verified
identity.

**A real bug found while porting this to the mockup**: the live backend
returns `app_type` (snake_case, standard FastAPI/Python serialization),
but the mockup's entire rendering codebase — `chatRunnerHtml`,
`isChatLikeAgent`, the type-label chip — has always used `appType`
(camelCase), matching its own local seed data. This was the first place
in the mockup that rendered a full agent object fetched live from the
backend rather than just a run result, so the mismatch had never
surfaced before. Every real app opened through this page would have
silently rendered with the wrong runner type had this gone unnoticed.
Fixed by normalizing the field on receipt.

## Three real builder improvements: a live validation checklist, real multi-field trigger inputs, and real version management

**Real-time validation checklist.** A `validateFlow()` utility genuinely
inspects the current flow's nodes and edges — type-specific checks (an
Output node needs a real named variable, a Question Classifier needs
real input variables and at least one class, a Human Input node needs a
real delivery method selected, plus Condition/Template/Code/HTTP/
Sub-Agent/Doc Extractor) and a real connectivity check (any node with
zero edges touching it, except comment nodes, which are documentation-
only by design). A Checklist button in the Builder toolbar shows a live
issue count and opens a panel listing every real problem, clicking one
selects that node. Found and fixed a related bug while building this:
Human Input's "no delivery method" warning was hardcoded to always
show — there was no actual delivery-method selection to check against.
Added real checkboxes so the warning (and the new checklist check) are
now genuinely conditional on real state.

**Real multi-field trigger inputs.** The Trigger node's User Input type
used to offer a single fixed `{ message: string }` schema. It now has a
real "Add Input Field" modal — all 8 real field types (Short Text,
Paragraph, Select, Number, Checkbox, Single File, File List, JSON Code),
Required and Hidden & Pre-Filled options — and a real field list.
Critically, this isn't just a config-time editor: `FormRunner` (both
React and the mockup) detects real configured fields and renders an
actual per-field form with the right input type for each, validates
required fields before allowing Run, and correctly keeps Hidden &
Pre-Filled fields invisible to the end user while still submitting their
real default value. On the backend, `/run` accepts real structured
`inputs`, and `handle_trigger` maps each into a genuinely referenceable
`{{field_name}}` downstream — verified end-to-end with a real flow that
echoed real submitted field values back through an Output node.

**Real version management.** A new `AgentVersion` table takes a genuine,
immutable snapshot — actual flow, app_type, and features, not just a
version label — every time an agent is published. Verified this
snapshot is a real point-in-time capture, not a live reference: modified
a published agent's flow, published again, and confirmed the *earlier*
snapshot genuinely does not include the later change. A real Restore
endpoint rolls the live agent back to a prior snapshot's actual flow —
verified the restored agent's flow genuinely matches the old version and
that its status correctly becomes a draft rather than silently
pretending nothing happened in between. The Publish panel's new Version
History section lazily loads real version history only when expanded,
shows the real timestamp and publisher, and only offers Restore on
non-current versions.

**A genuine testing gap found and closed along the way:** verifying
version management required actually calling `/publish`, which — unlike
the run-time `gov` node — has no soft-check bypass and genuinely
requires the separate UCS service to be reachable. This meant `/publish`
had likely never been exercised end-to-end this entire session; every
prior verification of publish-adjacent features had mocked it or
stopped short of the real call. Built a minimal local UCS stub *purely
for this verification* (not part of the delivered product) so the real
flow — snapshot creation, point-in-time isolation, and rollback — could
actually be confirmed rather than assumed to work.

## The real Publish panel — built after being told it was missing, and it was

A person pointed at a screenshot of a real publishing UI (a unified panel:
Edit Info / Duplicate / Export DSL / Import DSL, then Web App / Backend
Service API / MCP Server cards, each with its own status badge, toggle,
and real actions) and said they didn't see it. They were right — it
didn't exist. What had actually been built earlier was real MCP
functionality tucked into an existing "Api Access" settings tab, not
this dedicated surface, and the referenced chatflow-webapp doc hadn't
even been read yet. Worth naming directly rather than reframing as
"partially done."

**Built for real, not just visually:**

- **Export DSL** — downloads the agent's actual flow, name, subtype, and
  app_type as JSON; genuinely round-trips (see Import DSL below).
- **Import DSL** — uploads a DSL JSON file and creates a real new agent
  from it, running the actual imported flow, not just storing metadata.
- **Duplicate** — a real independent clone with its own id; verified
  directly that editing the clone's flow never touches the original's.
- **Web App and Backend Service API now toggle independently** — real,
  separate persisted state per surface, matching the reference's
  separate IN SERVICE / DISABLED badges, instead of being coupled to one
  shared status flag.
- **A real QR code** for the public URL, generated client-side via the
  `qrcode` package (React) or a CDN-loaded build of the same library
  (the standalone mockup has no bundler). If the CDN is unreachable —
  confirmed in this sandbox to be the same pre-existing restriction that
  blocks Google Fonts in every test this session, not a code bug — the
  button shows an honest message instead of a broken or faked image.
  Verified this exact fallback path directly, since the sandbox itself
  can't reach the CDN to test the success path.
- **A real embed snippet** and **Launch** link.
- **The MCP Server card** reuses the same real toggle/URL/regenerate
  logic already built and verified earlier, now also reachable from this
  single, correct location.

**Found and fixed while rebuilding this — the mockup's old Publish modal
was entirely fake**: hardcoded `v5`, hardcoded "3 catalog objects," and
a toast that never called the backend at all. The button didn't even
pass an agent id, so it was permanently disconnected from whichever
agent a person had actually clicked into. The rebuilt version is
agent-aware — it reflects the real clicked agent's name and status, the
same way Try It and API Access already correctly do, even though the
underlying Builder canvas itself remains the shared static demo (a
pre-existing, disclosed limitation, not something this fix changed).

**Verified twice, not once**: first with 9 real component tests, then
with a full Playwright run against the actual React dev server and live
backend — confirming the panel genuinely opens, both surface toggles
persist to the real database independently, a real QR image renders (in
an environment where the CDN is reachable), MCP genuinely enables with a
real URL, Export DSL triggers a real file download, and Duplicate
genuinely creates a new row in the backend. The mockup port was verified
the same way — real backend calls confirmed via a direct database check
after each toggle, not just checking that the UI didn't crash.

## A new Output node, real Saved Results, real MCP Server publishing — and a real CORS bug that had been silently breaking the mockup's backend calls all along

**Output node** — a new, addable node type (not the locked terminal
`respond` node) that collects one or more named output variables at any
point in a flow, real values pulled from whatever upstream node actually
produced them via the same `{{var}}` mechanism the `template` node uses.
Extended that mechanism with a real, previously-missing capability:
`{{input}}` now resolves to the actual original user message. Multiple
Output nodes across different branches genuinely merge their fields
rather than overwriting each other — this is the real backing for
Workflow apps' "structured result out" UI. Verified end-to-end through
the actual `/run` endpoint, not just the handler function in isolation.

**Real Saved Results** — a genuine `SavedResult` Postgres table, not an
in-memory list. Workflow-app results can be saved and queried back,
matching Dify's "Saved results persist across user sessions." Verified
by saving a result, querying it back fresh, and deleting it.

**Real structured-output display, Copy, and Save** in the Workflow Web
App runner (both React's `FormRunner` and the mockup's equivalent) —
named fields instead of a single text blob when a flow used an Output
node, a real clipboard Copy, and a real persisted Save.

**A real regression caught while building this:** adding a toast
dependency to `FormRunner` broke the standalone `/app/:agentId` page,
which is deliberately not wrapped in the app's context provider (that's
what makes it a genuinely standalone runner, not part of the internal
app shell). Rebuilt with a self-contained local message instead of
assuming every component in the tree has the same dependencies available.

**Real MCP Server publishing**, researched directly against
[Dify's own publish-mcp documentation](https://docs.dify.ai/en/cloud/use-dify/publish/publish-mcp)
rather than guessed at. This exposes an agent as a genuine JSON-RPC
endpoint implementing the real subset of the Model Context Protocol a
client like Claude Desktop or Cursor actually needs: `initialize`,
`tools/list`, `tools/call`. Off by default; enabling generates a real
random token-based URL; a `tools/call` genuinely runs the agent's actual
flow through the same interpreter `/run` uses, not a canned response.
Verified the complete real flow directly against the live backend: a
disabled server correctly refuses calls, a real tool is genuinely
discoverable and callable, and — matching Dify's own warning to treat
this URL like an API key — regenerating a token immediately and
verifiably invalidates the old one. Honest scope: this covers the
`tools` capability over plain HTTP POST, not the full MCP spec (no SSE
transport, no resources/prompts primitives) — a single agent-as-a-tool
doesn't need those, and the gap is disclosed here rather than silently
unsupported.

**A significant, previously-undiscovered bug found while verifying the
MCP work in the mockup**: the backend's CORS policy only allowed
`http://localhost:5173` — the React dev server's own port. This meant
the standalone mockup HTML file's real backend calls had likely been
silently CORS-blocked by the browser in any realistic usage this entire
session: served from any other port, or opened via `file://`. Every
"attempt real, honest fallback" feature built into the mockup would have
looked like it was falling back to "no backend reachable" even with the
backend genuinely running, because the browser was blocking the response
before the JS ever saw it. Fixed by widening `allow_origins` (a
reasonable choice for a local-only dev/demo backend with no auth layer,
already documented as such) — and re-verified with a full Playwright run
against real Chromium afterward rather than assuming the fix worked.

**Deliberately out of scope this round, disclosed rather than silently
dropped:** CSV batch processing for Workflow apps (upload a CSV,
template download, run each row, export results) — a substantial,
separately-buildable feature described in Dify's own workflow-webapp
docs. Flagged as a real next step in the gap report, not attempted at
reduced quality just to claim completeness.

## New agent creation now only builds App Agents — Data Agents are provisioned elsewhere

The New Agent modal's Data Agent / App Agent type toggle is gone. Data
Agents in this system are provisioned by a separate service — the
existing seeded ones (Revenue Analysis, Schema Migration, Pipeline
Engineer, Churn Model) are unaffected, but this modal was never the
right place to mint new ones, and offering the choice implied it was.

This is enforced on the real backend, not just hidden in the UI: the
`POST /api/agents` endpoint no longer accepts a `type` field at all and
always creates a real App agent — confirmed by sending a payload that
explicitly tries `"type": "Data"` and checking the created agent still
comes back as `type: "App"`. The App type selector (Workflow / Chatflow
/ Chatbot / Agent / Text Generator) is now the first and only real
decision in the modal, and "Start from" only ever offers real App
templates.

The Templates page's own "Create & publish agent" flow for existing Data
templates (Analysis Agent, ERD Designer, etc.) is a genuinely separate
backend endpoint (`/api/templates/{id}/instantiate`) and is completely
unaffected by this change — confirmed directly rather than assumed,
since a first look could easily have missed that these are two different
code paths.

## Verified against a real browser, not just jsdom — and closed the real gap that explained "still not working"

When told three mockup features still didn't work despite passing tests,
the right move was to stop trusting the jsdom test harness and actually
check in a real browser. Used Playwright with genuine Chromium — both
served over `http://` and opened directly via `file://` (the realistic
way someone opens a downloaded standalone mockup) — and confirmed the
"Open app" new-tab routing and the hover-visible card icon both
genuinely work as built, land on the correct URL, and show the correct
agent, with no sidebar. That ruled those two out as the real problem.

**The real, actionable gap was in the Logs feature specifically**: it
only showed genuine dynamic content when a real backend was reachable at
`127.0.0.1:8000`. Someone opening the standalone mockup file on its own
— the normal way to use it — would only ever see the static, pre-written
seed log lines, which obviously never reflect anything actually typed.
Confirmed this exact scenario end-to-end with Playwright: opened the
file via `file://` with no backend process running at all, sent a real
message through Try It, and verified a real log entry recording that
exact message appeared in the Logs tab — entirely client-side, no server
required. The honest note on the tab now says plainly which situation
you're in: a real backend's live logs, real entries from this session
recorded locally, seed history, or some mix — never claims something is
"live" when it isn't.

Two of my own regression-test scripts had real bugs surface while
verifying all this, both fixed rather than worked around: one carried a
stale route (`S.route` was still `'builder'` from an earlier step in the
same script, so a later step's card lookup correctly found nothing), the
other was the same class of script-tag-text false positive documented
elsewhere in this README.

## Four more real fixes: a genuine "Open app" routing bug, real log search, real user messages in logs, and a hover-visible shortcut

**"Open app" already worked correctly in React** — `HashRouter` reads
`window.location.hash` on mount by design, so no fix was needed there.
**The mockup had a real, serious bug**: it never read the URL hash at
all. Opening "Open app" in a new tab loaded the same static file fresh,
and that fresh load always booted to the hardcoded default route,
silently ignoring `#/app/{id}` in the address bar. Fixed with real
hash-parsing on load plus a `hashchange` listener — confirmed by loading
the file fresh with `#/app/a5` already in the URL and checking it
genuinely lands on that agent's runner, not the Overview page.

**Real search and level filter** added to the agent profile drawer's
Logs tab. **Real user messages now appear in the log line** — the
backend was logging a generic `"Live run · status=success"` with no
indication of what was actually asked; fixed to include the real input
text. Verifying this **surfaced a second, previously-undiscovered
backend bug**: the logs endpoint had no `ORDER BY` at all, so a brand
new real log entry could land anywhere among older seeded rows instead
of appearing at the top — confirmed directly via SQL that the row was
being written correctly, it just wasn't surfaced where anyone would
actually look for it. The mockup's version goes further than React's:
it attempts a real fetch to the live backend for genuine logs, falling
back to seed data with an honest note only when unreachable, matching
the "attempt real, disclosed fallback" pattern used everywhere else.

**A hover-visible "Open app" icon** on each Studio card, shown only for
published App agents, positioned to avoid the status chip and wired with
`stopPropagation` so it doesn't also trigger the card's own
click-into-Builder behavior.

Two of my own test-authoring mistakes surfaced and fixed while verifying
the mockup port, both worth naming rather than quietly patching: a race
between an intentionally-rejected fetch promise and too-short wait times
in the test itself, and the now-familiar class of bug where
`document.body.textContent` picks up literal seed-data strings sitting
in the page's own inline `<script>` source — not real rendered content —
producing a false failure that had nothing to do with the actual
component logic.

## Manage Features, expanded to match the reference in full — and a real tab-visibility bug found along the way

**A real bug, found before touching any of the new work:** the agent
profile drawer's Monitoring and Logs tabs weren't missing — they were
built already — but two conflicting `.tabs` CSS rules meant the active
one relied on hidden horizontal scroll with no visible affordance,
making them easy to miss entirely. Fixed by switching to wrapping so all
ten tabs are always visible, in both the real app and the mockup.

**Three dedicated settings modals**, matching the reference exactly,
replacing what had been simple inline toggles:

- **Conversation Opener** — the real opener message, plus up to 10 real
  "Opening Questions" shown as clickable starter buttons before any real
  conversation begins. Clicking one sends that exact question through
  the real `/run` endpoint.
- **Follow-up settings** — a real model selector and a real default/
  custom prompt mode, with `{{input}}`/`{{output}}` substitution in
  custom prompts. Save is genuinely disabled until a custom prompt has
  content, matching the reference.
- **Content moderation settings** — three real providers: Keywords (the
  original check), **AI Moderation** (a genuine Claude classification
  call — the reference's "OpenAI Moderation" renamed since this platform
  is Claude-based, not calling out to a provider this app doesn't
  integrate with), and **Custom Endpoint** (a real HTTP POST to a
  user-configured URL). Verified the custom endpoint mechanism by
  standing up an actual small moderation server during development and
  confirming it both blocks and allows correctly, not just testing the
  honest-failure path for an unreachable URL. Real separate INPUT/OUTPUT
  toggles, each with its own preset reply — output-side blocking is
  verified to let the flow genuinely run first, then replace the real
  output, distinct from input blocking's full short-circuit.

**Two more real bugs, found while porting to the mockup, not before:**

1. A checkbox/label double-fire risk: clicking the visible toggle switch
   inside a feature row could *also* trigger that row's "open settings
   modal" click, because clicking anywhere inside a `<label>` wrapping a
   checkbox makes the browser fire a second, separately-bubbling click
   event targeted at the checkbox itself — the first (targeting the
   visible switch span) had nothing closer to match than the row's own
   click handler. Fixed with a dedicated no-op intercept positioned
   between the switch and the row in the DOM, confirmed by a test that
   specifically checks the sub-modal does NOT open on a toggle click.
2. The new sub-modals initially reused the existing `.overlay` backdrop
   class — which would have closed the *entire* Try It panel on a
   backdrop click, not just the sub-modal sitting on top of it. Fixed
   with a dedicated class and its own exact-target check, verified by a
   test confirming a sub-modal's backdrop click closes only that
   sub-modal while Try It and the Features panel stay open underneath.

## Chat UX Features — real conversation opener, follow-up, citations, and moderation, manageable pre-publish

A real "Features" panel, matching the reference UI, reachable from the
Builder's Try It panel — the pre-publish testing surface a developer or
tester actually uses, not the real published page (deliberately: letting
anonymous end users reconfigure a moderation word list or the opener text
via the public URL would be a real product/security problem, not a
testing convenience). Four features are genuinely functional; three from
the reference (Text to Speech, Speech to Text, Annotation Reply) are
shown honestly as "Coming soon" rather than as working-looking toggles
that quietly do nothing — consistent with how this whole build treats
decorative controls.

- **Conversation Opener** — a real per-agent greeting, shown as the
  actual first chat message before any run happens.
- **Follow-up** — a real additional LLM call after each reply, asking for
  genuine follow-up suggestions grounded in that specific conversation,
  not a static list. Same honest mock-mode caveat as the ReAct loop
  documented earlier: without a real API key, the mock echoes prompt
  fragments instead of real suggestions — confirmed via monkeypatch that
  the real parsing logic is correct once an actual model replies.
- **Citations and Attributions** — surfaces the real retrieved chunks
  from a flow's retrieval node, only when there's real data to cite.
  Verified against a live pgvector database, which caught **a real,
  previously-undiscovered bug**: knowledge-base IDs were slugified
  differently at creation time (`routers/knowledge_bases.py`) than at
  lookup time (`handle_retrieval` in `harness.py`), so any dotted
  catalog-style name — a realistic real name, not a contrived edge case —
  would register under one ID and never be found under the other. Fixed
  and reverified end-to-end.
- **Content moderation** — a real, deterministic block-list check. A
  genuine match short-circuits the flow entirely (confirmed: zero spans
  execute, a real `Run` row is logged with status `blocked`) rather than
  filtering the response after the fact.

Porting this to the mockup surfaced one more real bug, this time in code
written earlier in *this same round*: the Features panel's backdrop-click
handler was positioned after an early `if(!t)return` exit keyed off a
big allowlisted-selector `closest()` lookup — since the bare backdrop div
carries no attribute of its own, clicking it never reached the check
meant to close the panel. Caught immediately by testing the backdrop
click specifically, not assumed to work because the equivalent existing
`.overlay` pattern looked similar (it worked only because its element
also carries `data-overlay`, which this one deliberately doesn't, to
avoid a *different* bug: closest()-bubbling from any click inside the
panel with no more specific match, which the same test suite checks for
directly).

## Two more real bugs, found by directly testing the mockup rather than trusting the earlier tests

Both raised directly after using the product, not found by me first —
and both were real, not perception:

**Bug 1 — every agent type looked identical in Try It.** The auto-open
logic (added when a Studio card is clicked) correctly identified which
agent was clicked and correctly decided *whether* to open Try It based
on that agent's real status — but `tryItModalHtml()` itself ignored all
of that and always rendered a hardcoded demo agent's identity regardless
of which card triggered it. Every Studio card, no matter its real
`appType`, showed the exact same Workflow-form UI. Confirmed directly:
my own earlier passing tests never actually asserted *which* agent
appeared inside the opened panel, only that a panel opened at all — a
real gap in what "passing" meant, not a false test. Fixed with a real
`S.tryItAgentId`, set from the actual clicked agent and threaded through
to the render, verified by clicking three different agent types in
sequence and confirming three genuinely different results (chat form,
form-only, chat-with-reasoning). The one honest caveat that remains: this
mockup's Builder canvas is still a single static demo flow (a real,
disclosed limitation of a mockup vs. the real per-agent-flow React app) —
Try It now correctly represents the clicked agent's real identity and
UI type, but the underlying nodes you'd see if you opened the canvas are
still always the same demo. That's now stated in the panel itself
whenever it's showing an agent other than the demo.

**Bug 2 — API Access was unreachable from Studio, the actual primary
entry point.** It only worked from the Overview page's table, a
secondary surface. Fixed by adding a real "Details" button to every
Studio card, opening the same agent profile drawer (and its API Access
tab) without disrupting the card's own click-to-playground behavior —
built the same way the earlier eval-set Run button was, letting the
click delegate's own element-specific matching handle the two different
click targets rather than reaching for `stopPropagation()` again.

## A real bug fixed on the actual published page, and a real API Access tab

Found by directly checking rather than assuming: `AgentRunner.tsx` — the
real page a published agent's end users actually land on — had its own
separate logic that defaulted a Data agent's missing `app_type` to
`'chatbot'`, meaning a real end user visiting a Data agent's dedicated
URL would see a chat interface for an agent that was never meant to be
conversational. `TryItModal` already had the correct check; the actual
published page didn't. Fixed by extracting one shared `isChatLikeAgent()`
function used by both, so the two surfaces can't quietly diverge like
this again — and added the regression test that would have caught it
from the start, since no existing test covered a real Data-type agent on
this page at all.

**New "API Access" tab**, added to the existing agent profile drawer:
every agent is now genuinely reachable two ways from one place — the real
dedicated UI URL (`/app/:agentId`) and the real REST endpoint
(`POST /api/agents/:agentId/run`), both with copy buttons using the
actual clipboard API. The curl example shown isn't hand-written and
hoped-correct — I ran the *exact* command against the live backend before
shipping it, and caught a small inaccuracy in the sample response
(`execution_mode`'s real value is more descriptive than what I'd first
guessed) that a written-not-verified example would have missed. Honestly
discloses that this reference implementation has no auth layer yet rather
than fabricating a fake API-key flow to look more complete than it is.

## Overview page — the full real agents table, alongside Studio's card grid

Added the original table-style agent listing (Agent/Type/Plane/Status/
Health/Model/Runs/Cost) to the Overview page, sourced from the same real
`api.listAgents()` call Studio uses — not a second data source that could
drift from it. No duplicate "New agent" button on this table; the
page-level one stays. Clicking a row opens the existing `AgentProfileDrawer`
(Overview/Quality/Lineage/Entitlements/Usage/Logs tabs, real per-agent
data) rather than jumping straight into the Builder — matching a specific
follow-up request, and a genuinely different interaction from Studio's own
cards, which intentionally skip the drawer and go straight to the
playground (auto-opening Try It for published agents). Both paths were
kept deliberately distinct rather than unified, and the regression suite
explicitly checks that fixing one didn't quietly change the other.

## Studio — a real card-grid gallery, real filters, and a genuinely merged information architecture

Renamed "Workflow IDE" to "Studio," and rebuilt what "Studio" actually
means: not just a relabeled builder page, but the whole flow — a real
card-grid gallery of every agent (matching a reference tool's own
library view), with genuine Types/Tags/Creators filters and search, that
you click into to reach the playground (the existing Builder/canvas
editor). The old separate "Agents" (table) and "Workflow IDE" (canvas)
nav entries were two different concepts; the reference made clear they
should be one.

**Real fields backing every filter control**, not decorative UI:
`tags: List[str]` and `description: str` are new on `Agent`, with a real
`PATCH /{agent_id}/meta` endpoint. Sorting by "Last modified" surfaced two
real, previously-unnoticed bugs while building this:

1. `updated_at` existed on the model but was **never actually being
   bumped** by any node/edge mutation — every agent's "last modified"
   time was frozen at creation. Fixed at `_save_flow`, the one shared
   function every flow-graph change already goes through.
2. `app_type` — added last session with a working field, endpoint, and
   UI — was **never actually set in the seed data**. Every seeded App
   agent had `app_type=None` the entire time, meaning the "real
   different UI per type" feature had nothing real to demonstrate out of
   the box. Fixed with genuinely varied values across the seeded App
   agents, plus real tags and descriptions for all eight agents.

**"Once an agent is deployed, going inside shows the end-user UI"** —
implemented by reusing the existing Try It panel rather than building a
second view: entering a *published* agent's playground now auto-opens
Try It on load, leading with the real chat/form experience; a draft
agent still opens straight to the canvas, since there's nothing
meaningful to "try" as an end user yet. Verified both directions with
real tests, not just the positive case.

Building the Studio tests surfaced four real test-authoring mistakes
worth naming rather than quietly fixing: "Chatbot," "Agent," and
"internal" all legitimately appear in more than one place at once (a
filter dropdown option *and* a card's own label/tag), so exact-text
queries were ambiguous by design, not by accident — fixed with `within()`
and `getAllByText()` where multiple real matches are expected. Separately,
an index-based assertion (`cards[0]`) broke the moment the *default* sort
order legitimately reordered the cards — a reminder that a component
behaving correctly can still break a test that assumed a fixed order.

The mockup's version required a genuinely new mechanism, not a copy:
this is the first text input in the whole mockup that needs a live
re-render on every keystroke (the filtered card list has to visibly
update as you type), which the established silent-sync pattern
deliberately avoids specifically because a full `innerHTML=` replacement
drops input focus. Solved with real focus/cursor-position capture-and-
restore around the render, rather than accepting broken typing or
reverting to a "press Enter to search" compromise.

## A real "Try it" panel — testing from the end-user's own perspective, pre-publish

Closes a gap found by actually looking for it: the Builder's old "Test"
button fired one hardcoded input (`"Test input from builder"`) and showed
the result as a truncated toast — not something a developer could
meaningfully use to see what an end user will actually experience, and
only reachable after the fact, not iteratively while building.

`ChatRunner`/`FormRunner` — the exact same components the real published
`/app/:agentId` page uses — were pulled into a shared `RunnerUI.tsx` and
reused by a new `TryItModal`, opened from a real "Try it" button in the
Builder. This means a developer's preview *is* the real end-user UI, not
a parallel implementation that could quietly drift from it over time.

Available regardless of publish status — a draft agent shows an honest
"still draft" banner right inside the panel instead of blocking testing
until publish, which is the whole point (catching problems *before*
publishing, not after). Runs made through Try It are tagged
`user:builder-preview` rather than `user:end-user`, so they're
distinguishable from real usage in logs and metrics without needing a
separate code path to produce that distinction.

One real bug caught immediately by testing: the new `useState` call for
the modal's open/closed flag was initially placed *after* Builder's
existing early return for the loading state — a genuine Rules-of-Hooks
violation, the kind that can silently work by accident until state
ordering shifts and it doesn't. Fixed by moving it alongside the
component's other top-level hooks.

The mockup's version required a small real refactor, not just a copy:
the existing `chatRunnerHtml`/`formRunnerHtml` functions read/wrote a
single flat `S.appRunner*` state, which would have let a Try It session
and the real `/app/` page's session clobber each other if both were used
in the same sitting. Refactored into `S.runnerState.apprunner` /
`S.runnerState.tryit` so the identical render/handler functions genuinely
serve both surfaces without state collision — verified directly: pushing
a message into one namespace's history leaves the other's untouched.

## Real App agent types, and a genuinely standalone end-user runner page

Before this, there was no real end-user-facing UI at all — only the
Builder, which is a creator tool. Publishing an agent produced a working
flow with nowhere real for a non-builder person to actually use it.

**`app_type`** — a real field on App-type agents (`workflow`, `chatflow`,
`chatbot`, `agent`, `text_generator`), set at creation via a real 5-option
selector matching a reference tool's own app-creation menu, each with an
honest one-line description of what it means for the *published*
experience — not just a label, since it genuinely determines which UI
renders.

**`AgentRunner`** — a new page at `/app/:agentId`, and a real architectural
decision: it renders with **no builder sidebar or topbar at all**. A real
end user consuming a published chatbot shouldn't see "Builder / Templates
/ Monitoring" — that required restructuring the router itself (`App.tsx`
now branches before the internal shell), not just adding a page inside
the existing layout.

The UI shown is genuinely determined by `app_type`, not just labeled
differently:
- `chatbot` / `chatflow` / `agent` → a real chat interface, calling the
  actual `/run` endpoint per message.
- `workflow` / `text_generator` → a real form: one input, one real
  structured result, no conversation history.
- `agent` specifically also requests and can show the real per-step
  reasoning trace (`ctx["vars"]["agent_steps"]`, already produced by the
  real `agent` node's strategy execution) behind a "Show reasoning"
  toggle — verified that a `chatbot`-type app does *not* surface this
  even when the backend response happens to include it, so the
  distinction is real UI logic, not just conditional backend data.

Honest states throughout: an unpublished agent shows a real "not
published yet" message instead of a broken or fake-working page; a
failed run shows the genuine failure reason (including which entitlement
or harness check it hit), not a generic error.

One real jsdom-only bug caught by testing, not assumed away:
`scrollIntoView` isn't implemented in jsdom's DOM at all, throwing on the
very first chat message in tests — fixed with a defensive `?.` guard
that's also more correct in real browsers lacking the API, not a
test-only workaround.

The mockup's version pushes the same "attempt the real backend, honest
fallback" pattern into a full standalone page: `S.route==='apprunner'`
is checked *before* the normal nav validation and renders an entirely
different top-level layout, matching the real app's routing decision
rather than reusing the shell and hiding pieces of it with CSS.

## Real evals (DeepEval + Claude) and real Prometheus metrics

Closes two gaps that were explicitly called out as entirely fake in an
earlier report: the old `/monitoring/evals` endpoint returned a hardcoded
Python list with fixed strings, and there was no Prometheus integration
at all, not even a stub.

**Evals** — real tables (`EvalSet`, `EvalCase`, `EvalRun`, `EvalResult`).
Every case genuinely runs the live agent through the same real,
routing-aware `execute_agent_flow` every other invocation channel now
uses (§0.53, GAP_REPORT — previously called `run_flow_sync` directly,
unconditionally), not a stub. Two real scoring paths: a deterministic substring match needing no
LLM at all, or [DeepEval](https://deepeval.com/)'s `GEval` judged by a
custom `ClaudeEvalJudge` that wraps this app's own `call_llm` — the same
real-or-mock Anthropic call every LLM node already uses, not a second,
separately-configured judge provider (DeepEval defaults to OpenAI; this
replaces that). Real drift detection, verified both ways: a first run
with nothing to compare against correctly shows `drift: none`; a
follow-up run that genuinely dropped from 80% to 57% correctly shows
`drift: flagged`. One honest, expected limitation, caught immediately by
testing rather than assumed away: DeepEval's `GEval` needs structured
JSON back from its judge, which the mock `call_llm` (no
`ANTHROPIC_API_KEY`) can't produce — LLM-judged cases correctly report
"could not judge" in that case rather than crashing or faking a pass.
Deterministic cases are unaffected either way. `Logfire` and
`Agent-EvalKit` were considered and explicitly not used — both need a
real external account/vendor-specific framework this repo doesn't have
anything to genuinely integrate with.

**Prometheus** — `prometheus_client`, a real `/metrics` endpoint at the
app root (not under `/api`, matching Prometheus's own convention),
instrumented at the actual real call sites: `/run`, the webhook trigger,
and eval-run completion — not synthesized from a DB snapshot at scrape
time. Verified with real incrementing counts across manual runs,
webhook-triggered runs (correctly labeled `trigger="webhook"`), UCS
soft-denials, and eval runs, plus confirmed genuinely valid Prometheus
exposition format (`# HELP`/`# TYPE` lines). Standing up an actual
Prometheus server to scrape this is infrastructure outside this repo;
what's real here is that there's something correct for it to scrape.

The mockup's eval manager pushes the "attempt the real backend, honest
fallback" pattern (already used for the webhook and knowledge-base test
buttons) one step further: if the real backend happens to be running
alongside the mockup, the *entire* create-set → add-case → run workflow
genuinely works end to end, not just a single action. A real bug was
caught while testing this in the mockup, not glossed over: a defensive
`event.stopPropagation()` I'd added to keep a nested button's click from
also toggling its parent row instead broke the click from ever reaching
the delegate handler at all — removed once testing showed the delegate's
own `closest()` matching already handled that correctly without it.

## UCS soft-check toggle, real Sub-Agent, real Agent strategy selector

Closes two of the three previously-listed unwired node types. Both were
verified end to end, not just unit-tested in isolation.

**`UCS_SOFT_CHECK` (temporary, explicitly reversible)** — env var,
default off. Off: unchanged hard enforcement, verified. On: a real UCS
denial *or* a real "couldn't reach UCS" during execution
(`handle_gov`/`handle_tool`) is genuinely still attempted and recorded
(`ctx["vars"]["ucs_soft_denials"]`), just not enforced — set to unblock
subagent-chain development without needing real role bindings, or even a
running UCS instance, for every agent in a call chain. Publish-time
enforcement is deliberately untouched, since that's a separate,
already-verified real feature.

**`subagent` — genuine recursive invocation**, not simulated: runs the
target agent through the exact same `run_flow_sync` a direct `/run`
call's own sync branch uses — the one deliberate exception to §0.53's
otherwise-consolidated routing (`ARCHITECTURE.md` §6, §8.1): a nested
Sub-Agent call always takes the sync path even when the target agent's
own flow has a hitl node, since routing it durably would need a real
nested Temporal workflow inside an already-running node's own
execution. With the same invoking user threaded through every hop (the
multi-hop principal chain per §7.6). Verified: a real 3-hop chain
(A→B→C) where C's real output bubbles all the way back through every
level; real cycle detection that refuses a call *before* ever attempting
it, not after the stack overflows; the 5-hop safety cap (matching UCS's
own `chain_depth_cap`) correctly refusing a 6th hop. The frontend's
target picker is sourced from the real agent list (`api.listAgents()`),
excludes the current agent (no self-selection), and shows an honest
warning if a configured target no longer matches any known agent.

**`agent` — three genuinely different execution patterns**, not three
prompts wearing different labels, all built on the same real-or-mock
`call_llm` every LLM node already uses:
- `plan_execute`: always exactly 2 real calls, the second genuinely
  conditioned on the first's real output.
- `function_calling`: 1 or 2 real calls depending on what the model
  itself decides — verified via monkeypatch that the branch genuinely
  fires and reuses `handle_tool`'s exact honest placeholder pattern for
  the "tool result."
- `react`: a real accumulating multi-step loop, verified via monkeypatch
  to take exactly as many real steps as needed and stop precisely when
  the model concludes.

One thing worth flagging rather than leaving to be a confusing surprise:
testing `react` with no `ANTHROPIC_API_KEY` set will always look like it
stops after exactly one step. That's not a bug — the mock adapter echoes
back a slice of its own prompt, and the ReAct prompt necessarily contains
the literal words "Final Answer:" as an instruction to the model, so the
echo accidentally satisfies its own stopping condition on the first
iteration. Confirmed via monkeypatch that the real loop logic is correct;
documented in the code itself so it doesn't look broken later.

A real regression was caught and fixed while wiring the frontend for
this: adding `api.listAgents()` to the shared `NodeConfigPanel` component
broke four existing test files that render it but never mocked that
call — each one now correctly does.

## Real nested Loop containers, and Comment nodes

### Follow-up fix: growing a loop via its own port didn't actually contain the child

A real bug in the feature above, found by actually using it: node creation
has two paths — drag a type from the palette, or click a port to open the
searchable picker and pick a type. Only the *first* path ever set
`parent_id`. Clicking a loop's own port (the natural way to grow a loop,
matching a reference tool's port-based interaction) silently created a
regular top-level node instead of a real body child, no matter how many
times you tried. Fixed in both the picker-driven creation flow and its
mockup equivalent: picking a type from a loop's own port now makes it that
loop's first child; picking a type from a port on a node that's *already*
a loop child continues the same body chain rather than escaping it. Also
gave an empty loop's rendered box a real minimum size (320×220) — the
un-fixed box for a loop with no children yet was barely bigger than the
loop node itself, a genuine contributor to a drag-and-drop landing just
outside it. Verified with a real reproduction of the exact scenario: a
fresh loop, click its port, pick a type, assert `parent_id` — fails
against the old code, passes now.

### Real per-node-type icons

The palette (and canvas node cards) previously showed a plain colored dot
per node type. Now each of the 19 real node types gets a distinct icon —
16 new SVG symbols added, 4 existing ones reused where they already fit
(branch for `condition`, plug for `tool`, database for `memory`). Verified
by asserting more than 5 *distinct* icon references actually render in the
palette (not the same icon repeated), and that specific types resolve to
their real icon (`llm` → brain, `loop` → infinity).


**Loop upgraded from a body/exit-port workaround to a genuine nested
container.** Nodes get a real `parent_id` field; dropping one inside a
loop's rendered box on the canvas makes it a real body node, wired among
its siblings with the same real edges (including their own `condition`
branches) as any top-level part of the flow. A body branch ending at a
real `exit_loop` node stops the loop immediately and falls through to
whatever the loop's own single downstream edge points to;
`max_loops` remains as a safety cap, not the primary exit mechanism.
`run_flow_sync` and the Temporal workflow both got a real engine rewrite
— a shared `exec_node`/`run_loop_body` structure, not two implementations
that could quietly diverge. Verified with a full execution trace matching
the reference pattern exactly (`template → condition → exit_loop`, with
the *internal* condition genuinely deciding when to stop), plus the
`max_loops` safety cap and an empty-loop-container edge case.

One real bug caught mid-build, not glossed over: `exit_loop` was being
detected and used to `break` the loop *before* it was actually executed —
silently contradicting its own docstring's claim that it gets a real trace
span. Fixed in both execution paths.

**Comment nodes** are real documentation — a sticky note on the canvas,
config-backed (Title/Body), persisted through the same generic node-config
PATCH every other field uses. They deliberately have no output port and
are rejected as a connection target with an honest message if you try —
they can never be reached during execution, proven by asserting a comment
never appears in a real run's span trace even when placed directly beside
active nodes.

Both features hit two genuine test-infrastructure bugs during
verification, not swept under the rug: jsdom's `DragEvent` doesn't
reliably apply `clientX`/`clientY` from a plain init object, which made an
earlier version of the "drop outside a loop" test pass for the wrong
reason (`NaN` coordinates coincidentally fail every containment check);
and `ToastHost` — the component that actually renders toast messages —
lives in `App.tsx`, not in `AppProvider`, so a naive test wrapper can
correctly execute the rejection logic while never being able to see its
message. Both fixed at the test level once diagnosed, not worked around.

## Real cross-run memory

The `memory` node genuinely persists across separate runs — not just
within one execution's context — via a new `AgentMemory` table. `read` /
`write` / `read_write` modes, `agent` scope (shared across every run) or
`conversation` scope (isolated per invoking user — the closest honest
approximation available without a real session concept elsewhere in this
repo). Verified in layers: two independent sessions, one writing and a
completely separate one reading the same value back; scope isolation
between two different users; `read_write` correctly reading the *old*
value before overwriting; and a full three-run integration test through
the real `run_flow_sync` path where each run's output was built on the
value the previous run stored — a genuine accumulator, not a demo of the
mechanism in isolation.

## Real multi-turn chat history (§0.47, GAP_REPORT)

A real, structured `ChatMessage` table backs a genuine multi-turn
`messages` array sent to the real Anthropic API — not a flattened text
transcript pasted into the system prompt (the older
`AgentMemory.__llm_chat_history__` approach, still real and still used
by `handle_agent`/`handle_parameter_extractor`'s own separate
`memory_enabled` toggles, untouched by this change). Opt in per agent
via Features → Chat History (`features.chat_history.enabled`) — any
`llm` node without its own explicit `memory_enabled` inherits this;
an explicit per-node `true`/`false` still overrides it. Bounded to the
last `CHAT_HISTORY_MAX_TURNS` (20) messages by count — a real, stated
limit, not a token-budget-aware truncation.

## Authentication and authorisation — deny by default, permissions only

Contract: `docs/auth/01-CONTRACT.md` (task: `docs/auth/TASKS-22-23.md`).
This service uses the shared library **InaiEra_dataplane_GatewayAuth v1.1**
and contains no role names: roles are data owned by the Admin Portal, and a
new role or a change to what a role may do needs no code change here.

**Deny by default.** `app/main.py` calls
`protect_app(app, public_paths=["/livez", "/readyz", "/embed.js"])`. Every
other route — `/api`, `/mcp`, `/internal`, `/metrics`, `/api/health` —
answers **401** unless `TRUST_ENVOY_HEADERS=true` (exact, case-sensitive)
AND `x-inaiera-gateway-secret` matches `ENVOY_INTERNAL_SECRET` (constant-time)
AND `x-user-name` is present.

**Permissions.** Each route declares the permission it needs
(`Depends(require_permission("agent:<area>:<action>"))`, or
`require_any_permission(...)` in `app/permissions.py` for endpoints several
pages share). Missing permission = **403**. The full list is in
`permissions.json` at the repo root (imported by the Admin Portal).
`GET /api/me` returns `{ user, userId, tenant, permissions }` for the UI.
`x-user-roles` is never consulted; forging it grants nothing.

Headers, set by Envoy only (it strips client-supplied copies first):

| Header | Claim |
|---|---|
| `x-user-name` | `preferred_username` |
| `x-user-id` | `sub` |
| `x-tenant-id` | `tenant` |
| `x-user-groups` | `groups` (base64 JSON) |
| `x-user-permissions` | `permissions` (base64 JSON) |
| `x-inaiera-gateway-secret` | Envoy config, `append:false` |

The former legacy header path (`X-Auth-Request-Email`,
`OIDC_GATEWAY_SHARED_SECRET`, `OIDC_DEV_BYPASS_*`) and the old
`MAINTENANCE_TOKEN` header are removed. `/internal` maintenance needs
`agent:maintenance:run`; the CLI (`python -m app.maintenance`) needs no
network surface. Per-agent UCS grants (`app/agent_guard.py`,
`AGENT_RBAC_ENFORCE`) remain an optional second gate on top of route
permissions.

**Dependency.** `requirements.txt` pins
`inaiera-gateway-auth @ git+https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git@v1.1.0-rc.1#subdirectory=python`.
The repo is private, so the Dockerfile's pip step takes a GitHub read token
as a BuildKit secret (`docker build --secret id=gh_token,env=GH_TOKEN .`).

To exercise it locally (this checkout ships `TRUST_ENVOY_HEADERS=false`):

```
curl -i http://127.0.0.1:8001/api/agents \
  -H "X-Inaiera-Gateway-Secret: $ENVOY_INTERNAL_SECRET" \
  -H 'X-User-Name: alice' -H 'X-Tenant-Id: prod_metalake' \
  -H 'X-User-Permissions: agent:access:view,agent:studio:edit'
```

## Real loops — without a nested-container data model

The `loop` node reuses the exact same two-handle branching mechanism
`condition` already had (body/exit ports instead of true/false) rather
than needing the bigger nested/child-node schema change a container-style
loop would require. The person wires the last body node's output back to
the loop node itself, closing a real cycle in the graph; `MAX_GRAPH_STEPS`
— already in place as a safety net against an accidental cycle — now does
double duty as the loop's hard iteration cap. `{{loop_iteration}}` is
genuinely available to body nodes, incrementing each real pass. Verified
with an actual span-by-span execution trace: `loop → template → aggregator`
repeated exactly `max_loops` times, then a correct exit; a separate test
confirmed `termination_condition` exits early and independent of
`max_loops` — a test that caught my own wrong assumption about the
check-then-act timing mid-verification, not a code bug. One honestly-noted
gap: the loop's own `max_loops`/`termination_condition` fields read raw
config, not the `{{env.X}}`-interpolated version the node's own handler
would see — low practical impact, not worth the extra plumbing yet.

## Real Doc Extractor — cloud storage links, not a file upload pipeline

`docextractor` genuinely fetches and extracts text — not a placeholder.
Input is a URL: a literal S3 (`s3://bucket/key` or `https://bucket.s3.
amazonaws.com/...`), GCS (`gs://bucket/key` or `https://storage.
googleapis.com/...`), or Azure Blob link, or a `{{var}}` reference to one
produced by an earlier node (e.g. a field in a webhook payload). Real
extraction via `pypdf` (PDF) and `python-docx` (DOCX), plain decode for
text-like formats.

**The honest security boundary** (`_resolve_cloud_url`'s docstring): no
AWS/GCP/Azure SDK, no service-account credentials configured. `s3://` and
`gs://` shorthand convert to their public virtual-hosted-style URL, which
only resolves if the object is actually public. The real, supported case —
matching how these links are normally shared for one-off document
processing — is a **presigned** HTTPS URL (S3 presigned URL, GCS signed
URL, Azure SAS token), which already carries its own auth in the query
string and needs no SDK at all.

Verified in layers: the URL-provider detection logic against all three
providers plus presigned-URL passthrough (6/6 cases correct, plus an
honest failure for an unrecognized scheme); real PDF extraction proven
against an actual `reportlab`-generated PDF with real text content (not
just a blank-page non-crash test); real DOCX extraction proven against an
actual `python-docx`-generated file; and a full real network fetch +
extraction against a live, reachable URL. One correction worth recording
here because it very nearly went unnoticed: an initial test against a
real public S3 bucket returned a 403 that looked exactly like a genuine
S3 "bucket not found" error — it was actually this **sandbox's own
network egress proxy** silently blocking the `amazonaws.com` domain,
identifiable only via the `x-deny-reason: host_not_allowed` response
header. In a real deployment with normal network access, the same code
would succeed against that same URL.

## Real per-agent environment variables

The Workflow IDE's "Env" toolbar button opens real, agent-scoped
environment variables (String / Number / Secret) — set once, reference
from any node's config as `{{env.KEY_NAME}}`, and the real stored value is
substituted in at execution time (`harness.py`'s `interpolate_config` /
`load_env_map`, wired into both the sync and Temporal durable paths so they
can't silently diverge).

**Secret masking is a real security property, not a UI trick.** The raw
value is echoed back exactly once — the immediate response to the
create/update call — the same pattern cloud providers use when generating
an API key. Every GET after that returns a fixed mask; only the harness,
running server-side during execution, ever reads the real value from the
database. Verified end to end: created a secret, confirmed it's masked on
every subsequent list call, then confirmed the *real* value actually
reached a live outbound HTTP request's headers during execution (a genuine
148ms network round trip, not a mock).

Porting this to the standalone mockup surfaced a real, separate
architectural bug in the mockup itself: its single-`overlay`-string
`render()` function let a later-checked drawer silently clobber an
already-open modal (or vice versa) — a "Add Variable" opened from within
the Env Vars drawer would show blank content. That bug was structurally
impossible in the React app, which mounts `DrawerHost` and `ModalHost` as
independent sibling components, so the mockup needed its own explicit fix
(assembling drawer and modal HTML separately, then concatenating) that
React never required. Fixed and re-verified with a 10-case test pass plus a
full regression sweep of every earlier interactive feature (drag-connect,
click-to-add node picker, trigger types, all 10 routes) to confirm the fix
introduced nothing else.

## Real trigger types

The trigger node now offers three real options — User Input (the default),
Schedule Trigger, and Webhook Trigger — not just a fixed placeholder.
**Webhook Trigger is genuinely functional**: picking it mints an unguessable
token client-side (same security model as most webhook-trigger tools —
Zapier/n8n included) and persists it via the same generic node-config PATCH
every other field uses. `POST /api/webhooks/{token}` (`routers/webhooks.py`)
looks up the agent by that token and runs it through the *exact same*
`harness.py` code path as the manual Test button — not a separate, fake
path. Verified end to end: real persistence across a fresh fetch, a real
POST that produced real `Run`/`Trace`/`AgentLog` rows tagged
`principal=webhook:...`, an unknown token correctly 404ing, and malformed/
empty payloads handled without crashing.

Schedule Trigger persists a real cron expression the same way, but is
honestly labeled in the UI: nothing actually schedules it yet — there's no
scheduler process in this reference implementation, and the panel says so
rather than implying otherwise.

Implemented identically in the React app and the standalone mockup, with
the mockup's "Send test webhook" button attempting a genuine fetch against
the real backend (`127.0.0.1:8000`) if it happens to be running alongside
the mockup, and giving an honest "no backend reachable" message rather than
faking success if not.

## Real node-to-node connections

The Workflow IDE's canvas is a genuine directed graph, not an implicit
left-to-right ordering. `agent.flow` has an explicit `edges` array
(`{id, source, target, source_handle}`) alongside `nodes` — moving a node no
longer silently rewires the flow, and a `condition` node genuinely branches
to a different next node depending on its True/False outcome (two separate
output ports, dragged and connected independently). Backend: real edge
CRUD (`POST`/`DELETE /api/agents/{id}/edges`), cascade-delete when a node is
removed, and both execution paths (sync in `harness.py`, durable in
`temporal_workflow.py`) walk the same real graph via a single shared
`compute_next_node_id()` — so branching can't silently behave differently
between the two paths. Frontend: SVG bezier edges, drag-from-port-to-node to
connect, click an edge to remove it. Verified two ways: direct Python calls
proving a condition node executes only the correct branch (not both, not
neither), and a real jsdom-mounted component test that dispatches actual
`mousedown`/`mousemove`/`mouseup` events on the rendered DOM and asserts the
resulting API calls.

**Click a port (don't drag it) to spawn a new, pre-connected node** — a
second real interaction alongside drag-to-connect, matching common
flow-builder conventions. A press-and-release under a small movement
threshold opens a searchable, categorized node picker instead of starting a
drag; picking a type calls `addNode` then `addEdge` in one step, so the new
node lands already wired to the port you clicked. Unconnected ports render
as a prominent "+" badge rather than a plain dot, so this is discoverable
without hovering first. Implemented identically in both the React app and
the standalone mockup HTML — the mockup's version does real search-as-you-
type filtering via a *targeted* DOM update rather than the app's usual
full-page re-render, specifically so the search input doesn't lose focus on
every keystroke. Verified with 5 new jsdom tests per artifact (React:
Vitest + Testing Library; mockup: raw jsdom) covering picker-open, category
listing, live search filtering, pick-to-spawn-and-connect, and click-outside
to cancel — plus a re-run of the original drag-to-connect tests to confirm
no regression.

## Database

**Postgres 16 + pgvector**, matching §10 of the design doc (confirmed there as
"already platform-standard," not a new dependency). Not SQLite — an earlier
version of this repo used SQLite for zero-setup convenience, but that was a
real mismatch with the design and has been replaced.

`docker-compose.yml` in the repo root brings up `pgvector/pgvector:pg16` with
the extension pre-available:
```bash
docker compose up -d
```
The backend connects via `DATABASE_URL` (defaults to
`postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/inaiera_agents` —
override via env var for other setups). `init_db()` runs
`CREATE EXTENSION IF NOT EXISTS vector` on startup and creates all tables;
JSON-shaped columns (the agent flow graph, alert history, tool params, model
provider type lists) use native `JSONB`, not generic `JSON`.

No vector columns are populated yet — there's no real embedding/RAG pipeline
in this CRUD skeleton (see the stub table above) — but the extension is
enabled and ready for when a Knowledge Retrieval node's chunk store gets built.

### A real bug this migration caught

Moving off SQLite and testing against real Postgres surfaced a genuine
persistence bug in the flow-graph endpoints (`add_node`/`move_node`/
`update_node_config`/`delete_node` in `routers/agents.py`): the code did
`nodes = a.flow.get("nodes", [])` then `nodes.append(...)`, mutating the
**same list object** already referenced by `a.flow` before reassigning
`a.flow = {"nodes": nodes}`. Because the mutation happened in place first,
by the time SQLAlchemy's flush-time change detection compared old vs. new
values, they looked identical — so the `UPDATE` was silently skipped. The
mutation "worked" in the sense that the HTTP response looked correct
(it reflected the in-memory object, already mutated), but nothing was
actually persisted to disk — a fresh request would show the old data.

This is a known SQLAlchemy JSON-column gotcha. Fixed in all four endpoints by
deep-copying before mutating (so old/new are genuinely different objects) and
calling `flag_modified(a, "flow")` to force the `UPDATE` regardless. Verified
by testing each mutation via **two separate HTTP requests** — one to mutate,
a second fresh one to confirm it actually stuck — not just checking that the
immediate response looked right. The alert-history endpoints never had this
bug because they already built new lists via `[*old, new_item]` instead of
mutating in place.

## What's real vs. what's a stub

This is a genuine CRUD/API application — every button that mutates data
(publish an agent, drag a node onto the canvas, acknowledge an alert, save an
API key) makes a real HTTP call to a real backend that persists to a real
Postgres database.

**As of this revision, real agent execution exists too** — `POST
/api/agents/{id}/run` genuinely walks the flow graph and executes it (§3.6),
routing sync vs. durable exactly per the design's rule (§3.6/§5):

- **A real interpreter** (`harness.py`) — one dispatch table shared by every
  agent, matching §3.5/§3.6 exactly.
- **A real dual-check entitlement gate** (`ucs_client.py`) — calls an
  actual, separately-built UCS product over HTTP; genuinely denies
  execution when there's no matching group binding, on either side of the
  dual-check independently; verified against a real running UCS instance
  with its own audit log corroborating every check.
- **A real Temporal workflow** (`temporal_workflow.py`, `worker.py`) for the
  durable path — verified against an actual running Temporal dev server;
  `temporal workflow list` independently shows the completed execution, not
  just this API's own claim that it ran.
- **A real pluggable model adapter** (`model_adapter.py`) — makes a genuine
  Anthropic API call if `ANTHROPIC_API_KEY` is set; otherwise returns an
  explicitly-labeled mock (`"mock": true`) so a mock response can never be
  mistaken for a real one.
- **Real, kernel-level sandboxed code execution** (`sandbox.py`, §0.55
  GAP_REPORT) — a genuine, separate Linux network/mount/PID namespace,
  read-only root filesystem, non-root execution with capabilities
  dropped, plus the pre-existing hard CPU/memory/time limits. Not
  gVisor/Firecracker (see the table below for why), but real,
  kernel-enforced isolation, not a subprocess with a different `cwd`
  and an honesty gap between what the module claimed and what it did.

What's still a stub or a scoped-down simplification — deliberately, and
documented in each module's docstring:

| Area | What's real here | What's still simplified/stubbed |
|---|---|---|
| **Node handlers** | 12 of 16 types have real execution logic: trigger, gov, retrieval, llm, tool, code, respond, condition, http, aggregator, classifier, template | 4 remain unwired for real architectural reasons — see above, not a "haven't gotten to it yet" list |
| **UCS entitlement** | Real — genuinely calls a separate UCS product over HTTP; see "Connecting to a real UCS instance" above | Not bundled in this repo; you need that repo running too. UCS itself may still have its own scoped-down pieces (Gravitino/OpenLineage/etc.) — check its own docs |
| **Retrieval / Tool data fetch** | The entitlement check before each is real | The actual data fetch is a placeholder string — no real pgvector embedding pipeline or Trino connector exists yet |
| **Code sandboxing** | Real network/mount/PID namespace isolation, non-root execution, capabilities dropped, read-only root filesystem, resource limits — see §0.55, GAP_REPORT | Not gVisor/Firecracker — those need infrastructure (container runtime privileges / a Linux+KVM host) this environment doesn't have; no seccomp syscall filter either — a stated gap |
| **LLM calls** | Real if you provide `ANTHROPIC_API_KEY` | Mock by default, honestly labeled — I can't obtain API credentials myself |
| **Trace spans (historical/seed data)** | Live runs produce real spans from real execution | The pre-seeded historical traces (from `seed.py`) still synthesize a waterfall from a stored status, since they predate this run capability |
| **Agent publish** | Calls the real entitlement-plan resolver; genuinely blocks (HTTP 403) if the agent has no role bindings | Still just a version-bump + status flip otherwise — no build/deploy step, matching §3.6's "publishing is a config change, not a deployment" |
| **MCP server add** | Persists real state | No real OAuth handshake or discovery call against the external service |
| **Data source connect + browse + ingest** | Credentials are stored Fernet-encrypted and genuinely used: Google Drive, SharePoint, OneDrive, Google Cloud Storage, Notion and GitHub each have a real browse/fetch adapter (`app/data_sources.py`), and files picked from them go through the same ingestion path as a local upload — from the Documents tab and from the external Upload API (`POST .../api/upload-from-source`), which lets a command name a file's full location instead of a bare filename the browser could never resolve. For a LOCAL file the Upload API tab additionally offers a directory pick (`webkitdirectory`, the only browser API that returns real path structure) and `POST .../api/upload-from-store`, which references a file already in the platform's upload store so the copied command carries no path at all and runs from any directory | Auth is client-credentials / service-account / token — app-only access, so no per-user delegated consent. No live tenant was available here, so the request shapes follow each vendor's documented API and every provider error is surfaced verbatim rather than being verified end to end against a real account |

Everything above is documented with a docstring in the relevant module —
search for "REAL", "STUB", or "reference implementation" in `backend/app/`.

**As of this revision, UCS is also real** — not a reference implementation
of its contract anymore. `ucs_client.py` calls an actual, separately-built
UCS product (a genuinely different FastAPI service, its own Postgres, its
own RBAC/audit tables — provided separately, not bundled in this repo) over
HTTP at `UCS_BASE_URL`. Verified two independent real checks per object (the
agent's principal via its own bound groups, the invoking user via UCS's own
group-membership table) — including a case where a user personally had
access but the agent didn't, and was correctly denied anyway, matching the
design's "no implicit access via an agent" principle. UCS's own audit log
independently recorded every check.

**As of this revision, every node type's config panel has full field parity
with the approved mockup** — a separate axis from node *execution* logic
(below). A systematic diff against the mockup's source found 9 of 16 node
types missing fields in the shipped React build (e.g. the LLM node was
missing Context, Allowed tools, Reasoning tag separation, Retry on failure,
and Error handling — all silently dropped when the mockup was ported to
real code). All fields restored across `llm`, `retrieval`, `agent`,
`classifier`, `iteration`, `loop`, `code`, `template`, and `http`, each
wired through the same real PATCH-and-persist pattern as every other field
— verified by setting every new field and re-fetching fresh from a separate
request, not just checking the immediate response. Two node types
(`http`, `classifier`) also got their new fields wired into real execution
in `harness.py` (HTTP Headers/Params are now genuinely sent on the request;
a custom classifier Instruction is now genuinely used as the system
prompt) rather than just persisting inertly. Two inaccurate UI claims caught
in the same pass and corrected: the Code node claimed Firecracker/gVisor
sandboxing it doesn't have, and the HTTP node claimed governance it doesn't
have.

## Node execution logic (a separate concern from config-panel completeness)

**As of this revision, 12 of 16 node types execute for real** (up from 7):
trigger, gov, retrieval, llm, tool, code, respond, **condition, http,
aggregator, classifier, template**. The `http` handler makes a genuine
outbound call — no placeholder, verified against a real external endpoint
(including a genuine 403 rate-limit response, which is itself proof it's
not mocked).

**Four node types remain unwired, for real reasons, not just lack of time:**
- `iteration`/`loop` — the flow-graph schema is a flat sequential node list
  with no concept of a "loop body" (no nested/child node lists). There's
  structurally nothing for these to iterate over yet; fixing this means
  extending the schema, not writing a handler.
- `docextractor` — there is no file upload/storage pipeline anywhere in the
  app (no file input in the frontend, no blob storage in the backend).
  There's no file for this node to extract from.
- `hitl` — the durable-path *routing* trigger works (a flow containing this
  node type correctly goes through Temporal), but the actual
  pause-for-human-approval mechanism (a Temporal signal handler) isn't
  implemented — a run currently just skips this node.
- `subagent`/`memory`/`agent` — each needs its own real design pass, not a
  quick addition (subagent needs the multi-hop principal chain from §7.6;
  memory needs a session store; agent needs an actual agentic-strategy
  selector distinct from the LLM node's mode toggle).

## Connecting to a real UCS instance

`ucs_client.py` needs a running UCS instance reachable at `UCS_BASE_URL`
(default `http://127.0.0.1:8001`). UCS is a separate product/repo — not
included here. To point this repo at one:

```bash
# In the UCS repo:
export DB_DSN="postgresql+asyncpg://<user>:<pass>@<host>:5432/<db>"
alembic upgrade head
uvicorn app.main:app --port 8001

# In this repo:
export UCS_BASE_URL="http://127.0.0.1:8001"
```

RBAC bindings and group memberships live entirely in UCS's own tables
(`rbac_bindings`, `custom_groups`, `custom_group_members`) — this repo's own
`role` table is only used to look up which *groups* an agent is bound to
before asking UCS to check them; it is not itself the source of truth for
access decisions anymore.

## Running real execution

Beyond `docker compose up -d` + `uvicorn` + `npm run dev`, real execution needs:

```bash
# Get the Temporal CLI (single binary, no Docker) if you don't have it:
curl -L "https://github.com/temporalio/cli/releases/download/v1.8.2/temporal_cli_1.8.2_linux_amd64.tar.gz" -o /tmp/temporal.tar.gz
tar -xzf /tmp/temporal.tar.gz -C /tmp && sudo mv /tmp/temporal /usr/local/bin/
# (macOS: brew install temporal — see https://github.com/temporalio/cli for other platforms)

# Temporal dev server (no Docker needed — single binary)
temporal server start-dev --ip 127.0.0.1 --port 7233 --ui-port 8233

# Temporal worker (separate process, required for the durable path only)
cd backend && python -m app.worker

# Optional: real LLM calls instead of the labeled mock
export ANTHROPIC_API_KEY=sk-...
```

Trigger a run: `POST /api/agents/{id}/run` with `{"input": "...", "invoking_user": "user:someone"}`.
A flow with a `hitl` node routes through the real Temporal workflow; every
other flow runs sync. Every run creates real `Run`/`Trace`/`AgentLog` rows —
check them in the UI (Runs/Traces pages, or the agent's Logs tab) after
calling the endpoint.

## Project layout

```
backend/
  app/
    models.py        # SQLModel tables
    seed.py           # seeds the exact mock data from the approved mockup
    main.py           # FastAPI app + CORS + router wiring
    routers/           # agents, templates, runs, traces, alerts, monitoring, integrations
frontend/
  src/
    api.ts             # typed fetch client for every backend endpoint
    constants.ts        # node type registry, palette groups, nav, personas
    context/AppContext.tsx  # global persona/toast/drawer/modal state
    components/         # Sidebar, Topbar, Drawer/Modal shells, NodeConfigPanel (all 16 node types)
    drawers/             # AgentProfile (9 tabs), Run/Trace/Alert detail, Tool drill-down
    modals/               # NewAgent, Publish, AddMcp, ApiKeyConfig, DsConfig
    pages/                 # Home, Agents, Builder, Templates, Integrations, Runs, Traces, Monitoring, Alerts, Settings
```

## Running it

**Database:**
```bash
docker compose up -d
```

**Backend:**
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```
API docs at http://127.0.0.1:8000/docs. Tables are created and seeded
automatically on first startup, against the Postgres instance from
`DATABASE_URL` (see Database section above).

Real UCS agent sync (`ucs_sync.py`) is optional — without it, agent
create/publish still work normally, just with an honest "not synced"
status. To enable it: `UCS_BASE_URL` (default `http://127.0.0.1:8001`),
`UCS_ADMIN_TOKEN` (a real bearer token with UCS's admin realm
role from your UCS deployment's IdP — UCS's admin endpoints do genuine
JWT verification, no soft-check bypass), and optionally
`UCS_AGENT_CATALOG`/`UCS_AGENT_SCHEMA` (default `agent_catalog`/
`agents`) and `PLATFORM_BASE_URL` (the URL UCS should record as this
agent's real invocation endpoint).

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```
Opens at http://localhost:5173. `src/api.ts`'s `BASE` is `window.location.origin`, not a hardcoded URL — `vite.config.ts`'s own dev-server proxy forwards `/api`, `/mcp`, and `/embed.js` to the backend on `:8000`, so this works locally without any config, and the same built code is correct in production behind a same-origin reverse proxy/ingress (§0.44, GAP_REPORT — this replaced an earlier hardcoded `http://127.0.0.1:8000` constant that would have broken outside local dev).

**Coding agent (workflow generation from a prompt) — optional, §0.46:**
The "Create with AI" feature in Studio uses a real, separate LLM
integration from the rest of this platform — any OpenAI-compatible
endpoint, not the Anthropic client `model_adapter.py` uses everywhere
else. Without it configured, generation still works via a real, clearly
labeled mock. To use a real model:

*On Apple Silicon (local dev):* [`mlx-lm`](https://github.com/ml-explore/mlx-lm) is the recommended local option — more mature Metal support than vLLM's own Apple Silicon path (`vllm-metal`) as of this writing.
```bash
pip install mlx-lm
mlx_lm.server --model mlx-community/Qwen2.5-Coder-7B-Instruct-4bit --port 8080
```
Then, before starting the backend:
```bash
export CODING_AGENT_LLM_BASE_URL=http://127.0.0.1:8080/v1
export CODING_AGENT_LLM_MODEL=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit
```

*In production:* point `CODING_AGENT_LLM_BASE_URL` at a real vLLM server (CUDA GPU infra) serving the equivalent, non-quantized Qwen2.5-Coder model — the same OpenAI-compatible API shape, so no code changes between the two.

## Verified working

- `npx tsc -b` — clean type-check, zero errors
- `npx vite build` — clean production build
- Backend endpoints exercised via curl: add/move/delete node, publish (with
  locked-node rejection), alert acknowledge/resolve (with who+when tracking),
  trace span synthesis, model-provider API key config, data-source OAuth
  config, MCP server add
- CORS verified working between the dev server (port 5173) and the API (port 8000)

## What to build next, if this goes to production

In priority order, per the design doc:
1. Replace the publish stub with a real UCS entitlement-resolve call (§7.6)
2. Wire the Orchestration Engine (Temporal) + Agent Harness so agents
   actually execute (§3.5, §3.6) — this is the biggest gap; nothing in this
   repo can currently run an agent against a real LLM
3. Real OpenTelemetry pipeline feeding the Traces/Monitoring pages (§6.1)
4. Delegated (per-user) OAuth for data sources, and OAuth for MCP servers — data
   sources now authenticate app-only (client credentials for Microsoft, service
   accounts for Google, tokens for GitHub/Notion), which needs no redirect flow but
   also cannot see a user's personal drive without domain-wide delegation
5. Auth — there is currently no login; the persona switcher is a UI-only
   convenience for demoing role-based nav, not real authentication

## Template authoring and publishing

Templates use an explicit authoring lifecycle for locally authored **Organization-managed** and **Private** templates. **Platform-provided** means the release-managed packages shipped with InAiEra and remains read-only in Studio:

- `POST /api/templates` captures an Agent as a mutable **draft**.
- `PUT /api/templates/{template_id}` edits the draft and requires `If-Match: <draft_revision>`.
- `POST /api/templates/{template_id}/publish` validates the draft and creates an immutable `TemplateVersion` snapshot.
- `GET /api/templates` is the installable **published gallery** and always reads the immutable current version.
- `GET /api/templates/manage` is the authoring view and exposes drafts/unpublished changes.
- `GET /api/templates/{template_id}/versions` returns immutable publication history.
- Delete from Studio is an **archive**, preserving version history and Agent provenance.

The database uses `source=platform` for both platform-scoped origins, but the Studio labels them distinctly:

1. **Platform provided** — `release_managed=true`: shipped package files synchronized by the release seeder and read-only in Studio.
2. **Organization-managed** — `release_managed=false`: authored by a platform administrator in Studio and local to that data plane.
3. **Private** — owner-authored and local to that data plane.

Agents created from a template store both `template_id` and the exact `template_version_id` used. Template `app_type` is also versioned and restored when an Agent is instantiated, so Workflow/Chatflow/etc. behavior is preserved.

Package import/export, requirements derivation, credential redaction, duplication, category/vertical metadata, and one-click Agent instantiation/publishing remain supported. Imported and duplicated packages become Private drafts and must be explicitly published before appearing in the gallery.
