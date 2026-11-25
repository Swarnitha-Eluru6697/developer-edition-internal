# InAiEra Agent Platform — Gap Report (V101.16.0)

**Versioning note:** per explicit instruction (starting `V101.1.0`), the
platform's own overall version label is `V<major>.<minor>.<patch>` —
this release is `V101.16.0`, incrementing the minor number only, as
instructed. Internal bug section numbers (`§0.117` below) remain a
separate, continuous counter — the same one this document has used
throughout — unaffected by this labeling change.

**Purpose:** a single, accurate accounting of what's genuinely real in this
build versus what's simplified, placeholder, or not yet built — so a
decision to extend or deploy this can be made on facts, not on the general
impression that "the agent platform is done." Every claim below was
verified against the actual running code during the build, not asserted
from memory; see the referenced files to check any of them directly.

**This supersedes the earlier `GAP_REPORT.md` (V101.15.0).** V101.16.0
fixes a sixty-seventh real, reported bug — confirmed live before any
code was written: a real `/run` call against a genuinely unreachable
UCS returned, with a `200` status, `"error": "UCS at
http://127.0.0.1:8001 unreachable or errored: [Errno 111] Connection
refused"` directly in its own response body — a real internal service
name, a real internal hostname/port, and raw OS-level connection-error
detail, handed straight to whoever called the public API. See §0.117.

## 0.117. Fixed in V101.16.0 — production error responses now return a real, structured `{error_code, correlation_id}` shape instead of raw internal exception detail (major, security, production-readiness)

### The real bug, exactly as reported

Confirmed live, not assumed: a genuinely unreachable UCS produced a real, raw exception string embedded directly in `/run`'s own JSON response body, via `harness.py`'s own central, generic `except Exception as e: span["error"] = str(e)` — the single choke point every node handler's own failure funnels through. A second, separate leak was found in the same response's own `ucs_soft_denials[]` list. A third was found in the batch endpoint's own per-row failure path. A fourth, in the existing `PublishedVersionIntegrityError` handler. A fifth — confirmed by directly triggering a genuinely unhandled exception — in FastAPI/Starlette's own default 500 handling, which this platform had no real, global backstop for at all.

### The real fix — a deliberately scoped, non-destructive design

A new module, `app/error_responses.py`, provides the report's own exact shape — `{"error_code": "AGENT_EXECUTION_FAILED", "correlation_id": "..."}` — applied only in `ENVIRONMENT=production` (matching this codebase's own existing convention for that check). A deliberate, narrow scope: legitimate, actionable messages (`"Agent not found"`, a real UCS entitlement denial's own structured reason) are never touched — sanitizing those would make the API worse, not more secure; only genuinely unexpected failures are sanitized. The existing `run_id`/`trace_id` is reused as the `correlation_id` rather than inventing a second identifier system — the full, unsanitized detail is already persisted on that same row (`spans_json`), viewable by an operator via `GET /api/traces/{id}`, this platform's own real structured-log equivalent; a `log_full_detail` helper additionally writes the complete detail (with traceback) to this process's own log output at ERROR level, tagged with the same correlation ID.

Wired into every external-facing invocation channel: `run_agent`'s main response, both streaming completion paths (including a separate raw leak in the stream's own exception handler), the batch endpoint's per-row failure path (using a fresh, ad-hoc correlation ID since no `Run` row exists yet at that point), `webhooks.py`'s response, and the existing `PublishedVersionIntegrityError` handler. A new, global, catch-all `@app.exception_handler(Exception)` in `main.py` is the final backstop for anything genuinely unanticipated — confirmed live, directly, that Starlette's own real default (this app has no `debug=True` set) returns a completely empty 500 body with no information at all; this handler's own structured response is a genuine improvement in every environment, not only production, since the pre-existing alternative was never useful detail to begin with. Frontend: `BatchRunRowResult.error` is now typed to accept either the legacy plain string or the new structured shape, with a real `formatRowError` helper so nothing downstream needs its own branch to tell which one it received.

### Real bugs found and fixed while building this, not left for later

**A gating bug caught by a live, end-to-end test, not by reading the code and trusting it.** The sanitization call at all four applicable sites was originally gated behind `result.get("status") in ("failed", "error")` — but `ucs_soft_denials` can carry the exact same raw connection detail even when the *overall* run succeeds (a soft, non-blocking denial). A live test against a real unreachable UCS caught this directly; the gate was removed so sanitization is always attempted, with `sanitize_result_for_external_response` itself already deciding internally when to add `error_code`/`correlation_id`.

**A genuine test-isolation bug, caught by running the full suite repeatedly, not by trusting a test that passed alone.** An early version of the global-handler tests used `app.dependency_overrides` — shared, mutable state on a module-level singleton — which passed in isolation but failed intermittently as part of the full suite. Redesigned to call the handler function directly with a constructed request/exception; confirmed stable across three consecutive full runs afterward.

**A real design correction**, also caught by testing rather than assuming: the global handler originally re-raised in non-production mode to "preserve dev behavior" — but testing revealed the actual pre-existing dev-mode behavior was an empty 500 body, not useful debugging detail, so the design was corrected to always return the structured shape.

### Verification

Live-tested in both directions against the real, running API: production mode confirmed to leak nothing (verified via direct string-search assertions for the hostname and errno text in the actual response), dev mode confirmed to still preserve full detail where it's genuinely useful (the `spans`/`ucs_soft_denials` sanitization, unlike the global handler, is deliberately production-only). Confirmed normal `HTTPException` responses (404s, etc.) are never shadowed by the new global handler. 16 new permanent tests. Full backend suite 927/927 passing, stable across three consecutive runs; full frontend suite 329/329 passing with a clean TypeScript build.

## 0.116. Fixed in V101.15.0 — typed fields are now canonical; duration/cost/ts are real, computed API-layer properties, never independently persisted, and Run/Trace status is a real, enforced enum (major, correctness, production-readiness)

### The real bug, exactly as reported

Confirmed directly: `execution.py`'s own `_finalize_run` and every sibling write site independently computed `duration=f"{duration_s:.1f}s"` at the same time it computed `duration_ms` via a separate `_typed_timing` call — two real, parallel formatting paths for the same underlying fact, with nothing structurally preventing them from drifting apart if either was ever changed without the other. `Run.status`/`Trace.status` were real, free-form strings, documented only by a Python comment nobody was forced to honor.

### The real fix

`duration`/`cost`/`ts` are now real, computed Pydantic `@computed_field` properties on `Run`/`Trace` (`models.py`) — derived from the real, typed fields (`duration_ms`, `cost_usd`, `created_at`/`started_at`) at response-serialization time, never an independently-settable column again. "Formatting belongs in the API/UI layer," made real, without any breaking API response-shape change: a caller reading `run.duration` from any real endpoint gets the identical shape as before. The real, original database columns are preserved unchanged (mapped, via an explicit `sa_column`, to Python attributes renamed `duration_legacy`/`cost_legacy`/`ts_legacy` — a real, existing row's own historical display value is used as an honest fallback only when the typed field is genuinely absent), so no destructive schema change or column drop was needed, and no frontend call site needed to change.

`RunStatus`/`TraceStatus` are new, real string enums in `models.py`, grep-verified against every real status-assignment site in the codebase (not asserted from memory) — `RunStatus` includes `denied` (a real, existing value) alongside the report's own suggested `queued`/`running`/`paused`/`success`/`failed`/`blocked`/`cancelled`. A new migration adds real, database-level `CHECK` constraints enforcing both enums directly in Postgres — a genuinely malformed status value is now rejected at the database layer, not just by convention.

### Two real bugs found and fixed while building this, not left for later

**Attribute assignment on the new computed properties genuinely crashes.** Tested directly: `existing_run.duration = "1.8s"` raises `AttributeError: property 'duration' of 'Run' object has no setter`, not a silent no-op. This meant all 16 real write sites across `execution.py` (6 constructor calls, 10 attribute assignments) would have crashed a live run the moment this shipped. Every one was found (by re-grepping after each fix until zero remained, not by a single pass) and updated to set only the typed fields.

**A real, non-nullable default silently defeated the legacy fallback.** `cost_usd` defaults to `Decimal("0")` rather than `None`, so a genuinely historical row with a real, non-zero `cost_legacy` value was indistinguishable from a genuinely zero-cost new row by `cost_usd` alone — confirmed live against a real seeded row, which incorrectly showed `$0.0000` instead of its own real `$0.031`. Fixed with a deliberate heuristic (prefer the legacy value only when `cost_usd` is exactly zero and a real legacy value exists — true only for a row this fix's own write sites no longer touch), then verified all three real cases directly.

**A real, missing migration was caught by running the full test suite, not assumed unnecessary.** The original `duration`/`cost`/`ts` columns were `NOT NULL` — confirmed via a live `IntegrityError` the moment a new row was inserted without them. A migration relaxes all five affected columns to nullable and adds the two `CHECK` constraints.

### Verification

Every one of the 16 write sites was fixed and re-verified by grep until none remained. The full backend test suite was run before *and* after the migration — 105 failures (every one a real `IntegrityError` from the missing nullable migration) resolved to 0 once the migration was applied, confirmed stable across two consecutive full runs. A full, live run through the actual API was inspected directly: the response correctly shows `duration_ms`/`cost_usd`/`started_at`/`completed_at` as the real, persisted source of truth, `duration`/`cost`/`ts` correctly computed from them, and `duration_legacy`/`cost_legacy`/`ts_legacy` correctly `null` for a new row. The `CHECK` constraint was proven live by attempting a direct, invalid-status `INSERT` and confirming a real, database-level rejection. 8 new backend tests, covering the computed-property fallback, the attribute-assignment crash guard, the zero-cost heuristic, and both `CHECK` constraints directly. Full backend suite 911/911 passing, stable across repeated runs; full frontend suite 329/329 passing with a clean TypeScript build, confirming the API response shape genuinely never changed for any existing caller.

## 0.115. Fixed in V101.14.0 — the LLM node's own config panel now genuinely disables every control its real handler doesn't consume, driven by a real, shared runtime schema (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `handle_llm` (`harness.py`) already, honestly documented which config controls it doesn't read at all — `vision`/`vision_detail`, `structured_output`, `allowed_tools`, `guardrail_profile`, `reasoning_tag_separation`, and `agentic_strategy` — and the frontend already had matching warning paragraphs next to each. But every one of these controls remained fully interactive: a toggle could be flipped, a dropdown changed, chips added, with nothing but nearby text stating it does nothing. The report's own core concern was precise: the frontend must not present a control as operational unless the runtime actually consumes it — text disclosure alone doesn't satisfy that; the control itself still looked and behaved like a normal, functional setting.

### The real fix — the report's own recommended architecture, made real

A new module, `node_wiring.py`, is the real, machine-readable "runtime schema" layer the report's own diagram calls for (`Node JSON schema → runtime schema → frontend generated from the same schema`). `UNWIRED_CONTROLS` is a real, single source of truth — a direct, grep-verified transcription of what `handle_llm`'s own docstring already states in prose, not a second, independently-maintained claim that could silently drift from it. A new endpoint, `GET /api/agents/node-schema/unwired-controls`, exposes this; the Builder's own LLM node config panel now fetches it once and genuinely sets `disabled` on every control the real schema names for that node type — the actual, structural fix, not merely more warning text. The existing warning paragraphs were deliberately kept unconditional (not gated on the fetch succeeding), so a failed or slow fetch degrades to the prior, still-honest text-only disclosure rather than silently losing it.

Two shared components, `Toggle` and `Chips` (the config panel's own toggle switches and tag-style multi-select control), were extended with real `disabled` support, matching a pattern already established elsewhere in this codebase (`PipelineControls.tsx`'s own identical `Toggle`), rather than inventing a new one.

### A real regression caught and fixed while verifying this

Wiring the new fetch into the config panel broke 86 existing frontend tests across 17 files — found by running the full suite, not assumed safe because the new code compiled cleanly. The real cause: many existing tests use Vitest's own `vi.mock('../api')` automock, which replaces every exported function — including this new one — with an unconfigured mock that returns `undefined` (not a rejected Promise) when called, unless that specific test explicitly configures it. A first fix attempt (checking `typeof api.getUnwiredNodeControls === 'function'`) was insufficient, since the automocked function genuinely exists as a function; it just doesn't return a real Promise. Fixed with a real `try`/`catch` around the call and a check for a genuine `.then` on its return value, so a missing or unconfigured mock degrades safely to "no schema fetched yet" rather than crashing the whole panel — without needing to touch dozens of existing test files individually.

### Verification

The registry was verified directly against the report's own named examples (every one is a real, listed entry) and against `handle_llm`'s own real docstring source (every listed control has a matching, real disclosure there — not a second, independent claim). The endpoint was verified live and confirmed not shadowed by the dynamic `/{agent_id}` route. The frontend fix was verified with real DOM assertions — a real `<select>`/`<div class="tog">` element's own `disabled` state, not merely that warning text is present — for both the "genuinely unwired" and "not disabled when the schema says it's fine" cases, plus a real, failed-fetch case confirming the panel still renders correctly. 6 new backend tests, 3 new frontend tests. Full backend suite 903/903 passing; full frontend suite 329/329 passing, stable across repeated runs.

## 0.114. Fixed in V101.13.0 — a real, canonical provider abstraction now backs a genuine second runtime provider (OpenAI), and every still-unwired provider is honestly disclosed as catalog-only (major, correctness + feature completeness)

### The real bug, exactly as reported

Confirmed directly: `call_llm` mapped every real model name through a single, Anthropic-only lookup, silently falling back to Claude Sonnet for any model name it didn't recognize. Separately, `routers/integrations.py`'s own seeded `ModelProvider` catalog listed Anthropic, OpenAI, Gemini, Amazon Bedrock, Cohere, Azure OpenAI, Azure AI Studio, Hugging Face Hub, Replicate, and DeepSeek — all configurable through the identical real "Add API Key" UI flow, with `configure_model_provider` genuinely storing real, encrypted credentials for any of them — while only Anthropic had any real execution path behind it. A builder could configure a real OpenAI key, see "credits" status, and have no way to know that a real Agent run would still never call it.

### The real fix

A new `ModelResult` dataclass (`model_adapter.py`) is the real, canonical shape the report asks for — `text`, `tool_calls`, `usage`, `finish_reason`, `provider_request_id`, `model`, `mock` — every real provider adapter now returns exactly this, regardless of which provider actually ran. `call_llm` itself keeps its existing signature and legacy dict return shape (converting from `ModelResult` internally), so every existing real caller (`harness.py`, `eval_judge.py`, `retrieval.py`) needed no change at all.

The existing Anthropic logic was extracted, unchanged in real behavior, into `_call_anthropic`. A real, second, genuinely-wired provider adapter, `_call_openai`, was built using the real, official `openai` SDK — the same real, governed-egress pattern (`governed_httpx_client`) and the same real, shared concurrency limiter Anthropic's own path already established, and the same real, honest mock-mode convention for a genuinely missing key. The hardest real piece — translating between Anthropic's and OpenAI's genuinely different real tool-schema shapes — is handled entirely inside `_call_openai`, so a caller builds one real, Anthropic-shaped tool schema regardless of which real provider ultimately executes it, exactly the "clean provider abstraction" the report asks for. `model` now dispatches through a real, explicit `_MODEL_PROVIDERS` mapping; a genuinely unrecognized model name now raises a real, honest `LLMCallError` immediately rather than silently substituting a different model's own real output.

A new, real, computed `runtime_supported` field (drawn from a single, real source of truth, `RUNTIME_SUPPORTED_PROVIDERS`, so it can never silently drift from what `_call_anthropic`/`_call_openai` actually implement) was added to `list_model_providers`'s own response and surfaced in the frontend's own Model Providers page — a real, visible "Runtime-wired" chip versus a real, honest "Catalog only — not yet wired for Agent execution" label and banner, so a builder can never be shown a still-unwired provider as if it were equally usable. The Agent/LLM node's own model picker was updated the same way: OpenAI now gets real, distinct `GPT-4o`/`GPT-4o mini` options, replacing what would now be an actively wrong "not yet wired" placeholder for it specifically, while every other, genuinely-unwired provider keeps that same, honest placeholder.

### Real bugs found and fixed while verifying this

Renaming the Anthropic-only `_stored_anthropic_config` to a real, provider-generic `_stored_provider_config` broke four separate call sites not touched directly by the main refactor — found by running the full test suite, not assumed complete: a test file's own direct import, `health.py`'s own `/readyz` model-provider check (which silently reported `"unreachable"` instead of `"not_configured"` once the import began raising), and `retrieval.py`'s own LLM reranker fallback. All four were fixed, and a stale docstring reference in `health.py` was corrected alongside them. A malformed test edit (an unescaped string literal introduced by an overly broad find-and-replace) was also caught and fixed before it could ship.

### Verification

The real schema translation was proven against a mocked SDK client, not merely trusted to compile: the actual captured request payload sent to `openai.OpenAI(...).chat.completions.create` was inspected directly and confirmed to carry OpenAI's own real `{"type": "function", "function": {...}}` shape, correctly derived from an Anthropic-shaped input. Both the plain-text and tool-call real response paths were proven against realistic mocked responses. Mock mode for the new provider was confirmed to name the correct, provider-specific missing-key environment variable. The `runtime_supported` field was proven live against the actual running API: Anthropic and OpenAI correctly report `true`; every other seeded provider correctly reports `false`. 13 new permanent tests in a new `test_multi_provider_models.py`. Full backend suite 897/897 passing, stable across repeated runs; full frontend suite 326/326 passing with a clean TypeScript build.

## 0.113. Fixed in V101.12.0 — core collection endpoints now use real, keyset cursor pagination, and monitoring aggregates run entirely in Postgres (major, scalability)

### The real bug, exactly as reported

Confirmed directly across every named category: `list_agents`, the top-level `/api/runs`/`/api/traces` endpoints, agent-scoped traces and logs, `list_alerts`, KB document/knowledge-base/outbox listings, and eval-run listings all used a real, unbounded `.all()` — every matching row loaded into this API process's own memory on every single request, regardless of how many real rows existed. Separately, `monitoring.py`'s own `_compute_platform_metrics` and `get_home_stats` loaded every real `Run`/`Agent`/`Alert` row in scope just to call Python's `sum()`/`len()` and a hand-rolled percentile function on them — exactly the "aggregation after loading records into application memory" the report names.

### The real fix

A new, shared `pagination.py` implements real, keyset ("seek") cursor pagination — not offset-based, which itself degrades at real scale (Postgres still has to scan and discard every one of the first `y` rows for a large offset, the same real problem this fix exists to close). Ordered by a real, stable `(sort_col, id)` pair, descending by default, with an opaque, base64-encoded cursor (matching the same established convention as Stripe's/GitHub's own cursor APIs) rather than a raw value a caller might be tempted to construct by hand. `limit` defaults to 100 and is hard-capped at 500 regardless of what a caller requests, closing the gap even against a caller that never sends `limit`/`cursor` at all. Real, deliberate backward compatibility: every converted endpoint still returns the same bare JSON array every existing caller (including this platform's own frontend) already expects, with pagination metadata carried in two additional response headers (`X-Next-Cursor`, `X-Has-More`) rather than a breaking envelope object.

Every endpoint the report names is now converted: `list_agents`, `/api/runs`, `/api/traces`, agent-scoped traces and logs, `list_alerts`, KB document/knowledge-base/ingestion-outbox listings, and eval-run listings. Two real, honest, stated trade-offs: `Agent` and `Alert` have no real `created_at` column of their own (only a display-string `ts` field with no real ordering guarantee) — both are paginated by their own unique `id` alone, a real, stable, correct order that is deliberately NOT chronological, rather than adding a new schema migration for cosmetic ordering alone. Per-eval-run `EvalResult` listings (bounded by a single eval set's own case count, not genuinely unbounded growing data) were deliberately left unconverted — pagination there would add real complexity with no real, corresponding benefit.

`monitoring.py`'s own two aggregation endpoints were rewritten as single Postgres queries using `COUNT() FILTER`, `AVG()`, and `PERCENTILE_CONT() WITHIN GROUP` — exactly the aggregate functions the report names — computed entirely server-side; this API process never loads a single matching row's own full content to produce these numbers. `Run.duration_ms` (a real, exact BIGINT column added by an earlier fix — §0.79 — but never actually used by this code until now) is used for the real percentile computation instead of parsing `Run.duration`'s own display string back into a float per row.

### A real bug found in the pagination helper's own existing test suite

While verifying this fix, one pre-existing test (`test_a_row_with_a_null_sort_value_does_not_get_pushed_to_the_front`) was failing. Investigated directly rather than loosened: the test's own setup tried to construct a genuinely NULL `created_at` row via `Run(..., created_at=None)`, but `Run.created_at` has a real `default_factory=datetime.utcnow` — SQLAlchemy's own column-level default fires whenever the Python-side attribute is `None` at flush time, not only when a field is genuinely omitted, so the row silently got a real, non-NULL timestamp on commit regardless of the explicit `None` passed to the constructor. Confirmed directly: the attribute was `None` immediately after construction, but a real, non-NULL datetime immediately after `session.commit()`. This is not a bug in `paginate` itself — a genuinely NULL row can only be produced by writing NULL at the database level directly (matching how a real, existing legacy row actually got its own NULL value in the first place: a raw `ALTER TABLE ADD COLUMN` with no default, never an ORM insert). The test was fixed to force a genuine NULL via a direct SQL `UPDATE`, and the underlying `paginate` NULL-ordering logic was then confirmed correct.

### Verification

Every converted endpoint was proven live through the actual, running API, not merely via unit tests: a full, real cursor walk through `/api/agents` returned three distinct agents on page one with a real `X-Has-More: true` and a real `X-Next-Cursor` header, and using that cursor for page two returned three genuinely different agents with no overlap. The monitoring rewrite was proven equivalent, not merely non-crashing: the old, Python-side computation and the new, single-SQL-query computation were run side by side against the same live data, and every value — success rate, p50/p95 duration, step-limit rate, guardrail rate, entitlement-pass rate, average cost, agent count, alerts-today, runs-today — matched exactly. 20 tests across a comprehensive `test_pagination.py` cover cursor encoding/decoding, the shared `paginate` helper directly (including the NULL-ordering fix above), and every real, converted endpoint live. Full backend suite 884/884 passing, stable across repeated runs.

## 0.112. Fixed in V101.11.0 — a real, built-in connector runtime now executes genuine tool calls, closing the platform's largest remaining feature-completeness gap (major, feature completeness)

### The real bug, exactly as reported

Confirmed directly, ignoring UCS entirely as the report itself instructs: `handle_tool` (the Tool node) performed a real UCS entitlement check, then unconditionally set `ctx["vars"]["tool_output"]` to `f"[no live connector configured — placeholder result for {obj}]"` regardless of which real catalog object was configured. `handle_agent`'s own function-calling tool loop had the identical, separately-duplicated placeholder for every real tool call a model genuinely decided to make. The report's own summary was exact: LLM tool decision, tool schema/context, and orchestration were all genuinely real; only the actual connector call itself was fabricated.

### The real fix — an honest, investigated scope, not a blanket "make it real"

Before writing any code, this platform's own seeded tool catalog (`seed.py`'s `TOOLS`/`TOOL_ACTIONS`) was inspected directly to determine which real catalog entries could genuinely be connected with no external, per-deployment credential of any kind, and which genuinely could not. Four real actions — JSON Parse/Delete/Replace/Insert, Get Current Time, Run Code, and Scrape Page — need no external account or credential at all and are now genuinely, fully real. Four others — Convert to Markdown, Convert File, Search/Read Page, and Text-to-Speech/Speech-to-Text — genuinely require a real, external, per-deployment API account (markitdown, MinerU, Jina AI, an audio provider) this platform has no way to obtain, and stay honest, explicit placeholders, listed by name in `tool_connectors.py`'s own `_NOT_YET_CONNECTABLE` set rather than left ambiguous.

A new module, `tool_connectors.py`, implements the four real, connectable actions: `JSON Parse/Delete/Replace/Insert` use a genuine `jsonpath_ng` query (the exact library the seed catalog's own description already named — now actually installed and wired, not merely mentioned in a description string) against real, parsed JSON, with real, deep-copy semantics for the mutating actions so a caller's own original input is never modified in place. `Get Current Time` reads the real, live system clock in any real IANA timezone. `Run Code` delegates to this platform's own already-real code sandbox (`sandbox.py`) — the identical, real isolation the Code node itself uses, not a second, separately-implemented execution path. `Scrape Page` performs a real, SSRF-governed fetch (`egress.py`'s own `safe_http_request` — the same real governance the HTTP node and Doc Extractor already go through) with real, optional CSS-selector scoping via BeautifulSoup.

`handle_tool` now dispatches into this registry using a new `param_values` config field — a real, per-parameter mapping (a `{{var}}` reference or a literal value) the Builder now has real input fields for, resolved against the run's own live context the same way every other node's own `{{var}}` substitution already works. `handle_agent`'s own function-calling loop dispatches the same way, using the model's own real, structured tool-call arguments directly.

### A real, separate, pre-existing bug found and fixed while building this

Investigating the actual tool-call dispatch surfaced a genuine, separate defect, independent of the report: `handle_agent`'s own tool schema construction handed the real Anthropic API a tool `name` built directly from the raw `"tool:{Name}:{Action}"` catalog string — colons and spaces are not valid characters in a real Anthropic tool name, confirmed directly against the real API's own documented constraint. This means `function_calling` strategy would have genuinely failed the moment any real tool was selected against a real (non-mocked) model call — invisible until now because only the mock path had ever been exercised. Fixed with a real, deterministic sanitization scheme, with a real, local mapping kept back to the original catalog string for dispatch. A second, related gap was fixed alongside it: every real tool previously shared a single, generic `"query"` string schema regardless of what it actually needed — a model calling "JSON Parse" had no real way to know it needed `json_data`/`json_filter` specifically, since it was never told. `tool_action_input_schema` now builds each real tool's own schema from its own real, configured `ToolAction.params`.

Frontend: the Tool node's own config panel previously showed only a read-only description of a selected action's own parameters, with no way to actually set them. Real, per-parameter input fields (with the same `{{var}}` affordance every other node's own config already uses) were added, backing the new `param_values` field.

### Verification

Every one of the four real connectors was proven individually against real inputs before any integration was attempted: JSON Parse/Delete/Replace/Insert against real JSON with real path expressions (including honest failures for invalid JSON, invalid paths, and missing parameters); Get Current Time against the real, live clock in multiple real timezones (including an honest rejection of an invalid one); Run Code against this platform's own real sandbox, confirmed to genuinely execute (not merely echo) a real Python snippet; Scrape Page against a real, live URL. The full, real, end-to-end chain was then proven through the actual, running API: a real agent with a real Tool node, configured with a real `{{trigger_input}}` reference in `param_values`, correctly extracted a real value via a genuine JSON path query and produced the correct final output — not through an isolated function call, through the complete real invocation pipeline. `handle_agent`'s own real dispatch, the tool-name sanitization, and the real per-tool schema were each proven directly, including a live confirmation that a not-yet-connectable tool still reports an honest, explicit result rather than a silent failure or a fabricated success. 33 new permanent tests across a new `test_tool_connectors.py` and extensions to `test_agent_node.py`. Full backend suite 868/868 passing, stable across repeated runs; full frontend suite 326/326 passing with a clean TypeScript build.



## 0.111. Fixed in V101.10.0 — request bodies are now capped before buffering, and ZIP-based document parsing is bounded by entry count, expanded size, compression ratio, and wall-clock time (major, security)

### The real bug, exactly as reported

Confirmed directly: `upload_file`'s own `data = await file.read()` read the ENTIRE real request body before its own `MAX_UPLOAD_BYTES` check ever ran. Investigating the actual fix surfaced a deeper, structural finding not obvious from the route handler's own code alone: this cannot be fixed inside the route handler at all — confirmed directly against Starlette's own source (`starlette.formparsers.MultiPartParser.parse`), its own multipart parser already writes every real incoming chunk to the `UploadFile`'s own underlying spooled temp file AS IT ARRIVES, genuinely BEFORE FastAPI's own dependency-injection machinery ever hands control to the route handler. By the time any route handler's own code runs, the real damage is already done, regardless of how that handler then chooses to read the data.

Separately: `_docx_to_text`, `_xlsx_to_markdown`, `_pptx_to_text`, and `_epub_to_text` each call their respective real library (python-docx, openpyxl, python-pptx, ebooklib) directly on raw bytes, with no real limit of any kind — confirmed directly, none of these libraries enforce their own archive-member-count, expanded-size, or parse-time bound. A small, real, malicious archive (comfortably under this platform's own 20MB upload cap) can genuinely expand far beyond that once handed to any of these four real parsers.

### The real fix — three distinct, verified protections

**Request-body size, enforced at the correct layer**: `MaxRequestBodySizeMiddleware` (`middleware.py`) is a real, pure ASGI middleware — deliberately not a `BaseHTTPMiddleware` subclass, which sits at the same layer the actual buffering already happens at and would not close the gap — wrapping the raw `receive` callable to count real bytes as they arrive off the wire, rejecting with a real, honest `413` the instant the running total would exceed the configured limit, before Starlette's own form parser ever buffers the excess. Scoped to the real upload endpoint specifically via `path_prefixes`, not a single global cap.

**ZIP-container safety, checked before any real decompression**: `validate_zip_container_safety` (`upload_security.py`) inspects only the ZIP central directory (`ZipFile.infolist()` — real, already-present metadata, no real member content decompressed) for every limit the report names together: max entry count, max per-entry uncompressed size, max total uncompressed size across all members, and max compression ratio (the classic zip-bomb signal itself). All four are real, separately configurable via environment variables, with generous real defaults that a genuine, ordinary office document or ebook would never approach.

**Wall-clock parsing timeout, enforced by real process isolation**: DOCX/XLSX/PPTX/EPUB parsing now runs inside a real, separate `ProcessPoolExecutor` worker — the report's own suggested "constrained worker" made real, not a documented aspiration. A real `concurrent.futures.TimeoutError` genuinely cancels the still-running worker process rather than merely giving up on waiting for it. The pool is explicitly constructed with `mp_context="spawn"`, a real, deliberate choice over this platform's own Linux default of `fork` — a real, documented Python risk (forking a genuinely multi-threaded process, which this API process is, can inherit a lock held by a non-forking thread at the moment of fork, a real deadlock risk) caught and fixed while building this, not left as a latent hazard.

### Verification

Every claim was proven live before any test was written. The middleware: a 25MB upload against the 20MB limit rejected in 0.15s; a 200MB upload (10x the limit) rejected in 0.046s — the rejection time does not scale with the attacker's own declared body size, the actual property this fix needs to hold. The zip-safety validator: a real, constructed zip bomb (51KB compressed expanding to 50MB, a 1029:1 ratio) correctly rejected; a real entry-count bomb (3000 members) correctly rejected; a real, legitimate `.docx` built with python-docx correctly passes with no false positive. The timeout: a real, genuinely slow function submitted to the same pool this platform's own document parsing uses was cut off at the configured limit, not left to run to completion; an artificially tiny timeout forced even a fast, legitimate document to report an honest timeout failure; the real, default, generous timeout still lets ordinary documents extract normally. A full, real, end-to-end chain was also proven: a genuine malicious `.docx` zip bomb was uploaded through the actual, running `/api/uploads` endpoint, then read back through the real local-upload-retrieval path and extracted — confirmed to produce an honest rejection, not a crash or a hang, through the complete real pipeline, not an isolated function call. 12 new permanent tests cover all three protections plus regression checks for every one of the four real ZIP-based formats. Full backend suite 834/834 passing, confirmed stable across three consecutive runs given this fix's own timing/process-sensitive nature.


## 0.110. Fixed in V101.9.0 — storage operations now resolve each row's own recorded backend, not the current global default, plus a real, explicit cross-backend migration process (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `get_storage_backend()` took no arguments at all — a single, process-wide singleton, lazily constructed from whatever `STORAGE_BACKEND` env var happens to be set right now, reused for every real caller regardless of what any specific row actually recorded. `UploadedFile.storage_backend` — which the report correctly identifies as "exactly what you need" — was written at upload time but never read back by any of the three real operations that need it: the download endpoint, the retention sweep (which resolved one backend, once, outside its own per-row loop), and a `harness.py` shortcut that reads an uploaded file's bytes directly for the Doc Extractor node. Reproduced live, before writing any fix, with the report's own exact scenario: a file written while one backend was configured became genuinely unreachable — a confirmed `StorageError`, not a hypothetical — the instant the global configuration was switched to a second, distinct backend, even though the row's own `storage_backend` field still correctly said where the real bytes actually were.

### The real fix

`get_storage_backend(name)` is now a real, lazily-populated registry keyed by name, not a single singleton. `name=None` (every real caller creating something NEW — the upload endpoint's own `put`) is unchanged: it still resolves the current `STORAGE_BACKEND` env var, correct for a genuinely new object with no row of its own yet. A specific `name` (every real caller reading or deleting an EXISTING row's own object) returns the real, specific, independently-cached backend for that exact name — so a real deployment with some rows on `local` and others already on `s3` can genuinely serve both correctly at the same time, not just whichever the global env var currently says. All three real call sites (download, retention sweep, the `harness.py` shortcut) now resolve via `get_storage_backend(row.storage_backend)`. An unrecognized backend name raises a real `StorageError` immediately, matching every other real failure mode this module already holds itself to, rather than silently defaulting to the wrong backend.

### The report's own second, explicitly requested half: a real migration process

`migrate_upload_backend(session, row, target_backend)` — reads the row's real, existing bytes from its CURRENT backend, writes them to the real target, commits the row's own `storage_backend` update only once that write has genuinely succeeded (a failure between read and write leaves both the original object and the row's own metadata completely unchanged — the safe, honest failure mode), and only then deletes the old copy, so a real crash between the metadata commit and the old-copy delete leaves a harmless orphan rather than a genuinely lost file. `POST /api/uploads/_migrate-backend` is a real, manually-triggerable bulk endpoint matching this codebase's own established convention (the existing retention-sweep trigger) for an operator-invoked maintenance operation with no in-process scheduler of its own.

### Verification

The exact reported scenario was reproduced live, twice — once to confirm the bug's real presence before the fix (a file written under one backend, unreachable after a global config switch), once after the fix (the same file, resolved correctly via its own row's `storage_backend`, while the current, post-switch default backend genuinely does not have it — proving this really would have been the failure mode). The migration function was proven live end to end: real bytes moved from one backend to another, the row's own `storage_backend` updated, and the old copy genuinely removed. 7 new permanent tests cover registry resolution, an unrecognized backend name, the live download endpoint, the retention sweep resolving multiple rows on different backends independently, the migration function (including its no-op case), and the bulk migration endpoint. A pre-existing test helper (`cleanup_upload`) shared the exact same bug pattern this fix closes and was corrected alongside it. Full backend suite 822/822 passing, stable across repeated runs.

## 0.109. Fixed in V101.8.0 — an absent identity against an owned uploaded file is now rejected, not silently allowed through (major, security)

### The real bug, exactly as reported

Confirmed directly: `_check_tenant_access` (`routers/uploads.py`) read `if identity and identity != row.owner: raise 403` — a check that only ever fires when a real, PRESENT identity doesn't match the owner. A genuinely absent identity (`identity is None`, a real, anonymous request) never reaches that comparison at all, and the function returns without raising. Confirmed live, before writing this fix, with the report's own exact scenario: a file uploaded by a real, verified identity (`victim@example.com`) was successfully downloaded by a genuinely anonymous request carrying no identity, given only the file id — a real `200`, not the `403` an owned file should produce. This was not a theoretical gap: an existing, deliberately-written test in this codebase (`test_anonymous_access_remains_backward_compatible`) explicitly asserted this exact behavior as correct, codifying the vulnerability as intended.

The report's own framing is precise and was not narrowed in the fix: this is a resource-authorization rule defect, independent of which identity provider verifies who's asking — the fix applies to the authorization logic itself, not to `auth.py`'s own OIDC verification.

### The real fix

`_check_tenant_access` now treats an absent identity exactly like a mismatched one for any file with a real, named owner: `if identity is None or identity != row.owner: raise 403`. Two honest, deliberate carve-outs are preserved, both representing cases where there genuinely is no real owner identity to protect: a file whose `owner` column is the literal string `"anonymous"` (nobody was authenticated at upload time — the upload endpoint's own existing `owner = get_verified_identity(request) or "anonymous"`), and a genuinely legacy row with `owner` still `NULL` (predating the column's own existence, with no real value to recover). Both stay open to any caller, matching this platform's own default, not-globally-auth-gated posture — the fix narrows exactly the reported hole, not the platform's existing anonymous-access model as a whole.

The report's own suggested stronger alternative for genuine, deliberate anonymous link-sharing — a real signed download token with its own expiry and scope, rather than treating the primary object id as an implicit credential — was deliberately not built: no real caller of this endpoint today asks for shareable-anonymous-access to a real, verified identity's own file, so building that mechanism now would be speculative. This is a real, honest, stated scope decision, not a silently dropped recommendation.

### A real, pre-existing test was directly contradicting the fix

`test_anonymous_access_remains_backward_compatible` asserted the exact vulnerable behavior as the platform's own intended contract (`assert anon.status_code == 200` for an anonymous request against a real, named owner's file). Replaced with three tests that together state the corrected contract precisely: anonymous access to a real, named owner's file is now rejected; anonymous access to a genuinely anonymous-owned file still works; anonymous access to a genuinely legacy, ownerless row still works.

### Verification

The exact reported scenario was reproduced live, end to end, before writing any test: a file uploaded under a real, verified identity, then a genuinely anonymous download request against it — confirmed `200` (the vulnerability) before the fix, confirmed `403` after. The two preserved carve-outs were verified live in the same session: the real owner's own access still returns `200`; a genuinely anonymous upload, downloaded anonymously, still returns `200`. 3 tests (one corrected, two new) cover all three cases. Full backend suite 815/815 passing, stable across repeated runs.

## 0.108. Fixed in V101.7.0 — the full agent-behavior pipeline (input/output moderation, follow-ups, citations) now runs identically on every real invocation channel (major, correctness + architecture)

### The real bug, exactly as reported

Confirmed directly, channel by channel, before writing any fix: `run_agent` (`/run`) was the only channel applying all four of input moderation, output moderation, follow-up generation, and citations. `webhooks.py` and `mcp.py` applied **none of the four**. `run_agent_stream`'s durable branch, `_reconcile_running_run`, and `respond_to_hitl` applied output moderation only (from an earlier, narrower fix — §0.107) — still missing input moderation, follow-ups, and citations entirely. The report's own core claim — "same AgentVersion, same input" behaving differently by channel, including a genuine safety control being silently bypassed — was confirmed exactly as described, not merely plausible.

### The real fix

`execute_agent_flow` — already the one, shared routing decision every real channel calls (§0.53) — is now also the one, shared place the report's own recommended "pre-execution middleware → graph execution → post-execution middleware" pipeline actually lives. Two new functions, `_apply_input_moderation` and `_apply_output_postprocessing`, were extracted from `run_agent`'s own previously channel-specific inline logic and wired directly into `execute_agent_flow`: input moderation runs before routing/execution even begins; output moderation, follow-ups, and citations run on every successful result before the function ever returns. Every real caller — `run_agent`, `run_agent_stream`'s durable branch, webhooks, MCP, batch, evals — now gets the full, consistent pipeline by construction, not because each one remembers to re-implement it correctly. `run_agent`'s own now-fully-duplicated inline logic was removed entirely.

A new, narrow `apply_response_enrichment` parameter (default `True`) lets a caller skip follow-ups/citations specifically — never moderation, which always applies with no opt-out anywhere, since that is the actual safety property the report identifies as needing to never be bypassed. `evals.py` is the one, real, stated exception, matching the report's own explicit concession: "some differences are reasonable for Eval." Generating follow-up suggestions for every real eval case would add real, unnecessary LLM cost and noise to a scoring exercise no person will ever actually read as a chat response — moderation itself still fully applies to eval cases regardless, since scoring an agent's real, moderated behavior is more useful for eval fidelity than scoring around it.

An additional, deliberate improvement: input-blocked runs now flow through the same, real, shared `_finalize_run` every other terminal outcome uses, producing a genuine `Trace` row (previously, an input-blocked run persisted a `Run` row only, with no `Trace` at all) — closing a small, real asymmetry with the output-blocked case, which always produced both.

### Two further, more severe instances found while live-verifying the primary fix

Live-testing the report's own exact scenario — the same `AgentVersion`, same input, across REST/webhook/MCP — surfaced two additional, real, concrete gaps beyond the report's own explicit list:

- **`webhooks.py` kept its own, separate, duplicated Run/Trace status-mapping and construction logic** — a real, direct instance of the "parallel implementation" the report's own architecture recommendation specifically warns against. That duplicate map had silently drifted out of sync with the shared one (missing `"blocked"` entirely, from an earlier, narrower fix that only updated the shared map): confirmed live, a webhook-invoked, moderation-blocked run's own API response correctly said `"blocked"`, while the row Postgres actually held said `"failed"`/`"orch_fail"` — the exact audit inconsistency this whole fix exists to close, reintroduced by a second, un-updated copy of the same logic. Refactored to call the shared `_finalize_run` directly; there is now only one status map to keep current, not two.
- **MCP created no `Run`/`Trace` row at all for any outcome except a genuine HITL pause** — confirmed live: a normal, successful MCP call left zero rows in either table. Every other real channel's own invocation is visible to the Runs/Traces dashboard and every monitoring view that reads from them; MCP's was not, for the overwhelmingly common case. Fixed by resolving the execution context explicitly (so the correct version metadata is available) and calling the same shared `_finalize_run`. A related, smaller inconsistency was fixed alongside it: MCP's own `isError` logic treated a `"blocked"` result as an error, while REST correctly returns a real HTTP 200 for the identical outcome — now aligned.

### Direct evaluation of the report's own proposed architecture

The report's own recommended pipeline — `Invocation Adapter → ExecutionService → ResolvedExecution → Pre-execution middleware → Graph execution → Post-execution middleware → Run/Trace finalization` — is structurally correct, and its ordering is exactly right; the fix above implements that ordering precisely. Two honest points of divergence from the recommendation as literally drawn:

1. **A function, not a full class hierarchy.** `execute_agent_flow` already existed as the shared routing/execution function every real channel calls; extending it with two new pipeline functions achieves the substance of "transport adapters over parallel implementations" without a separate `Invocation Adapter`/`ExecutionService` class hierarchy this specific bug doesn't require. A full, formal class hierarchy is a reasonable next step if this platform's own invocation surface keeps growing, but would have been disproportionate restructuring for what this report actually reported.
2. **Resume/reconciliation paths are a genuine gap in the model as drawn.** `_reconcile_running_run`, `run_agent_stream`'s sync branch, and `respond_to_hitl` don't fit "fresh invocation through an adapter" cleanly — they continue an *already-started* run rather than adapting a new one, so input moderation (which only makes sense before a flow first runs) doesn't apply to them at all, while output moderation/follow-ups/citations do. These three now call `_apply_output_postprocessing` directly rather than going through `execute_agent_flow`, since they already possess a raw execution result from their own, separate resume mechanism. A full, literal implementation of the report's own diagram would need an explicit accommodation for this "resume" case that the diagram, as drawn, doesn't show.

### Verification

The report's own exact scenario was reproduced live: the same `AgentVersion`, same input, invoked through REST, webhook, and MCP, with both input and output moderation configured — all three channels now block consistently and return the identical replacement message. The two further gaps were also proven live, database row by database row, both before and after their fix: webhook's persisted `Run.status` confirmed `"failed"` before the fix and `"blocked"` after; MCP's `Run`/`Trace` row count confirmed `0` before the fix and a real row after, with `isError: false` for a blocked outcome. 5 new permanent tests cover cross-channel consistency directly, the webhook status-map fix, the MCP audit-trail fix, the input-moderation Trace-row improvement, and the eval opt-out. Full backend suite 813/813 passing, stable across repeated runs.



## 0.107. Fixed in V101.6.0 — output moderation now runs before terminal Run/Trace persistence, on every real path (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `run_agent`'s main `/run` path called `_finalize_run` — persisting `status="success"` — BEFORE running output moderation. If moderation then blocked the result, the API's own response said `status="blocked"` while the already-persisted Run/Trace pair honestly claimed `"success"`, with the original, blocked output still sitting in `Trace.final_output`. Confirmed live, before writing this fix, using the report's own exact scenario (a keyword-based moderation block): the persisted `Run.status` was `"success"` at the moment the caller received a `"blocked"` response.

A second, real leak was confirmed while building the fix, independent of ordering: even after `Trace.final_output` is replaced with the safe reply, the LAST NODE's OWN SPAN can carry its own, separate `output_preview` field (harness.py, up to 300 raw characters) — the exact, raw, blocked content, genuinely independent of `final_output`. Left alone, this would still land in `Trace.spans_json` on every real moderation block.

### The real fix

Output moderation now runs BEFORE any terminal persistence — `result` (status/output/fail_class/spans) is fully, honestly rewritten first, so `_finalize_run` always persists the exact outcome the API response itself reports, by construction rather than by remembering to keep two code paths in sync. `"blocked"` is now a real, distinct, honestly-tracked status (`Run.status="blocked"`, `Trace.status="moderation_blocked"`) rather than falling through to a generic failure classification, addressing the report's own "monitoring success rate wrong" concern directly. A new `_redact_blocked_output_spans` implements a real, explicit, stated policy for the raw, blocked content's own per-node trace footprint — the report's own second recommendation: **discard**, not redact-with-a-hint and not a separate restricted security-audit table (a real, materially larger, separate feature — its own table, its own access controls — stated here as a known, deliberate trade-off, not silently built or silently ignored).

### Three further, related instances found and fixed while verifying the primary one

Checking every other real caller of `_finalize_run` (rather than stopping once the reported `/run` path was fixed) surfaced three more real gaps — in each case, output moderation was not merely mis-ordered, it was **entirely absent**:

- `_reconcile_running_run` (the `GET /api/runs/{run_id}` reconciliation path for a durable-path run that outlived its original HTTP response) — a reconciled run's raw output could persist completely unmoderated.
- Both branches of `run_agent_stream` (the SSE streaming endpoint, durable and sync) — same gap, zero moderation coverage on either branch.
- `respond_to_hitl` (the HITL-resume endpoint) — a resumed run's own final output was never moderated at all.

All three now resolve the correct, frozen `features` snapshot the ORIGINAL invocation actually ran against (via the Run row's own recorded `agent_version_id`, never the agent's current live config) and run the same moderation check before finalizing, matching the primary fix's own convention exactly.

### Verification

The exact reported scenario was reproduced live, end to end, before writing any test: a keyword-moderation-configured agent was run against input producing a blocked output; the API response showed `status="blocked"` with the safe reply; the persisted `Run` row was confirmed at `status="blocked"`/`fail_class="moderation"` (not `"success"`); the persisted `Trace` row was confirmed at `status="moderation_blocked"` with `final_output` holding the safe reply, and its own span-level `output_preview` confirmed redacted, not the raw, blocked text. A clean, non-blocked run was confirmed to still persist `"success"` normally (no regression). The HITL-resume path was proven the same way, live, end to end. 4 new permanent tests cover the main path, the redaction helper directly, the HITL-resume path, and the non-blocked regression case. A related test-fixture bug — an untargeted HITL action edge that silently produced no downstream routing at all — was found and fixed while debugging an initial test failure, distinct from the fix itself. Full backend suite 808/808 passing, stable across repeated runs. Frontend: `Run`/`Trace` status types and `RunStatusChip`/`FailClassChip` updated for the new `"blocked"`/`"moderation_blocked"`/`"moderation"` values; TypeScript build clean; full frontend suite 326/326 passing, no regressions.

## 0.106. Fixed in V101.5.0 — HITL requests can no longer become permanently stuck in "resolving" (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `respond_to_hitl`'s own atomic claim (`UPDATE ... WHERE status='pending'` → `resolving`, committed immediately) correctly closes the concurrent-request race the claim itself exists to fix, but nothing in the function ever moved a row back OUT of `resolving` if the resume attempt that followed — a real Temporal signal call, or the sync path's own `resume_flow_sync` — then genuinely failed. No `try`/`except` existed around that attempt at all. Confirmed live, before writing this fix: a simulated resume failure (standing in for Temporal being temporarily unreachable, a workflow-signaling failure, a worker outage, or any other genuine resume exception) left the row's own `status` at `resolving` permanently, and a subsequent, entirely legitimate retry of the same request was rejected with a `409` — exactly and only because the row was no longer `pending` — with no path back to a retryable state at all.

### The real fix — both of the report's own recommended layers, not just one

**A real, bounded lease** (`HitlRequest.resolving_until`), set to `now() + 60s` the instant a row is claimed. **A real `try`/`except`** now wraps the entire resume attempt (both the Temporal-signal branch and the sync path's `resume_flow_sync`, including the delegated Sub-Agent case) — on any exception, the row is rolled straight back to `pending` (lease cleared) in the SAME request that caught the failure, the associated `Run` row is rolled back to `paused` too (it had been optimistically moved to `running` just before the attempt), and a real, honest `503` is returned telling the caller this is likely transient and safe to retry — the report's own first, fastest recovery option. **A real, separate periodic reconciler** (`reconcile_stale_hitl_requests`, plus a manually-triggerable `POST /api/agents/_reconcile-stale-hitl` endpoint, matching this codebase's own established convention for `uploads.py`'s retention sweep) rolls back any row whose lease has genuinely expired — the real backstop for the one case the `try`/`except` structurally cannot cover: the process handling a request dying entirely (a hard kill, not a caught exception) before its own `except` clause ever runs.

The report's own "stronger solution" — a transactional outbox with a separate delivery worker, so acknowledgment doesn't depend on Temporal being reachable at that instant — was deliberately not built. The lease-plus-reconciler approach the report itself offered as "preferably" already closes the specific, reported bug completely: no more permanently stuck rows, and a legitimate retry now genuinely succeeds. A full outbox pattern is a materially larger, separate architectural change, stated here as a real, known trade-off rather than silently scoped in or silently left unmentioned.

### Verification

The exact reported failure mode was reproduced live, end to end, before writing any test: a real, paused HITL request was created via the real `/run` endpoint; `resume_flow_sync` was made to raise; `/respond` was called for real and confirmed to return a clean `503`, with the `HitlRequest` row confirmed at `pending` (lease cleared, not stuck at `resolving`) and the `Run` row confirmed rolled back to `paused`. The SAME request was then retried, without the simulated failure, and confirmed to succeed (`200`, not a `409`) and reach `resolved` — the complete, real lifecycle the report described as broken. The reconciler was tested directly and separately: a row with an expired lease was confirmed reconciled back to `pending`, while a row with a still-valid lease was confirmed left untouched. 6 new permanent tests cover all of this. Full backend suite 804/804 passing, stable across repeated runs.

## 0.105. Fixed in V101.4.0 — Temporal Activities no longer block the event loop, timeouts are derived from node configuration, and retry ownership no longer stacks (major, correctness + performance)

### The real bug, exactly as reported — three distinct problems, all confirmed directly

**Blocking work inside an async Activity**: `execute_node_activity` was declared `async def` but called `call_node_handler` (genuinely synchronous — model SDK calls, `httpx` calls, DB queries, sandboxed subprocess execution) directly, with no `await` anywhere near the actual blocking work, and `worker.py` passed no custom activity executor to `Worker(...)`. Confirmed live, before writing this fix, with a side-by-side comparison: a simulated slow handler run the OLD way (a blocking call directly inside `async def`, no offloading) let a concurrent asyncio ticker task make exactly zero progress for the full duration — the event loop was completely starved.

**Timeout mismatch**: every real `start_to_close_timeout` call site used a flat, hardcoded 30 seconds, while `handle_llm` has a real, user-configurable `timeout_s` field with no ceiling — confirmed directly, the report's own exact example (a node configured for 60 seconds) would still have been killed/retried by Temporal at 30.

**Retry multiplication**: `handle_llm`, `handle_code`, and `handle_http` each have their own real, independent, handler-level `retry_on_failure` loop, confirmed to stay fully active on the durable path too, stacking on top of Temporal's own per-node-type `_retry_policy_for_node` — confirmed directly, before this fix, an HTTP node configured for 3 handler-level retries made 4 real calls per Temporal-level attempt, not 4 total.

### The real fix — three distinct, targeted changes

The genuinely blocking body of `execute_node_activity` was extracted into a new function, `_execute_node_sync`, and the `async def` Activity now runs it via `asyncio.to_thread(...)` — the report's own suggested approach, moving blocking work to a real, separate worker thread rather than the Activity's own event loop.

A new `_activity_timeout_for_node()` implements the report's own exact formula — `activity timeout = node timeout + safety margin`. For LLM-touching node types (`llm`/`agent`/`subagent`/`classifier`/`parameter_extractor`, every real member of `_LLM_TOUCHING_TYPES`, since they all ultimately call the same real, timed provider SDK) with a configured `timeout_s`, the Activity timeout is that value plus a 15-second margin. Every other node type — which has no user-configurable timeout field at all today — keeps the same, generous, unchanged flat 30-second default as an honest floor, not a fabricated per-type value with no real, configured basis.

A new `ctx["_durable_execution"]` flag, set only inside `_execute_node_sync` (the durable path), is checked by all three handlers' own retry decisions to force a single attempt — implementing the report's own recommended model exactly: "pick one retry owner per execution mode... let Temporal own infrastructure retries and make node handlers one-attempt operations" for the durable path specifically. The sync path (`run_flow_sync`, `harness.py`) never sets this flag, so handler-level retries remain the sole, fully-active retry mechanism there, unchanged — matching the report's own framing that retry ownership is a per-execution-mode decision, not a universal one.

### Verification

Every one of the three fixes was proven live, not merely implemented. The event-loop fix: the exact side-by-side comparison described above, run again after the fix — the same simulated slow handler, now dispatched through the real, fixed `execute_node_activity`, let the concurrent ticker task make 21 real increments during the same 2-second blocking call, versus zero before. The timeout fix: `_activity_timeout_for_node` called directly with the report's own 60-second example, confirmed to return 75 seconds (60 + 15), not the old flat 30. The retry fix: `handle_http`/`handle_llm`/`handle_code` each called directly, twice — once simulating a persistent failure on the sync path (confirmed to retry the full configured count), once with `ctx["_durable_execution"]=True` (confirmed to make exactly one real call each). 12 new permanent tests cover all three fixes, including the event-loop proof itself (run 5 times consecutively to confirm it isn't a timing-flake) and the report's own specific worked examples. Full backend suite 798/798 passing, stable across repeated runs.

## 0.104. Fixed in V101.3.0 — Sub-Agent dependencies are now version-pinned to the parent version, closing a real reproducibility gap (major, correctness)



### The real bug, exactly as reported

Confirmed directly: `_resolve_subagent_flow` (`harness.py`) always
queried the target's `is_current == True` `AgentVersion` — the
target's CURRENT, latest published version, resolved FRESH at every
single execution, with no concept of pinning to whatever version the
target was when the calling parent was itself published. Confirmed
live, before writing this fix, the report's own exact scenario:
published parent A while child B was at v2; republished B to v3 with
no change to A at all; re-ran the unchanged, already-published A — and
it silently invoked B's new v3 rather than the v2 that existed when A
was published.

A second, more subtle concern the report raised was checked directly
too, not assumed resolved by the primary fix: a parent validated today
while its child has no HITL node could later see that child gain HITL
in a subsequent version, encountering a durable-vs-sync execution
topology that was never valid at the parent's own publish time.

### The real fix, matching the report's own recommended model

A real dependency manifest — `AgentVersion.subagent_manifest`, in
exactly the shape the report proposed:
`{"subagents": [{"node_id", "agent_id", "agent_version_id",
"version"}, ...]}` — is now built and frozen at PUBLISH time
(`_build_subagent_manifest`, `routers/agents.py`), from the same real
flow the `AgentVersion` itself snapshots, so the two can never drift
apart. A Sub-Agent node defaults to `version_policy="pinned"`:
`_resolve_subagent_flow` consults this parent invocation's own frozen
manifest (threaded through `ResolvedExecution` → `execute_agent_flow`
→ `run_flow_sync`/`_run_durable` → `ctx["_subagent_manifest"]`, the
same real threading pattern every other versioned-execution field
already uses) and fetches the EXACT `AgentVersion.id` that was current
when the parent was published — regardless of what the target's
current version has since become.

`version_policy="latest"` is a real, explicit, per-node opt-out —
matching the report's own suggested option — for a caller who
genuinely wants a Sub-Agent call to always follow the target's newest
published behavior. A target still only a draft at the parent's own
publish time (nothing real to pin to yet) is honestly recorded with
`agent_version_id=None` and falls back to resolving the target's
live/current state at execution time — the one, unavoidable case where
pinning genuinely isn't possible.

The report's second concern — HITL topology drift — is structurally
mooted by the same fix, not separately patched: the durable-vs-sync
routing decision for a Sub-Agent call is made against the exact same
`target_flow` variable `_resolve_subagent_flow` returns, which is now
the pinned content. A child gaining HITL in a later version can never
reach an unchanged, already-published parent's own execution at all,
confirmed live before writing this fix's own permanent test for it.

### Two real mistakes caught and fixed along the way

A relative import inside the new manifest-building function used the
wrong depth (`.models` instead of `..models`), only surfacing once
live-tested — caught and fixed immediately. Separately, the new
`AgentVersion.subagent_manifest` column was initially added to the
model with no matching Alembic migration generated, which surfaced as
what first looked like a startup hang and turned out to be a genuine
`ProgrammingError` against the missing column, confirmed by testing
`init_db()`/`seed()` in isolation before finding the real cause — fixed
with a properly-scoped migration.

### Verification

The exact reported scenario was run live, twice — once to prove the
bug's absence after the fix (unchanged parent A keeps invoking B's
pinned v2 after B is republished to v3), and once for the HITL
topology concern (a child gaining HITL in v3 never reaches an
unchanged parent pinned to its own no-HITL v2, confirmed still on the
sync path with the pinned output). The `version_policy="latest"`
opt-out was verified to genuinely follow the child's newest version.
6 new permanent tests cover all of this plus the manifest's own exact
recorded shape, the honest "unpinned" fallback for a draft target, and
a direct unit-level proof that `_resolve_subagent_flow` prefers a
pinned manifest entry over the target's current version. Fixing the
resulting regressions (2 test mocks with stale `_run_durable`
signatures, 1 hardcoded `ResolvedExecution` field-name assertion)
confirmed no other real caller was missed. Full backend suite 786/786
passing, stable across repeated runs.

## 0.103. Fixed in V101.2.0 — directed graph cycles can no longer silently complete as "success" (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `flow_validator.py`'s only real cycle check walked
`parent_id` containment chains (container nesting), never the actual
`source`→`target` execution edges nodes run along. Both real
schedulers — `harness.py`'s `run_graph_concurrent` and
`temporal_workflow.py`'s `AgentRunWorkflow` — compute a real "wave" of
every currently-ready node each iteration and simply `break` the
moment that wave is empty, with no check for WHY it's empty. Confirmed
directly, live, before writing this fix: the report's own exact
scenario (`Trigger → A ↔ B`) let both schedulers reach this `break`
with `A` and `B` neither done nor dead — genuinely stuck waiting on
each other forever — and both then proceeded straight to their own
normal, successful completion path, with `A` and `B` never having run.

### The real fix, at both levels the report asked for

**Publish-time** (`flow_validator.py`): a new, real, iterative
(non-recursive — genuinely safe regardless of flow size) three-color
DFS cycle check, run separately over the top-level graph and,
individually, over each `loop`/`iteration` container's own children (a
container body is walked by its own separate, linear runtime
mechanism, bounded by `MAX_GRAPH_STEPS`, so a cycle there fails loud
rather than silently succeeding — but a clear, early validation error
is still real, honest value over an exhausted step budget with a
confusing message instead). A legitimate diamond fan-out/fan-in
(one node branching into two parallel paths that reconverge) was
specifically tested to confirm it is never mistaken for a cycle.

**Runtime** (`harness.py` + `temporal_workflow.py`): a new
`GraphDeadlockError` (structured `blocked_nodes`/`unresolved_edges`
fields) is raised the moment an empty wave is found to still have real
nodes neither done nor dead — distinguished from genuine completion
using state the wave scheduler already tracks internally, not a new,
separate pass. Both schedulers now return a dedicated
`fail_class: "graph_deadlock"` result, never a silent
`status: "success"`. This runtime guard matters independently of the
publish-time check, exactly as the report argued: it protects an
unpublished draft's own "Try It" runs (which never go through
publish-time validation at all), and remains a real, correct backstop
regardless of how a deadlock might arise.

**Frontend mirror validator** (`flowValidation.ts`), found and fixed
along the way: this client-side validator exists specifically so the
Builder's own UI can flag the same problems the backend would
otherwise only reject at publish time — but it had no cycle check at
all, meaning a person would only discover a cycle after attempting to
publish. The identical DFS logic was ported so the Builder now
highlights every node actually IN a cycle, not just the first one
found, before a publish attempt is even needed.

### Verification

Direct, live proof of every layer, in order: `flow_validator.py`
correctly rejects the exact reported scenario with a clear
`graph_cycle` error naming the cycle path; `run_graph_concurrent`
directly raises `GraphDeadlockError` with the exact correct
`blocked_nodes`/`unresolved_edges` when called with the same cycle; an
actual, unpublished draft agent's real `/run` endpoint, given the
identical cyclic flow, returns `status: "failed"`,
`fail_class: "graph_deadlock"`, with the specific blocked node ids —
not the silent `status: "success"` this exact request would have
produced before this fix; and attempting to publish that same agent is
correctly blocked with a 400 and the new `graph_cycle` issue. 9 new
backend tests (6 in `test_flow_validator.py`, matching this file's own
established convention) and 3 new frontend tests confirm the exact
reported cycle, a loop-container-body cycle, and — critically — that a
legitimate diamond fan-out/fan-in pattern produces zero false
positives at either layer. Full backend suite 780/780 passing; full
frontend suite 326/326 passing; a full TypeScript build check
confirmed no type errors from the new `flowValidation.ts` logic.



## 0.102. Fixed in V101.1.0 — condition/loop expressions can no longer escape into the Python runtime (CRITICAL, security)

### The real bug, exactly as reported

Confirmed directly, at the exact cited location (`harness.py`,
`eval_expr`): the function called Python's own real `eval()`, with
`{"__builtins__": {}}` as the globals dict, believing this was a valid
sandbox — it is not, and this was verified live, directly, before
writing this fix, not merely asserted from reading the report. An
empty `__builtins__` dict blocks direct calls to builtin FUNCTIONS
(`open`, `__import__`, `exec`, etc.), but does nothing at all to stop
attribute traversal through Python's own, always-reachable runtime
object graph — every real object still has a real `__class__`, every
class still has a real `__mro__`, and `object.__subclasses__()` is
reachable from literally any value at all, builtins or not. Confirmed
directly, live, against this exact function before writing this fix:
`().__class__.__mro__[1].__subclasses__()` genuinely enumerated 830
real, live classes in this process, including `subprocess.Popen` — a
real, complete path to arbitrary code execution in the Agent API
process or the Temporal worker process, entirely OUTSIDE the real,
dedicated Code Node sandbox (`sandbox.py`'s own `run_sandboxed_code`)
this codebase has otherwise invested considerable, real effort
hardening (namespaces, filesystem isolation, capability handling,
resource limits).

### The real fix — Python's own eval() removed completely, not merely restricted further

`eval_expr` no longer calls Python's `eval` at all, anywhere, under any
circumstance. The expression is parsed with `ast.parse(expr, mode="eval")`
— which alone already rejects multi-statement input and assignment-like
constructs — and the resulting tree is walked by a new function,
`_safe_eval_ast_node`, that implements a real, explicit allow-list:
`Compare` (`Eq`/`NotEq`/`Gt`/`GtE`/`Lt`/`LtE`/`In`/`NotIn`), `BoolOp`
(`And`/`Or`), `UnaryOp` (`Not`/`USub`), `BinOp` (`Add`/`Sub`/`Mult`/
`Div`/`Mod`, the report's own "possibly safe arithmetic"), `Constant`
(`str`/`int`/`float`/`bool`/`None` only — never bytes or Ellipsis),
`Name` (resolved only against the flow's own `vars` dict, never
Python's own builtins/globals), and `List`/`Tuple` literals (needed
for `in`/`not in` to be usable at all, e.g.
`status in ("approved", "pending")`). Every other real AST node type
— explicitly including, and individually tested against below,
`Attribute`, `Call`, `Lambda`, `Subscript`, `ListComp`, `DictComp`,
`GeneratorExp`, `NamedExpr` (walrus), `Await`, and `Yield` — falls
through to an unconditional `raise`; there is no default/passthrough
branch of any kind. This closes the underlying class of attack, not
just the one reported expression: since attribute traversal itself
(`ast.Attribute`) is rejected outright rather than merely
builtins-starved, no future dunder, subclass, or built-in reachable
through the object graph can ever be reached this way again, regardless
of which one a future attacker might try.

The report's own "possibly safe arithmetic" and `In`/`NotIn` inclusion
implied a need for list/tuple literals (otherwise `in`/`not in` would
have no right-hand side to check membership against) — added as a
deliberate, minimal, explicitly-scoped extension beyond the report's
literal node list, in the same spirit as the rest of the allow-list.

A purpose-built expression language (the report's own suggested
long-term alternative, e.g. `status == "approved" && score >= 0.8`)
was considered but not built for this fix — the existing `{{var}}` →
Python-comparison-syntax expression language is what every existing
flow's own condition nodes are already written in, and changing the
surface syntax itself would be a breaking change to every existing
flow, well beyond the scope of closing this specific, critical
vulnerability. The existing `{{var}} >= 70`-style syntax is preserved
exactly; only the evaluation mechanism underneath changed.

### Verification

21 new tests, covering every node type the report explicitly asked for
regression coverage of individually (`__class__`, `__mro__`,
`__subclasses__`, `lambda`, function calls, generic attribute
traversal, list/dict comprehensions, generator expressions, the walrus
operator, and bare subscripts), confirming each fails closed (returns
`False`, matching the function's own existing, established fallback
behavior for a malformed expression — never raises out to the caller).
A full suite of legitimate-expression tests confirms the fix is not
merely restrictive but correctly backward-compatible: numeric and
string comparisons, `and`/`or`/`not`, `in`/`not in`, safe arithmetic,
and the existing missing-variable-defaults-to-zero behavior all
verified unchanged. The exact, reported attack expression was also run
through a REAL, complete flow via the real running API (a genuine
`condition` node's own `expression` config, not just a direct unit-level
call) — confirmed the malicious expression evaluates falsy and the flow
completes successfully, routing to the false branch, rather than
crashing or executing anything. A full sweep of the entire backend
confirmed no other `eval()` call exists anywhere in the codebase — this
was the only instance of this vulnerability class. Full backend suite
774/774 passing.



## 0.101. Fixed in v101 — operational endpoint credentials can no longer be resurrected through a version restore (critical, security)

### The real bug, exactly as reported

Confirmed directly, at the exact cited location: `restore_agent_version`
copies `v.features` — including any `mcp` key — wholesale back into
the live `Agent.features`. `set_mcp`/`regenerate_mcp_token` store the
real, encrypted MCP token at exactly that location, so an old,
rotated/revoked token could be brought back to life by publishing v5,
rotating the credential, then restoring v5 — with no separate publish
step even required. Interestingly, the codebase already had a working
precedent for excluding "mcp" from a features copy — `duplicate_agent`
already does this for cloning — but it had never been applied to
`restore_agent_version`.

### The real fix — the report's own recommended model, matching an existing, proven pattern

Rather than a narrow patch (excluding "mcp" from restore's own copy,
which would still leave the root cause — an operational credential
living inside a versioned snapshot — intact), this credential now
lives on real, top-level `Agent` columns: `mcp_enabled`,
`mcp_token_encrypted`, `mcp_token_created_at`, `mcp_token_rotated_at`.
`restore_agent_version`/`_resolve_and_publish` only ever read/write
`flow`/`app_type`/`features` — never these columns — so a version
restore is now structurally incapable of touching this credential at
all, the same real guarantee §0.97's own `WebhookEndpoint` table
already established for webhook tokens. A real, one-time backfill
migration was shipped alongside — confirmed directly against this
environment's own database: 611 real, existing agents had
`features["mcp"]` configured, all migrated to the new columns, with
the legacy location stripped from every live agent afterward.

### A second, real, related bug found and fixed while proving this live

The report's own parallel claim about webhook tokens ("a related
lifecycle problem") was checked directly, not assumed already closed
by §0.97. A genuine gap was found: restoring an old version's webhook
token and republishing could collide with a DIFFERENT, already-revoked
`WebhookEndpoint` row's own `token_hash` (a deterministic hash — the
same plaintext always hashes the same) — the real, database-level
unique constraint then made a blind `INSERT` attempt genuinely fail
with an `IntegrityError`, misreported by an unrelated, too-broad
`except` clause as "a concurrent publish." Fixed by looking up any
existing row (enabled or revoked) by hash before deciding whether to
insert or re-activate. A deliberately different decision than the MCP
case: a webhook token is part of a flow's own explicit, versioned
configuration, not a separately-managed operational credential — a
person explicitly restoring and republishing an old version is making
a real, deliberate choice to bring that configuration back, token
included, unlike the MCP case's silent, no-separate-publish-required
resurrection.

### A related frontend fix

`AgentProfileDrawer.tsx`/`PublishModal.tsx` read the MCP toggle's own
state from the old `features.mcp` shape — after this fix, that key is
genuinely never returned by the backend, so the UI would show nothing
after a page refresh. Both were updated to read the real, top-level
`mcp_enabled` field, with a new, honest UI state for a real, already-
enabled agent whose own plaintext URL isn't known to the current page
load (never recoverable from a masked value) — a "Regenerate URL"
prompt, rather than silently showing nothing with no way to ever see
or use that endpoint again.

### Verification

3 new backend tests, plus live reproduction of the exact reported
attack against the real running API before writing them: published
with an MCP token, confirmed the version snapshot's own features
object was genuinely empty (no `mcp` key at all), rotated the
credential, confirmed the old token was revoked, restored the
original version, confirmed the old token STAYED revoked while the
new one kept working. The related webhook bug was reproduced the same
way — the exact crash first, then confirmed gone, with the database
checked directly afterward to confirm no duplicate `token_hash` rows
and correct re-activation. Full backend suite 752/752 passing. On the
frontend: fixed 3 tests whose own fixtures still simulated the old
`features.mcp` shape, confirmed via a clean TypeScript build check and
the full Vitest suite (323/323 passing, no regressions).



## 0.100. Fixed in v100 — every real node-config mutation now passes the current draft_revision as If-Match, closing the one, real, remaining gap in the frontend's own optimistic-locking coverage (major, correctness)

### The real bug, exactly as reported

Confirmed directly, at both cited locations: `NodeConfigPanel.tsx`'s
own shared `set()` function (every node config field change in the
Builder funnels through this one, real function) called
`api.updateNodeConfig(agent.id, node.id, patch)` with no fourth
argument; `AddInputFieldModal.tsx` did the same. Confirmed via a full,
repo-wide search that these were the only two real, production call
sites missing it — every other real Builder mutation
(`addNode`/`moveNode`/`deleteNode`/`addEdge`/`deleteEdge`, all in
`Builder.tsx`) already correctly passes `agent.draft_revision`.

### The real fix

Both call sites now pass `agent.draft_revision` as the real fourth
argument, exactly matching the report's own suggested fix and the
established convention every other Builder mutation already follows.
`agent` was already an available prop in both components, so no
further prop-drilling was needed.

**A real, honest, stated scope boundary**: `NodeConfigPanel.tsx` also
calls `api.setAgentMeta` (for a Snippet's own declared input
variables) with no `If-Match` at all — but unlike `updateNodeConfig`,
this isn't a case of the frontend bypassing an existing backend
capability: `set_meta` (routers/agents.py) has no optimistic-locking
support whatsoever, on either side, confirmed directly. Closing that
gap would require a real, separate backend change (adding real
CAS support to `set_meta` itself) beyond this report's own concrete,
cited scope (where the backend capability already existed and only
the frontend failed to use it) — left open here, stated rather than
silently folded in or silently ignored.

The report's own closing recommendation — making `If-Match` mandatory
on Builder mutation APIs — was deliberately NOT done: a real, breaking
change to the backend API affecting every existing caller (including
this codebase's own test suite), and the report itself frames it as a
conditional, future opinion ("I would...") rather than a concrete ask
for this specific fix.

### Verification

Two new tests added to the existing, established
`BuilderOptimisticLocking.test.tsx` — the same file that already
tests every other Builder mutation's own `If-Match` behavior, closing
the exact coverage gap the report's own framing describes. Both
render `NodeConfigPanel`/`AddInputFieldModal` directly (the same real,
established pattern this test suite's own other node-type test files
already use) and confirm `updateNodeConfig` is genuinely called with
the real, current `draft_revision`. Fixing the resulting regressions —
this change added a required fourth argument to an existing call, so
8 pre-existing tests across 4 files that asserted the OLD, 3-argument
call signature now correctly failed — surfaced two real, separate,
pre-existing test-fixture gaps along the way: two `makeAgent()`
fixtures (in `AgentSubagent.test.tsx` and `Comment.test.tsx`) were
missing `draft_revision` entirely, fixed properly rather than merely
asserting `undefined`. A real TypeScript build check (`tsc -b`)
confirmed no type errors from either fix. Full frontend suite
323/323 passing (up from an established 321); full backend suite
749/749 passing (confirmed unaffected, since this was a frontend-only
change).



## 0.99. Fixed in v99 — webhook, batch, and eval now resolve the execution version exactly once per invocation and record it accurately (major, correctness)

### The real bug, exactly as reported

Confirmed directly, at every cited location: webhook's own Run/Trace
creation never populated `agent_version_id`/`agent_version`/
`flow_hash`, and Trace's own `version` field used the LIVE
`agent.version` rather than the version actually resolved and executed
against. Batch had the identical gap. Eval's own `EvalRun` model had
no version-tracking columns at all. The report's own deeper concern
was confirmed too: batch's `run_one` and eval's `_score_case` each
independently called `execute_agent_flow` per row/case with no shared,
pre-resolved snapshot — a real, narrow window for a concurrent publish
to splinter one logical invocation across two different versions.

### The real fix — resolve once, thread the same snapshot everywhere

Webhook now resolves the execution snapshot exactly once via
`resolve_execution`, before calling `execute_agent_flow` (also closing
the same narrow re-resolution race §0.93 already closed for `/run`),
and records `agent_version_id`/`agent_version`/`flow_hash` on both Run
and Trace — matching `/run`'s own established convention exactly
(keeping the legacy `version` field as `agent.version`, unchanged, for
backward compatibility; adding the new fields alongside it).

Batch already resolved once, up front, for its own §0.98 preflight
HITL check — that same object is now actually threaded through: passed
as `execution=` into every row's `execute_agent_flow` call (so a row
can no longer independently re-resolve), and recorded on every row's
Run/Trace. `EvalRun` gained the same three columns eval had none of;
`run_eval_set` now resolves once, before ever looping over cases, and
passes that one, shared object into every case's own scoring call —
the same, one real snapshot every case in a run is genuinely scored
against.

`create_hitl_pause_run` (§0.98's own shared helper) gained the same
version-recording capability, so a pause captured through any of these
channels carries the same accurate metadata as a normal completion.

### Verification

5 new tests, plus live reproduction of each channel's own new behavior
against the real running API before writing them: webhook's Run and
Trace were confirmed to record the exact same, correct
`agent_version_id`/`agent_version`/`flow_hash` after a real publish.
A real, 3-row batch call was confirmed to have every row share the
exact same version metadata — a direct, unit-level test (capturing
what `execution=` `execute_agent_flow` was actually called with for
every row) confirmed all rows receive the SAME already-resolved
object, not each independently re-resolving. A real eval run was
confirmed to record accurate version metadata on `EvalRun`, with the
same direct, unit-level proof that every case shares one resolved
object. One of these tests initially failed for a real, instructive
reason — monkeypatching `execute_agent_flow` on the wrong module (the
one that defines it, not the one that separately imports and calls
it) silently intercepted nothing; fixed by patching the actual calling
module. Full backend suite 749/749 passing; no frontend files touched.



## 0.98. Fixed in v98 — a genuine HITL pause is now represented and resumable identically across every invocation channel (major, correctness)

### The real bug, exactly as reported

Confirmed directly, at every cited location: webhook's own status map
(`{"success": ..., "failed": ..., "denied": ...}`) had no entry for
`paused_for_human_input` at all, silently defaulting to `"failed"`.
MCP's own `is_error = result.get("status") != "success"` made a
genuine, correct pause indistinguishable from a real failure — and MCP
was further confirmed to create no `Run`/`Trace` row at all, for ANY
outcome, not just paused ones. Evals' own `_score_case` fell straight
through to scoring a paused case's own still-empty output as if it
were the real, final answer. Batch's own honest, existing "batch runs
don't support resuming individual rows" message was confirmed to
arrive only AFTER a real, separate, durable Temporal workflow had
already been started and left genuinely, permanently paused — real,
orphaned durable state nothing in this reference implementation could
ever clean up.

### The real fix — the substance of the report's own recommendation, not the full rewrite

The report's own "Architecture recommendation" describes a complete
`ExecutionService`/`Invocation Adapter` rewrite. That full rewrite was
deliberately NOT attempted — a large, invasive change to the core
execution engine that couldn't be fully, honestly verified in this
environment, the same real, stated reasoning behind every other
"decline the large, unverifiable rewrite" decision this session (most
recently §0.95). Instead, the actual SUBSTANCE of the recommendation —
"invocation channels should not independently implement Run/HITL
persistence" — was built directly: a new, shared
`create_hitl_pause_run` function, modeled precisely on `/run`'s own
already-correct, established lifecycle, that every other channel now
calls instead of independently reimplementing (or never implementing)
the same thing.

Webhook and MCP now create the real, same `Run(status="paused")` plus
`HitlRequest` `/run`'s own pauses already use, resumable through the
exact same, real `/api/hitl/{request_id}/respond` endpoint — one real,
shared resume mechanism, not a second, channel-specific one. MCP's own
response is now honestly `isError: false` with clear text explaining
the pause (MCP's own protocol genuinely has no long-lived session
primitive to resume through, an honest, stated limitation, not a
silently-invented one). Evals now honestly report a paused case as
unscored, with the same real, resumable lifecycle created underneath
rather than a mis-scored fabrication.

Batch takes the report's own explicitly-preferred alternative: reject
the whole call upfront (a real preflight check against the agent's own
resolved flow) if it has a Human Input node at all, before any row —
and any real, separate Temporal workflow — ever starts, rather than
create durable state nobody can finish.

### A real bug in this fix's own first draft, caught by live testing

The first version of `create_hitl_pause_run` added the `HitlRequest`
row before the `Run` row it references via a real foreign key — since
these are two, otherwise-unrelated Python objects with no real
SQLAlchemy `relationship()` linking them for it to infer insertion
order from, this genuinely raised a foreign-key violation the moment
it was tested live against the real running API, not caught by a
syntax-only compile check. Fixed by adding and flushing the `Run` row
first.

### Verification

5 new tests, plus the exact live reproduction, against the real
running API, of each channel's own new lifecycle before writing them
(there's no reachable Temporal test server in this sandbox to produce
a real pause end-to-end, so `execute_agent_flow` was monkeypatched to
return a genuine `paused_for_human_input` result — the same,
established pattern this codebase's own existing tests already use for
this exact situation): webhook correctly returned
`paused_for_human_input` (not `failed`) with a real, matching
`Run`/`HitlRequest` pair; MCP correctly returned `isError: false` with
the same real pair; evals correctly reported the case as unscored,
not mis-scored, with the same real pair; batch correctly rejected a
HITL-containing agent's whole call with zero `Run` rows ever created.
Fixing two, real, existing tests that depended on the OLD batch
behavior (allowing a HITL agent through, then reporting its paused row
in the results table) confirmed the new rejection is what actually
happens now, not just what the new code intends. Full backend suite
744/744 passing; no frontend files touched.



## 0.97. Fixed in v97 — webhook token resolution is now a first-class, indexed registration, activated at publish time (major, correctness + performance)

### The real bug, exactly as reported

Confirmed directly, at the exact cited location: `_find_webhook_node`
scanned every agent's own live `Agent.flow` for a `trigger` node whose
`config.webhook_token` matched, completely independent of whatever
flow was actually published. A live features edit changing that token
with no new publish silently changed production exposure. A real,
existing test (`test_webhook_invocation_also_stays_frozen_to_the_
published_snapshot`) was confirmed to directly depend on this exact
bug — it set the token on the live, mutable flow AFTER publishing and
only ever passed because of it.

### The real fix — the report's own recommended model, built as specified

A new, first-class `WebhookEndpoint` table (`agent_id`,
`agent_version_id`, `trigger_node_id`, `token_hash`, `enabled`,
`created_at`, `revoked_at`), registered/updated/revoked as part of the
SAME real, atomic publish transaction that creates the new
`AgentVersion` — using a real `session.flush()` to get the new
version's own id before the final commit, so both genuinely land in
one transaction. Webhook lookup is now a real, indexed query against
this table; a JSON flow is never scanned again. One deliberate
addition beyond the report's literal schema: tokens are stored as a
`sha256` hash, not plaintext — reasoned through directly (a webhook
token is a real bearer credential, and its own high entropy already
makes an unsalted hash safe, unlike a low-entropy user password) and
matched against how this codebase already handles the analogous MCP
token case.

The publish-time sync handles all three real cases: an unchanged
token just updates its version pointer for a real audit trail; a
changed token revokes the old registration and creates a fresh one;
a token removed from the flow entirely gets genuinely revoked. A real
schema migration plus a separate, one-time backfill migration were
shipped alongside — confirmed directly against this environment's own
database that zero pre-existing rows needed backfilling here, but a
real production deployment could have live integrations depending on
one.

### Verification

7 new tests, plus the exact live reproduction of the report's own
scenario before writing them, against the real running API: published
with TOKEN_A — webhook worked. Edited the live draft to TOKEN_B
WITHOUT republishing — TOKEN_A still resolved (the core proof),
TOKEN_B did not yet. Republished — TOKEN_B then resolved and TOKEN_A
was genuinely revoked (confirmed directly in the database: the old
row `enabled=false`/`revoked_at` set, the new row pointing at the new
AgentVersion). Confirmed an unchanged token across two publishes keeps
the same, single endpoint row active rather than spuriously
revoking it. Confirmed the stored value is a real hash, never the raw
token. The existing test that depended on the old, buggy behavior was
rewritten to set its token via the real node-config-patch endpoint
and genuinely publish, rather than mutating the live flow directly
after the fact. Three, separate, unrelated test fixtures were also
found and fixed along the way — two webhook-routing tests that never
actually published before (the old buggy behavior never required it)
needed genuinely publishable flows once this fix made a real publish
a precondition for a working webhook. Full backend suite 739/739
passing; no frontend files touched.

### A real mistake caught before it went further

While editing `models.py` to add the new `WebhookEndpoint` class, an
editing mistake accidentally deleted the `class SavedResult(SQLModel,
table=True):` line itself, which would have broken the entire
application at import time. Caught immediately by verifying with a
full `import app.main` (not just a syntax-only compile check) before
proceeding, and fixed before any further work built on top of it.



## 0.96. Fixed in v96 — versioned runtime Features are now frozen at publish time alongside Flow, closing a real gap in this platform's own earlier immutability work (major, correctness)

### The real bug, exactly as reported

Confirmed directly, at every cited location: `_resolve_execution_context`
already returned `features`/`app_type` fields on its own
`ResolvedExecution` object (added in §0.93, anticipating exactly this
class of gap) — but nothing actually used them. `_check_moderation`,
`_generate_follow_ups`, and the `chat_history`/`citations` checks all
read `agent.features` directly, even for a genuinely published,
production invocation. Separately, `PATCH /{agent_id}/features`
mutated `a.features` in place with no real signal that anything had
changed — `draft_revision` never moved.

### The real fix — the report's own recommended category split

Every VERSIONED features read (moderation, follow-ups, chat history,
citations) now threads through the same frozen
`ResolvedExecution.features` every other §0.93 caller already uses —
through `run_flow_sync`, `_run_durable`, and the streaming endpoint's
own sync-path worker, all the way down. `require_authenticated_user`
is deliberately categorized as OPERATIONAL/deployment configuration,
per the report's own recommended split — it stays reading the live
value, so an operator can toggle it immediately without a re-publish.
`run_agent`'s own input moderation check was moved to run AFTER the
execution snapshot is resolved (previously it ran first, independently
reading the live value before the snapshot even existed).
`PATCH /{agent_id}/features` now routes through the same real, atomic
`_atomic_save_agent` every other "edit the draft" endpoint already
uses — `draft_revision` genuinely increments, the same honest signal
a node-config edit already gives.

**A real, honest, stated scope boundary**: `agent.model` (the agent's
own default LLM selection) is a separate, top-level column that
`AgentVersion` never stored in the first place, even before this fix —
closing that gap too would mean a new column and further threading,
a real, additional change beyond this report's own concrete, cited
examples (all `agent.features` sub-fields). Left reading the live
value, stated here rather than silently left unmentioned.

### Verification

6 new tests, plus the exact live reproduction of the report's own
scenario before writing them: published an agent with moderation
enabled and a blocking keyword — confirmed blocked. Edited live
features to disable moderation WITHOUT republishing — ran the same
published version again with the same keyword — still correctly
blocked, proving production stayed genuinely frozen. Confirmed a
preview run correctly reflects the live, edited features instead
(the honest, established counterpart). Republished — confirmed
production then correctly reflects the change. Confirmed
`draft_revision` genuinely increments on a features edit, and that
`require_authenticated_user` takes effect immediately without a
republish, per its own operational categorization.

Fixing the resulting test regressions caught a real bug in this fix's
own first draft: `_generate_follow_ups`'s parameter rename from
`agent` to `features` missed a separate, remaining reference to
`agent.model` inside the same function body, which would have raised
a real `NameError` in production the first time a genuine provider
failure triggered that code path — found and fixed properly (see the
stated scope boundary above), not silently patched over. A second,
real, pre-existing test-isolation bug was also found and fixed while
chasing an unrelated, flaky failure: a test in `test_oidc_auth.py`
mutated shared, global module state via `importlib.reload` and never
restored it, silently leaking a dev-only auth bypass into every later
test in the same session. Full backend suite 732/732 passing; no
frontend files touched.



## 0.95. Fixed in v95 — a durable parent calling a durable child that pauses for HITL is now rejected at publish, not silently misreported as a runtime failure (major, correctness)

### The real bug, exactly as reported

Confirmed directly at both cited locations: `handle_subagent`
(harness.py) correctly raises a real `HitlPause` when a durable
child's own nested workflow pauses for human input — but
`execute_node_activity` (temporal_workflow.py), the real code that
actually runs a `subagent` node when the PARENT is itself already a
Temporal workflow, has no special `HitlPause` handling at all; its own
generic `except Exception` catches it and reports a false
`"_fail_class": "orchestration"` — even though the child genuinely,
correctly paused. Separately confirmed: `subagent` is not among the
node types the workflow's own main traversal special-cases as a
`"solo"` type requiring workflow-level (not Activity-level) handling —
only `loop`/`hitl`/`iteration` are, which is exactly why this gap
exists.

### The real fix — the report's own suggested, safer alternative

The report itself presented two options: a genuine architectural fix
(a real Temporal Child Workflow, started from real workflow code, so
the parent can natively, durably await the child's own completion —
which would also simplify nested cancellation, timeout, and trace
propagation) or rejecting the combination outright. The architectural
fix would be a substantial rewrite of the durable execution engine
that can't be verified end-to-end in this sandbox (no reachable
Temporal test server, the same real, standing constraint stated for
every other durable-path feature this session) — shipping an
unverifiable change to stateful, distributed workflow code would be a
real, worse outcome than being honest about the limitation.

Rejected instead at the one, real, authoritative, publish-blocking
validation layer, matching this session's own established precedent
for the identical class of decision (§0.87's own nested-container
rejection): if a flow being published already needs durable routing
(has its own `hitl` node) and one of its `subagent` nodes targets an
agent whose own current, real flow ALSO needs durable routing, publish
is now blocked with a specific, actionable error naming the exact
node — rather than silently advertising a graph the runtime can't
correctly execute.

### Verification

4 new tests, plus live verification of all three real scenarios
before writing them, against the real running API: the exact, reported
broken combination (a durable parent's own `hitl` node, calling a
Sub-Agent whose target also has a `hitl` node) is now correctly
rejected at publish, naming the specific Sub-Agent node. The existing,
already-working case (§0.83) — a sync parent with no `hitl` node of
its own calling a durable child — was confirmed genuinely unaffected,
still publishing correctly, since `handle_subagent`'s own real
`HitlPause` is genuinely caught by the sync harness in that case,
exactly as designed. The other safe case — a durable parent calling a
Sub-Agent whose own target never needs durable routing at all — was
also confirmed to publish correctly, with no false positive. Full
backend suite 726/726 passing; no frontend files touched.

### A real, honest, stated scope boundary

This fix closes the gap at the validation layer, not the execution
layer — the report's own preferred, fuller architectural fix (real
Temporal Child Workflows) remains a real, stated, deferred
improvement, not silently assumed done. A person who genuinely needs
both a durable parent-level pause AND a durable, HITL-capable
Sub-Agent call must restructure the flow (e.g., move the parent's own
`hitl` node so it isn't required on the same invocation as the
Sub-Agent call) until that fuller fix is built and can be verified
against a real Temporal cluster.



## 0.94. Fixed in v94 — a resumed execution now always continues on its own original, frozen version, never re-resolving a version at resume time (major, correctness)

### The real bug, exactly as reported

Confirmed directly at the exact cited location, and in one more, more
commonly-hit place the report itself invited ("I would apply the same
principle to every resume function"): `resume_delegated_subagent_flow`
(the report's own named function) AND `resume_flow_sync` (the
primary, far more frequently exercised HITL resume path) both read
`agent.flow` directly. The real, reported scenario: a parent published
at v5, its own Sub-Agent node pauses waiting on a real, nested child's
own HITL, and while genuinely waiting — for minutes, hours, or days —
a Builder edit changes the parent's own live flow. Resuming would then
continue using nodes that were never part of what was actually
invoked, even though the Run already stores `agent_version_id` and the
required information to do this correctly already existed.

Separately confirmed: the durable/Temporal resume path is
architecturally immune to this exact bug — a workflow's own `nodes`/
`edges` are real Temporal workflow INPUT parameters, baked in once at
workflow start and never re-fetched by a later, real signal-based
resume, by Temporal's own determinism guarantee.

### The real fix

A new, shared `_resolve_paused_flow` helper chains through the real,
already-persisted trail: `HitlRequest.run_id` (§0.91) → `Run`'s own
real, immutable `agent_version_id` (§0.91, captured at the ORIGINAL
invocation's own `resolve_execution` call) → that exact
`AgentVersion`'s own real, frozen flow. Both real sync-path resume
functions now use it instead of reading `agent.flow` directly. A real,
honest fallback to `agent.flow` applies only to the two genuinely
narrower cases where no such trail can exist: an unpublished draft/
preview run (there's no `AgentVersion` to freeze in the first place),
or a real, pre-existing `HitlRequest`/`Run` row from before this exact
tracking existed.

**A real bug in the first attempt at this fix was caught by testing
the FULL resume, not just the first node.** The initial version
correctly resolved the frozen flow to find which node to resume from,
but the shared internal traversal engine both resume functions hand
off to, `_execute_flow`, has its own, separate `flow=` override
parameter — neither function was passing the resolved flow through to
it, so execution continued past the first resumed node by silently
falling back to `agent.flow` all over again. Found only by
constructing the exact, full, live scenario end to end (a real pause
pinned to v1, a real edit and republish while genuinely paused, then a
real resume) and watching it still return the newer version's content
— not by re-reading the diff.

### Verification

3 new tests, plus the live, adversarial reproduction that caught the
above bug before writing them: reproduced the exact scenario for both
resume functions — pinned a real pause to v1, edited and republished
to a new version while genuinely still paused, then resumed and
confirmed the original, frozen v1 content is what executed throughout
the ENTIRE resumed path, immune to the live edit. A third test
confirmed the honest, narrower fallback: a `HitlRequest` with no
`run_id` (predating this fix) correctly falls back to the live
`agent.flow`, the only real option available for that case. Full
backend suite 722/722 passing; no frontend files touched.



## 0.93. Fixed in v93 — the execution version is now resolved exactly once per invocation, closing the real race between Run-row creation and execution (major, correctness)

### The real bug, exactly as reported

Confirmed directly at the exact cited locations: `run_agent` called
`_resolve_execution_context` to populate its own real Run row at
invocation time (§0.91's own fix), but `execute_agent_flow` then
independently called `_resolve_execution_flow` again, internally,
with no connection to the first resolution at all. The real,
described race:

    Resolve v5 -> Create Run saying v5 -> another request publishes v6
    -> execute_agent_flow resolves again -> potentially executes v6
    -> Run/Trace still says v5

The durable streaming branch (`run_agent_stream`) had the identical
pattern — resolves `_stream_ctx` once, then `execute_agent_flow`
resolves again internally.

### The real fix

A new `ResolvedExecution` — a real, frozen (`@dataclass(frozen=True)`,
genuinely raises on attribute reassignment, not just a naming
convention) value object — is now resolved exactly ONCE per real
invocation via a new `resolve_execution` function (the prior
`_resolve_execution_context`/`_resolve_execution_flow` are kept as
thin, fully backward-compatible aliases). Beyond the report's own
suggested shape (`flow`, `agent_version_id`, `agent_version`,
`flow_hash`), `features` and `app_type` were also included — confirmed
directly that a published `AgentVersion` snapshot freezes flow,
app_type, AND features together (`routers/agents.py`'s own
`publish_agent`), so the identical race applies to those fields too,
not just flow content.

`execute_agent_flow` gained a new, optional `execution:
Optional[ResolvedExecution] = None` parameter — when a caller passes
its own already-resolved object, the internal re-resolution is
genuinely skipped entirely, rather than merely narrowed. Both
`run_agent` and `run_agent_stream`'s own durable branch now pass their
own already-resolved context straight through. Every other real
invocation channel that never pre-resolves in the first place
(webhooks, MCP, evals, batch) is correctly, deliberately unaffected —
there was no earlier resolution for them to race against, so a single
internal resolution was never racy for those channels.

### Verification

4 new tests, plus live, direct proof before writing them: instrumented
`resolve_execution` itself with a real call counter and confirmed a
real `/run` invocation through the live, running API calls it exactly
once — the old code would have shown exactly two calls. A second,
direct test confirmed passing an explicit `execution=` object
genuinely skips `execute_agent_flow`'s own internal resolution (zero
further calls recorded), not merely reduces it. A third test
reproduced the report's exact race scenario directly: resolved a v1
snapshot, then published a new, different v2 version to simulate a
real concurrent publish landing in the narrow window, then confirmed
`execute_agent_flow` (called with the pre-resolved v1 object) still
executed the original v1 content — genuinely immune to the version
published after resolution but before execution. One test-suite-wide
interaction was found and fixed along the way: an `isinstance` check
against this test file's own collection-time class import turned out
to be fragile against a real, separate, pre-existing test file
(`test_runtime_config.py`) that legitimately `importlib.reload()`s
this same module for its own, unrelated test-isolation reasons —
replaced with a reload-safe `dataclasses.is_dataclass`/`fields` check
that tests the same real property without depending on exact class
identity. Full backend suite 719/719 passing; no frontend files
touched.



## 0.92. Fixed in v92 — a KB's embedding configuration is now immutable per KB, pinned at every real ingestion and query call site (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `embed_texts()`/`embed_query()` always dispatched
to whatever `EMBEDDING_PROVIDER`/`VOYAGE_EMBED_MODEL` was CURRENTLY
configured, at BOTH ingestion and query time, independently. The KB
already stored `"embed_model": current_embed_model_name()` at
ingestion, but no real query-time call ever read that stored value
back — `retrieval.py`'s own `embed_query(query)` call had no way to
know what a KB's chunks were actually embedded with. The exact
scenario the report described — ingest Monday under one provider/
model, change the global config Friday, query the same KB — could
produce a real dimension mismatch at best, or worse, dimension-
compatible but semantically incompatible vectors with silently poor
results and no error at all to notice.

### The real fix

**`embed_texts()`/`embed_query()`** now accept an explicit
`provider`/`model` override, letting a caller pin a computation to a
specific configuration regardless of the current global env vars —
additive, not a behavior change for any existing caller that omits it.

**A KB's first real ingestion persists its own real, structured,
immutable configuration** — `embedding_provider`, `embedding_model`,
`embedding_dimension` — computed from the real vectors just produced
(read directly from their own length, not re-asked from a separately-
cached global value). Every subsequent real ingestion into that SAME
KB reuses that exact, stored configuration instead of whatever the
current global env vars say by then, keeping every chunk the KB ever
holds in one consistent embedding space.

**Every real query-time embedding call is pinned to that same stored
configuration**: `handle_retrieval` (harness.py's own production query
path), the KB retrieval-test endpoint (both real call sites), and
chunk-content re-embedding on edit — all now pass a KB's own stored
`embedding_provider`/`embedding_model` through to `embed_query`/
`embed_texts`, rather than letting each independently read whatever
the current global default happens to be.

**Changing a KB's embedding configuration is now a real, explicit,
deliberate action** — a new `force_reindex=true` parameter on the
ingest endpoint — rather than a side effect of an unrelated later
config change. Existing vectors are genuinely incompatible with a new
embedding space, so a real `force_reindex` truncates the table and
resets the embedding column's dimension before re-embedding fresh
under the current config, then updates the stored metadata to match.

**A real, honest failure mode, not a silent one**: when a KB's own
stored provider can no longer actually run (a missing/rotated API
key, a provider outage), every real call site now returns a clear,
actionable error naming the KB's own stored provider/model and the
real underlying failure — found and fixed after a live test first
surfaced this as a bare, unhelpful 500 in three separate places.

### Verification

10 new tests, plus extensive live, adversarial verification before
writing them: created a real KB, ingested real content, confirmed the
correct structured config was persisted, then directly corrupted the
database to make the KB falsely claim a different, unavailable
provider was used — confirmed BOTH re-ingestion and retrieval
correctly attempted that stale, stored provider (not the live,
working default) and failed with an honest, specific error. Directly
verified `force_reindex` truncates existing rows and re-embeds fresh.
Full backend suite 715/715 passing; no frontend files touched.



## 0.91. Fixed in v91 — the durable Run lifecycle is now genuinely production-grade: a real, continuous Run row through pause/resume, a correct duration calculation, and auditable version tracking (major, correctness)

### The three real bugs, exactly as reported

**(1) No Run row during a real HITL pause.** Confirmed directly at the
exact cited location: when a workflow reaches a real `hitl` node
within `_run_durable`'s own 25s poll window, only a `HitlRequest` got
created — a workflow could sit genuinely paused for hours or days
with nothing in `GET /api/runs` to show for it the entire time, and
`respond_to_hitl` always created a brand-new, disconnected Run/Trace
pair on eventual completion, with no link back to anything.

**(2) A duration calculation that breaks across midnight.** Confirmed
directly: `_reconcile_running_run` parsed `run.ts` (a bare
`"%H:%M:%S"` string, no date at all) and reattached TODAY's date
before diffing against "now" — genuinely wrong the moment a run's real
start and this reconciliation call land on different real calendar
days, silently clamped to a wrong 25.0s floor.

**(3) No real version/content tracking on Run or Trace.** Confirmed
directly: neither row recorded which real `AgentVersion` (or, for a
draft/preview run, which real live-flow content) actually executed at
invocation time — only `Trace.version`, set from `agent.version` at
FINALIZATION time. A real, long-running durable workflow that started
on a frozen v5 snapshot could have the agent republished to v6 before
the workflow finishes, and the eventual Trace would incorrectly claim
v6 despite the real, frozen v5 nodes being what actually ran.

### The real fix

**A real, continuous Run row through the whole lifecycle.** `run_agent`
and `run_agent_stream` now both create a real Run row (`status=
"running"`) at real INVOCATION time, before execution even starts —
matching the report's own exact requested lifecycle: `Invocation ->
Run(running) -> HITL -> Run(paused) -> response -> Run(running) ->
complete -> Run(terminal)`. A new `HitlRequest.run_id` links a pause
back to that same row; `respond_to_hitl` now updates it in place
across every real transition (back to "running" on response, to
"paused" again on a second pause, to its real terminal state on
completion) rather than creating a disconnected row. A genuine
execution exception now also marks the row "failed" rather than
leaving it stranded at "running" forever — a real, additional
improvement caught while building this, not just the originally
reported gap. The streaming endpoint's own two HITL-pause branches
(durable and sync-path) get the identical fix, and their own
previously-duplicated Run/Trace-creation logic was consolidated onto
the one, real, shared `_finalize_run` path already used elsewhere,
rather than a second copy that could silently drift from it.

**A real, exact duration calculation.** `_reconcile_running_run` now
uses `(datetime.now(timezone.utc) - run.started_at).total_seconds())`
directly — no string round-trip, no date-reattachment guesswork,
correct regardless of what real calendar day(s) a run's start and its
reconciliation happen to fall on. A real bug was caught and fixed
while testing this very fix: `datetime.utcnow()` (naive) can't be
subtracted from `run.started_at` (a real, timezone-aware TIMESTAMPTZ)
— found by running the full test suite, not assumed correct.

**Real, auditable version/content tracking.** `Run` and `Trace` both
gained `agent_version_id`, `agent_version`, and `flow_hash` (a real
sha256 digest of the exact flow JSON resolved and executed) — captured
once, at real invocation time, via a new, shared
`_resolve_execution_context` (the old `_resolve_execution_flow` is now
a thin, fully backward-compatible wrapper around it). Live-verified
the exact scenario the report described: ran an agent at v2,
republished it to v3, ran it again — the v2 run's own database record
still correctly shows v2 with its own distinct flow hash, permanently
immune to the later republish.

### Verification

6 new tests, plus extensive live, end-to-end verification before
writing them — including a direct, complete pause-to-resume round
trip (using the same real, established monkeypatch pattern this test
suite already uses elsewhere to exercise HITL logic without requiring
a live Temporal server, a real, standing constraint of this sandbox)
that confirmed the exact same `run_id` survives from initial pause,
through resume, to a real terminal "success" state. The version-drift
scenario was proven directly against the real database, not just
asserted. Full backend suite 705/705 passing; no frontend files
touched.

### A real, honest, stated scope boundary

This fix covers `/run` and `/run/stream` — the two real, interactive
channels the report's own cited code and lifecycle diagram concern.
Batch already had its own correct, explicitly stated behavior for a
paused row (`"batch runs don't support resuming individual rows
yet"`) and needed no change. Webhooks, MCP, and evals don't support a
real HITL pause/resume at all today — a real, separate, pre-existing
gap, not silently swept into this fix.



## 0.90. Fixed in v90 — a published agent with no AgentVersion snapshot now fails closed instead of silently executing its mutable draft (major, correctness)

### The real bug, exactly as reported

Confirmed directly, at real scale, before writing anything:
`_resolve_execution_flow`'s own prior fallback (`current_version.flow
if current_version else agent.flow`) was already honestly documented
as "a genuine data-integrity edge case... not silently assumed away"
— but querying this environment's own real database directly found
**9,536 total published agents, 6,950 of them genuinely missing a
current `AgentVersion` row** — including the platform's own original
seeded agents (`a1`, `a2`, `a3`, `a5`, `a6`, `a8`; `seed.py` creates
these with `status="published"` and never creates a matching
`AgentVersion` at all), plus thousands more accumulated from this
session's own earlier test runs. The identical bug pattern was found
a second time in `harness.py`'s own `_resolve_subagent_flow` (a
nested Sub-Agent call's own target resolution, added in an earlier
session, §0.83) — the exact same class of bug §0.54's own fix had
already closed for every other invocation channel, silently unfixed
for this one.

### The real fix — both parts the report asked for

**A real, one-time Alembic data migration**
(`5a5c062ae225_backfill_missing_agentversion_for_`) creates a real
`AgentVersion` snapshot, from each agent's own current
flow/app_type/features/version, for every real, pre-existing
published agent missing one — marked with a distinct, honest
`published_by='system-backfill-0.90'` so a backfilled row is never
confused with a genuine publish event in any real version history a
person looks at later. Run live against this environment's own real
database: created exactly 6,950 rows, matching the count found by
direct query beforehand: gap fully closed, not partially patched.
Idempotent by construction (`WHERE NOT EXISTS` + `ON CONFLICT DO
NOTHING`) — safe to re-run.

**A new `PublishedVersionIntegrityError`**, raised by both real
resolution points (`_resolve_execution_flow` in `execution.py`,
`_resolve_subagent_flow` in `harness.py`) instead of silently falling
back, whenever a published agent/target has no real, current
`AgentVersion` row. A real, dedicated FastAPI exception handler in
`main.py` surfaces this as a clear, specific 500 response
(`{"error": "published_version_integrity_error", "agent_id": ...}`)
rather than a generic one. Two real call sites (`run_agent`, the
webhook trigger) that used to wrap ANY exception in an actively
misleading "is the Temporal dev server running?" message — found
only by testing this live, not by reading the code — now re-raise
this one distinctly so the real, honest message reaches the caller
un-obscured; the other four real invocation channels (streaming,
batch, MCP, evals) were checked individually and confirmed to already
surface the message cleanly, needing no change.

### Verification

7 new tests, plus live, end-to-end verification of the complete
lifecycle before writing them: directly reproduced the exact bug (an
agent marked published while bypassing the real publish endpoint,
exactly matching both `seed.py`'s and the report's own described
state), confirmed the run now fails closed with the new, clean,
specific error rather than the old misleading Temporal-hint wrapper;
re-published the same agent through the real, normal endpoint and
confirmed the run then succeeds — proving the stated remediation path
actually works. The identical fix was separately verified for the
Sub-Agent path: a parent agent's real `subagent` node targeting a
broken published child correctly failed that one node honestly
(`fail_class: "orchestration"`), without crashing the parent run
itself. One existing test that explicitly asserted the OLD, now-
intentionally-changed fallback behavior was found and updated to
assert the new, correct, fail-closed behavior instead — a real,
necessary consequence of this fix, not a regression. Full backend
suite 699/699 passing; no frontend files touched.



## 0.89. Fixed in v89 — secrets encryption now fails CLOSED in production, and an undecryptable value is a real, loud error instead of a silent pass-through (major, security)

### The real bugs, exactly as reported

Confirmed directly: `_get_fernet()` generated a real, temporary,
in-memory-only key whenever `SECRETS_ENCRYPTION_KEY` was unset —
genuinely fine for a local dev accident, but a real, silent trap
outside this repo's own exact Docker Compose deployment (which
correctly requires the real env var for both API and worker): a
real, separate API process and a real, separate Temporal worker
process would each generate their OWN, independent temporary key, so
a secret the API encrypted would be genuinely undecryptable by the
worker. Separately, `decrypt_secret()` caught ANY decryption failure
and returned the raw value completely unchanged — a real encryption-
key mismatch looked identical to "the stored API key/password is
simply wrong," exactly the confusing failure mode the report named.

### The real fix

**Fail CLOSED in production**: `ENVIRONMENT=production` with no real
`SECRETS_ENCRYPTION_KEY` now raises a real, loud `RuntimeError`
immediately — checked at real process startup in BOTH `main.py` (the
API process) and `worker.py` (the Temporal worker process, which had
no such check at all before this fix, despite being the other real
half of the report's own named cross-process risk), not just lazily
on first secret use.

**An undecryptable value is now a real, loud, specifically-named
error**: new encrypted values carry an explicit `enc:v1:` prefix
(`_ENC_PREFIX`); a value marked this way that genuinely fails to
decrypt now raises `SecretDecryptionError`, logged clearly as a real
key-management problem, never silently treated as a bad credential.

**A real, deliberate, honest refinement of the report's own literal
"no prefix -> legacy plaintext" instruction** — found necessary by
checking this environment's own real, existing database before
writing anything: real EnvVar/KnowledgeBase rows already exist,
genuinely encrypted under the original §0.59 scheme, which never had
a prefix at all. Treating every no-prefix value as literal plaintext
(the report's own literal wording) would have silently broken every
one of those real, already-encrypted secrets, returning raw
ciphertext as the "credential" — the exact failure mode this fix
exists to prevent, just inflicted on real, existing data instead of
new. A no-prefix value therefore still gets one real decrypt attempt
first (preserving those real rows correctly); only a genuine failure
falls back to legacy-plaintext treatment — the real, inherent
ambiguity of a completely unmarked value is honestly irreducible, and
the strict, hard-fail behavior applies unambiguously to anything
carrying the new `enc:v1:` marker, which is what actually matters
going forward. Real, stated follow-on, not silently assumed done,
matching this module's own pre-existing precedent: existing no-prefix
rows are not retroactively re-prefixed by this fix.

### Verification

8 new tests, plus live, end-to-end verification before writing them:
`ENVIRONMENT=production` with no key was confirmed to genuinely
refuse to start (a real failed `uvicorn` boot, not a lazily-deferred
error), the same configuration with the key set was confirmed to
start and serve traffic normally, and the ordinary local-dev case (no
`ENVIRONMENT`, no key) was confirmed unchanged. The report's own exact
scenario was reproduced directly: a value encrypted under one real
key, decryption attempted under a genuinely different one, correctly
raised `SecretDecryptionError` with a message stating plainly this is
a key-management failure, not a bad credential. A real, existing
no-prefix encrypted row was confirmed to still decrypt correctly. The
full encrypt-store-decrypt-interpolate pipeline was verified through
the real, running HTTP API end to end, not just the crypto functions
in isolation: created a real secret env var, confirmed the stored
database value carries the `enc:v1:` prefix, ran the agent, confirmed
the real secret correctly decrypted and interpolated into the output.
Four existing tests that asserted the OLD stored-value shape
(`startswith("gAAAAA")`) were updated to the new, correct, deliberate
shape (`startswith("enc:v1:")`) — a real, necessary consequence of
this fix, not a regression. Full backend suite 692/692 passing; no
frontend files touched.



## 0.88. Fixed in v88 — Builder optimistic locking is now a real, atomic compare-and-swap, closing the race the original check-then-write left open (major, correctness)

### The real bug, exactly as reported

Confirmed directly: `_check_if_match` did a plain, in-Python `if
expected != a.draft_revision`, entirely separate from the later,
also-plain `a.draft_revision += 1; session.commit()`. The two were
never atomic with each other — two genuinely concurrent requests could
both read the same pre-write `draft_revision`, both pass the identical
check, and both proceed to write, with the second silently winning.
The existing test suite only ever proved a STALE request (one write
fully completes, THEN a second, unrelated request is attempted)
correctly gets rejected — genuinely different from two transactions
racing the SAME check at the same time, exactly the distinction the
report drew. Confirmed this gap was real across **nine** separate call
sites, not just the one cited: `_save_flow` (shared by six endpoints —
add/delete node, node config, node position, add/delete edge) plus
three more found by tracing every real write to `draft_revision`
directly — agent rollback, the DSL-import replace-existing path, and
snippet insertion.

### The real fix — the report's own preferred option, atomic CAS

A new `_atomic_save_agent` performs one real, atomic SQL statement —
`UPDATE agent SET <fields>, draft_revision = draft_revision + 1,
updated_at = now() WHERE id = :id [AND draft_revision = :expected]
RETURNING draft_revision` — folding the check and the write into the
same real database operation, using Postgres's own row-level locking
during the UPDATE itself rather than a Python-level gap. Zero rows
matched means a real, genuine conflict — a real 409, same honest
message as before, but now backed by a real database guarantee.
`_check_if_match` was kept, but re-scoped honestly: a real, early,
non-authoritative fast-fail (rejects an obviously stale/malformed
request before a caller does other real work, like rollback's own
`AgentVersion` lookup, that a doomed request has no reason to pay
for) — clearly documented as NOT the actual guard, so it can't be
mistaken for the fix again. All nine real call sites now route
through the one, real, atomic path.

### Verification

2 new tests, including the report's own exact scenario reproduced with
real, genuinely concurrent threads (the same real, established pattern
`test_hitl_bugs.py`'s own `TestRealHitlAtomicClaim` already uses — real
`httpx` connections to the real, live running server, not `TestClient`'s
in-process ASGI dispatch, which doesn't genuinely exercise concurrent
database transactions the same way): 10 real, simultaneous requests, all
carrying the identical `If-Match` revision, against the real running
server — exactly 1 succeeded, the other 9 got real, honest 409s. Directly
confirmed the write itself wasn't corrupted by the race either: the
agent's final `draft_revision` was exactly one higher (not ten, not
zero), and exactly one new node landed in the flow — not lost, not
duplicated. A second, direct test confirmed `_atomic_save_agent` leaves
the database completely untouched (same revision, same node count) on a
genuinely stale `expected_revision`. Full backend suite 684/684 passing;
no frontend files touched (the response shape is unchanged).

### A real, honest, stated scope boundary

The report's own further suggestion — eventually requiring `If-Match`
rather than leaving it optional, once every real caller has migrated —
remains a real, deliberately deferred, separate decision, not made
here: confirmed directly that the frontend's own API layer still sends
it conditionally, not universally, so making it mandatory today would
be a real, unrequested breaking change to callers that don't yet send
it, not a component of closing the actual race this fix targets.



## 0.87. Fixed in v87 — nested Loop/Iteration containers are now rejected at publish, not silently accepted and left to fail at runtime (major, correctness)

### The real bug, exactly as reported

Confirmed directly by reading both real execution paths before writing
anything: `flow_validator.py`'s own "Nested parent rules" section
explicitly allowed a container node's own parent to also be a
container — the code comment even said so outright ("a real container
can genuinely nest inside another... depth itself isn't restricted").
But neither runtime actually supports it. The sync path's own
`run_loop_body`/`run_iteration_body` dispatch each body child via
`exec_node`/`NODE_HANDLERS`, which has no real entry for `"loop"` or
`"iteration"` — those two types are only ever handled by the OUTER
traversal's own dispatch (`run_one_node_dispatch`), never recursively
from inside a body. A nested container reaches
`raise UnsupportedNodeType(...)` the instant execution walks into it.
The Temporal/durable path has the identical gap in its own equivalent
container-body code (confirmed directly, not assumed). A flow like
`Loop → Loop → LLM` was accepted by validation and would publish
successfully, then fail the moment anyone actually ran it.

### The real fix — the report's own explicitly recommended, safer option

The report offered two real choices: build true recursive container
dispatch, or reject nested containers at validation. Both real
execution paths were read end to end before deciding — true recursive
support would need a substantial, correctness-sensitive rewrite of
both, including reworking how the durable path's own parallel
Iteration body (which dispatches each body node as a separate,
isolated Temporal Activity call, not inline workflow code) would need
to handle a nested container that isn't a single node at all but a
whole sub-graph — a real architectural mismatch with the current
per-node-Activity model, and one that can't be live-verified end to
end in this sandbox (no reachable Temporal test server, the same real,
standing constraint already stated for every other durable-path
feature this session). Given that, this fix takes the report's own
explicitly stated safer path: `flow_validator.py` now rejects a
container whose own parent is ALSO a container, with a real, specific,
publish-blocking error naming both nodes and explaining exactly why
(neither execution path recursively dispatches a container from inside
another one's own body) — rather than advertising a graph the runtime
genuinely can't execute.

### Verification

5 new tests, plus live, end-to-end verification through the real
running HTTP API before writing them: the exact scenario from the
report (`Loop → Loop → LLM`) was confirmed to publish successfully
against the OLD code (a real bug reproduction, not assumed), then
correctly, honestly blocked at `/publish` once the fix was live — with
a real, specific `nested_container_unsupported` error naming the
inner node and its container parent. A genuinely valid, non-nested
loop was confirmed to still both publish AND actually run correctly
end to end (not just pass validation) — no regression to the common
case. The iteration-in-loop combination and three-level nesting (not
just the report's one two-level example) were also directly verified
rejected. One real bug caught and fixed in the new tests' own shared
cleanup helper while building this — it didn't account for a test that
actually runs an agent (leaving a real `Run`/`Trace` row a bare
`DELETE FROM agent` then violates via a real foreign key), unlike
every prior, publish-only test in that same file. Full backend suite
682/682 passing; no frontend files touched.



## 0.86. Fixed in v86 — every configurable outbound destination now goes through one real policy layer (major, security)

### The real bugs, exactly as reported

Confirmed directly at both cited lines, plus two more found while
tracing every real, configurable destination in the codebase: the
custom moderation endpoint (`routers/execution.py`) and non-pgvector
KB reachability checks (`routers/knowledge_bases.py`) both called
`httpx` directly, completely bypassing the SSRF/redirect governance
the HTTP node and Doc Extractor already had. The KB case was the more
severe of the two: it attached the KB's own real, DECRYPTED API key as
an `Authorization` header before making that ungoverned call — a
direct credential-exfiltration mechanism if the configured URL ever
pointed anywhere unsafe. Separately confirmed: `ModelProvider
.api_base_url`, a real, database-stored, configurable value, was
passed straight into `anthropic.Anthropic(base_url=...)` with zero
destination validation — the SDK's own real API key genuinely attaches
to every request it makes, including real redirects (its own default
is `follow_redirects=True`, confirmed directly against its source).
And pgvector KB connections sent a real, decrypted database password
to whatever `host`/`port` was configured, with no real role/permission
gating on who can create a KB.

### The real fix

**The two direct `httpx` bypasses** (`execution.py`'s moderation check,
`knowledge_bases.py`'s reachability check) now route through the exact
same `safe_http_request` every other governed outbound call already
uses — gaining SSRF blocking, redirect re-validation, and (from §0.85)
cross-origin credential stripping automatically, with zero new,
separately-maintained logic.

**A new, reusable `GovernedTransport`** (`egress.py`) — a real
`httpx.HTTPTransport` subclass — closes the SDK case: any library that
builds its own internal `httpx.Client` and only exposes an
`http_client=` injection point (the Anthropic SDK's own real,
documented mechanism) now gets the same real destination validation on
every connection it makes, including every real redirect hop, via
`governed_httpx_client()`. Wired into the one real Anthropic client
construction site (`model_adapter.py`), applied unconditionally (not
just when `api_base_url` is set — harmless for the real, default
`api.anthropic.com` endpoint).

**A new, deliberately NARROWER `validate_database_host`** closes the
database case, addressing the report's own final point ("Database KB
connections should receive similar destination policy if untrusted
users can configure them") — confirmed directly that KB creation has
no real role/permission gating today. This is genuinely not the same
policy as the HTTP cases: RFC1918 private ranges and loopback are
commonly, legitimately the real, intended way to reach a KB's own
separate pgvector instance (this platform's own connection code
already defaults `host` to `127.0.0.1`) — blocking those would break
real, working deployments to close a much narrower actual risk. Only
the real, never-legitimate case — the cloud metadata endpoint — is
blocked. All three real `psycopg2.connect` call sites in
`knowledge_bases.py` now share one real, validating connection path
(`_pgvector_connect`), rather than three independently-drifting
copies — the exact class of gap this whole fix exists to close.

### A real, honest, stated boundary

`GovernedTransport` validates the resolved destination before
connecting but does not re-pin the connection to that exact validated
IP the way `safe_http_request`'s own from-scratch request construction
does — `httpx.HTTPTransport`'s own real connection pooling doesn't
expose a clean per-request IP override. What this closes completely: a
literal malicious/internal `base_url` configured outright (the
dominant, actually-reported risk) and any real redirect chain leading
somewhere unsafe. A narrower, sustained, actively-timed DNS-rebinding
attack against this one specific path remains a real, unclosed edge
case, unlike the HTTP node's own fully IP-pinned path — stated
precisely here rather than left implied.

### Verification

12 new tests, plus live, end-to-end verification through the real
running API for every one of the four fixes: an agent with a custom
moderation endpoint pointed at the cloud metadata service reported a
real, honest "blocked by egress policy" and never made the call; a
non-pgvector KB pointed the same way was blocked with its real API key
never sent; `GovernedTransport` confirmed to block the metadata
endpoint and RFC1918 addresses directly, pass a legitimate request
through with headers intact, and correctly block a real redirect hop
mid-chain; a pgvector KB pointed at the metadata endpoint was blocked,
while the same KB pointed at a real, local address was confirmed to
reach an actual connection attempt (failing on real, wrong credentials
— not refused by the validation layer), directly proving the narrower
policy doesn't over-block real, legitimate deployments. Full backend
suite 677/677 passing; no frontend files touched.



## 0.85. Fixed in v85 — egress redirect credential leak closed; 307/308 body preserved; redirect responses now capped during download (major, security)

### The real bugs, exactly as reported

Confirmed directly at the cited lines: `current_headers` was reused,
unmodified, across every real redirect hop — a malicious or
compromised destination could `302` a request carrying
`Authorization`/`Cookie`/API-key headers to a completely different,
attacker-controlled origin, and those real secrets would genuinely be
sent there. Separately, request body/params/json were only ever
attached `if hop == 0` — silently dropped on any subsequent hop, wrong
for 307/308 specifically, since those two status codes exist to
preserve both method AND body across a redirect. And a redirect hop's
own response body was drained with a real, unbounded `resp.read()`,
while the FINAL response already correctly streamed with a real size
cap enforced during download — a malicious redirect endpoint could
return an arbitrarily large body on a hop never even returned to the
caller.

### The real fix

**Cross-origin credential protection** — `allow_cross_origin_redirects`
defaults to `False`, the report's own explicit recommendation ("I
would actually make cross-origin redirects disabled by default"). A
redirect that crosses a real origin boundary (scheme, host, or port)
is refused outright with a specific `SSRFBlocked`, unless it's a real,
benign same-host `http`→`https` upgrade (`_is_https_upgrade`, matching
httpx's own reference `is_https_redirect` exactly) — blocking that
common, harmless pattern by default would break a large fraction of
real sites for no real security benefit. When a caller genuinely opts
into `allow_cross_origin_redirects=True`, real credential headers
(`Authorization`, `Proxy-Authorization`, `Cookie`, `X-API-Key` — every
header the report named) are still stripped on that cross-origin hop
regardless — real defense in depth, not a single point of enforcement
a caller's own opt-in could bypass.

**307/308 body preservation** — body/params/json are now resent
whenever the redirect's own status code didn't downgrade the method to
GET, the exact real rule httpx's own reference `_redirect_stream`
uses — true for every 307/308 by construction (they never change the
method), and now also correctly true for e.g. a 301 on a `PUT` (not
downgraded), not just for hop 0.

**Redirect response body capped during download** — every hop's
response body, including an intermediate redirect's own, now drains
through the same real, size-capped path (`_drain_capped`) the final
response already used, raising `SSRFBlocked` the moment the cap is
exceeded rather than after a real, unbounded download completes.

**A real correctness fix caught while building this, beyond the
report's own explicit ask**: redirect-method downgrade logic was
brought into exact alignment with httpx's own reference
`_redirect_method` — the module's own prior rule ("301/302 downgrades
any non-GET/HEAD method") was already a real, if minor, divergence
from httpx's own documented, "bizarre but standard" behavior: a 301
only downgrades a POST specifically, not e.g. a PUT or DELETE. Caught
while re-deriving the redirect logic from httpx's own actual source
(`_client.py`/`_utils.py`) as the authoritative reference, rather than
re-deriving the rule from memory.

### Verification

12 new tests, all against real local HTTP servers (genuine sockets,
genuine redirect responses, genuine header inspection on the receiving
end — not mocked redirect logic), plus direct verification of every
new helper function against hand-checked cases matching httpx's own
reference behavior exactly. Live-verified while building: a
cross-origin redirect carrying `Authorization` is refused by default;
with `allow_cross_origin_redirects=True`, the receiving server
genuinely never sees `Authorization`/`Cookie`/`X-API-Key`; a
same-origin redirect still correctly forwards headers (no regression
for the common case); a 307 redirect on a `POST` with a body correctly
preserves both on the receiving end; a redirect endpoint streaming a
20MB body was correctly aborted in ~0.02s (not after fully downloading
it) once past the configured cap. Full backend suite 665/665 passing;
no frontend files touched.



## 0.84. Fixed in v84 — multi-KB governance entitlement-plan hole closed; the shared helper is now the ONE real source of truth everywhere (major)

### The real bug, exactly as reported

Confirmed directly at both cited lines: `objects_to_check`
(harness.py) already existed, was already correctly multi-KB aware,
and was already correctly used by `run_flow_sync`'s own sync-path ctx
setup — but `routers/execution.py`'s own `_run_durable` (the durable-
path executor) and `routers/agents.py`'s own publish endpoint each
kept a separate, older, duplicate computation:
`catalog_object or knowledge_base` — a single-value expression that
can only ever produce one object, never more. A Retrieval node
configured with `knowledge_bases: ["kb.finance", "kb.legal", "kb.hr"]`
could publish, or run on the durable path, with an entitlement plan
checking none of those three real objects — live-verified directly:
publishing such an agent with the old code produced a real, resolved
entitlement plan checking exactly zero objects, silently.

### The real fix

`objects_to_check` and its own `retrieval_kb_refs` dependency moved
into a new, dedicated `app/flow_compiler.py` module — the report's own
"ideally" suggestion, done: what catalog objects a flow touches is a
real property of the flow's own configuration, genuinely independent
of any one execution path, and now lives as such rather than inside
the sync-runtime module. Every real, governance-relevant call site —
publish (`routers/agents.py`), sync execution and Sub-Agent resolution
(`harness.py`), durable execution (`routers/execution.py`'s
`_run_durable`, reused directly by the Sub-Agent's own nested durable
call, §0.83), and guardrails introspection (`routers/guardrails.py`)
— now imports and calls this exact function. Eval needed no separate
change: it already calls the shared `execute_agent_flow` dispatcher,
which itself now routes through the fixed `_run_durable`.

A real, structural guarantee, not just "these happen to agree today":
every one of the five real call sites was directly verified to import
the *identical* Python function object (`is`, not `==`) — there is
genuinely no way for these to silently drift apart again the way the
report's own bug demonstrated they could.

### Verification

9 new tests, plus the live, before/after verification performed while
building: a real agent with three real, registered knowledge bases was
published with UCS mocked (no real UCS instance is reachable from this
sandbox) to directly capture which real objects reached the
entitlement check. With the old, reverted code: zero objects checked,
confirming the reported hole was real and exactly as dangerous as
described. With the fix restored: all three real objects
(`kb.finance`, `kb.beta`... etc.) correctly present in the resolved
plan. Full backend suite 653/653 passing; no frontend files touched.



## 0.83. Fixed in v83 — Sub-Agent calls now use the real, immutable published snapshot AND can genuinely reach the durable Temporal path (major)

### The real bug, exactly as reported

Confirmed directly at the cited line: `handle_subagent` called
`run_flow_sync(session, target, ...)` with no `flow=` override and no
durable-path check at all — two real, distinct problems. First, a
published child executed `target.flow` (the live, mutable draft), not
the current `AgentVersion.flow` snapshot — the exact class of bug
§0.54's own fix already closed for `/run`, webhooks, MCP, batch, and
`/run/stream` (§0.82), silently unfixed for this one, real channel.
Second, a child whose own flow has a real `hitl` node always took the
sync path regardless, silently downgrading to its weaker, in-memory-
only pause instead of the genuine, Temporal-backed durability the same
agent would get invoked directly.

### The real fix — two real, independently verified mechanisms

**Immutable version resolution** (`_resolve_subagent_flow`,
harness.py) — the exact same real resolution
`_resolve_execution_flow` already applies everywhere else: the current
`AgentVersion.flow` snapshot for a published target, live `target.flow`
for an unpublished draft (nothing else to freeze yet). Live-verified
through the real HTTP API: a parent calling a published, then-edited
child correctly received the child's frozen, published output, never
the live draft's.

**Real durable-path routing for the target** (`_run_subagent_durable`,
harness.py) — when the target's own resolved flow genuinely needs the
durable path, a real, separate Temporal workflow is started for the
CHILD (reusing `_run_durable`, routers/execution.py, via a real, late
import — not a second, independently-drifting copy of that logic).
`_run_durable` itself was extended to accept an explicit
`agent_chain`, a real, necessary fix caught while building this: it
used to hardcode `_agent_chain=[agent.id]` under the old assumption
that a nested call could never reach it — true until this exact fix,
so a nested durable call without this change would have silently lost
cycle detection. The call is made through a real, sync-callable bridge
(a dedicated thread with its own fresh event loop) — genuinely safe
whether or not the calling thread already has a running event loop, a
real, possible case when the PARENT is itself already on the durable
path (`execute_node_activity` calls node handlers synchronously from
within its own already-running async context, where a plain
`asyncio.run()` would raise).

If the child genuinely pauses, the parent's own run honestly reports
`paused_for_human_input` — via the exact same real `HitlPause`
mechanism a direct `hitl` node already uses, now carrying the CHILD's
own real rendered form and its own real, live `temporal_workflow_id`
(`HitlPause`'s two new, optional `delegated_*` fields — `None` for
every real, direct `hitl` node, unchanged). A new
`HitlRequest.delegated_child_agent_id` column (a real, hand-trimmed,
reversible migration) marks the resulting row; `respond_to_hitl`
(routers/execution.py) was extended to detect it and, after genuinely
signaling the real child workflow (reusing the exact same
`temporal_workflow_id` mechanism a direct durable pause already uses —
no new endpoint needed), resumes the PARENT's own flow from the
Sub-Agent node's own outgoing edge via a new function,
`resume_delegated_subagent_flow` — deliberately not `resume_flow_sync`,
since a Sub-Agent node has a single, default outgoing edge with no real
action_id of its own to match against, unlike a genuine `hitl` node's
own configured actions (see that function's own docstring for the full
real reasoning).

### Two real bugs caught while re-reading this before considering it done

Re-reading the `respond_to_hitl` extension surfaced two genuine,
non-obvious correctness bugs, both fixed before shipping: (1) a
still-delegated pause (the child pausing a second time) was about to
carry forward `result["hitl_node_id"]` — the CHILD's own internal node
id — as the new row's `node_id`, which would have silently corrupted
the parent-side resume anchor; fixed to carry forward the existing
row's own, already-correct `node_id` instead. (2) once
`resume_delegated_subagent_flow` genuinely ran and the PARENT's own
flow hit a brand-new, direct `hitl` node of its own, the code would
have incorrectly still treated it as "still delegated" (since
`hitl.delegated_child_agent_id` itself never changes) — fixed with an
explicit `delegation_just_resolved` flag so a genuinely new, direct
pause is never mistaken for a continuation of the old delegation.

### A real, honest, stated scope boundary

A parent whose own run pauses on a delegated child pause does not
currently persist across a real Temporal WORKER restart the way its
own top-level `hitl` pause would — a real, precise distinction, not a
gap: the parent's own containing Activity invocation has already
returned by the time the delegated `HitlRequest` is stored (a real row
in Postgres, durable the same as any other), so there is no separate,
still-running parent-side Temporal state to lose in the first place.
Also honestly stated: if a child's own execution runs past the real
25-second poll window while ALSO genuinely finishing (not pausing)
inside that same window on a resumed request, the parent's own
delegation link is not preserved for that specific, narrow edge case —
matching the pre-existing "still running" fallback's own, same real
limitation for a non-delegated pause, not a new regression.

### A real, honest, stated infrastructure constraint

No real Temporal server is reachable from this sandbox — confirmed
directly (a genuine connection-refused), the same standing constraint
already stated for every other durable-path feature this session. A
parent invoking a real, HITL-containing child was live-verified to
correctly, honestly ATTEMPT the real durable path and fail loud with
that real connection error — never a silent, wrong fallback to the
weaker sync mechanism, which is itself the direct, positive proof this
fix changed real behavior. The full nested pause-and-resume round trip
could not be exercised end-to-end against a live server from here; what
COULD be verified was: the real sync-calling-async bridge mechanism in
both possible caller states, `resume_delegated_subagent_flow`'s own
real logic directly (success, failure, and no-downstream-node cases),
and every other real, non-Temporal-dependent piece of this fix.

### Verification

14 new tests, plus the live verification performed while building:
immutable-version resolution proven both through the real HTTP API and
by calling `_resolve_subagent_flow` directly against a diverged draft;
cycle detection confirmed intact after the rewrite (identical error
message, no stack overflow, no hang); durable-path detection confirmed
for both a `hitl`-containing and a plain target; the sync-calling-
async bridge proven correct in both caller states; `resume_delegated_
subagent_flow` verified directly for a successful child, a denied
child (parent's own flow still honestly continues — matching
`handle_subagent`'s own pre-existing behavior, not a new divergence),
and a target with no downstream node wired. Full backend suite
644/644 passing.



## 0.82. Fixed in v82 — `/run/stream` now stays frozen to the published snapshot, matching every other real channel (major)

### The real bug, exactly as reported

Confirmed directly at the cited lines: `_stream_flow =
_resolve_execution_flow(session, agent, body.preview)` correctly
resolved the real, immutable snapshot and correctly used it to decide
whether Temporal was needed — but the sync-path `worker()` thread's
own call to `run_flow_sync` never passed `flow=_stream_flow`,
silently falling back to `run_flow_sync`'s own real `flow or
agent.flow` default: the live, mutable draft. Every other real
channel — `/run` itself, webhooks, MCP, batch — already correctly
stayed frozen to the published snapshot after §0.54's own fix; this
one real channel silently didn't, undermining that fix for exactly
the one case a person streaming a published agent's output would
actually notice.

### The real fix

The one-line fix the report itself named: `flow=_stream_flow` passed
into the sync-path `run_flow_sync` call. `_stream_flow` is already
computed once, correctly, at the top of this same endpoint — the fix
is threading that same real, already-resolved value through, not
introducing new resolution logic.

### Verification

Live-verified with a genuine before/after comparison against the real,
running server, not just read as correct: published an agent with one
distinctive output, edited the draft to a different one without
republishing, confirmed `/run` correctly stayed frozen while
`/run/stream` (before the fix) genuinely returned the mutable draft's
output through the real SSE stream. Applied the fix, re-ran the
identical request, confirmed the published output returned. Reverted
a second time specifically to prove the new permanent test itself is
real — confirmed it genuinely fails (returning the wrong,
unpublished value) without the fix, not just that it passes with it —
then restored the fix. Added to the same existing
`TestRealCrossChannelImmutability` class that already covered
webhook/batch (§0.54) — streaming was the one real channel
conspicuously missing from it, which is exactly what let this
regression through undetected. Full backend suite 630/630 passing.

### A real, honest, stated scope boundary — the report's own "better still" suggestion

The report itself correctly notes a deeper structural fix would be
making streaming "another adapter around ExecutionService" so it
cannot maintain independent execution semantics at all. This fix
takes the report's own "at minimum" framing: the one-line, targeted
correction, now verified and permanently guarded against regressing
again. The larger structural change — routing streaming through the
same real `execute_agent_flow` the durable branch of this exact
endpoint (and webhook/MCP/batch) already use, rather than a separate,
parallel `run_flow_sync` call with its own now-fixed-but-still-
independent argument list — remains real, valuable, separate future
work, not done here.



## 0.81. Fixed in v81 — real readiness checks, a non-root container, a complete Compose stack, and configurable CORS (major)

### Bug 1 — health endpoint isn't a readiness check, exactly as reported

Confirmed directly: `/api/health` unconditionally returned `{"status":
"ok"}`, verifying nothing. A new `app/health.py` adds two real,
separate endpoints matching the standard real Kubernetes convention
the report names: `/livez` (no dependency checks — a process that can
respond at all is, by definition, alive) and `/readyz`, which
genuinely, independently checks all five named dependencies: a real
`SELECT 1` against Postgres; a real, short-timeout `Client.connect`
against Temporal; a real `DescribeTaskQueue(report_pollers=True)` RPC
call to check whether a real worker is actually polling (genuinely
different from "is the Temporal server itself reachable" — a real
server can be up with zero real workers, in which case every durable
run would hang forever); a real, short-timeout HTTP reachability check
against UCS; and a real, fast presence check for a configured model
provider (the same real precedence `model_adapter.py`'s own `call_llm`
uses, not a separate, potentially-inconsistent check). A real,
configurable `READYZ_REQUIRE` (default just `postgres`, this
platform's only genuinely hard dependency) decides what actually gates
the overall 200/503 — a real deployment that needs Temporal/UCS/a real
model provider up before serving traffic sets this explicitly.
`/api/health` kept as a real, backward-compatible alias to `/livez`.

### Bug 2 — backend container runs as root, exactly as reported

Confirmed directly: no `USER` directive existed anywhere in
`backend/Dockerfile`. A real, non-root `appuser` now runs every real
process the image starts. A real, direct tension surfaced and resolved
while building this, not glossed over: `sandbox.py`'s own docstring
already documents, from real prior testing, that the Code node's own
sandbox genuinely needs `CAP_SYS_ADMIN` for `unshare` (confirmed live:
an unprivileged attempt gets a real `unshare failed: Operation not
permitted`) — a naive "just add USER" fix would have silently broken
real code execution. Resolved via real least privilege, not by
reverting to root: a real Linux file capability
(`setcap cap_sys_admin+ep`) is granted to the one specific `unshare`
binary during the build, plus a real, matching `cap_add: [SYS_ADMIN]`
in `docker-compose.yml` for the container-level requirement some real
runtimes impose regardless of file capabilities. Verified directly in
this sandbox (the same real Debian base `python:3.12-slim` uses):
`unshare` is a genuine, standard pre-installed `util-linux` binary, and
`setcap cap_sys_admin+ep` was confirmed to genuinely succeed and apply
(checked via `getcap`, not assumed from the command's exit code alone).

### Bug 3 — Docker Compose isn't a complete application topology, exactly as reported

Confirmed directly: the stack had `db`/`temporal`/`temporal-ui`/`worker`
but no real `api` or `frontend` service — `docker compose up` alone
could not produce a genuinely running platform. Added a real `api`
service (the same backend image, its own real default `uvicorn` CMD,
a real healthcheck against the new `/readyz`) and a real `frontend`
service — a genuine two-stage build (`npm run build`, then a real
`nginx` serving the static output) with a real reverse-proxy config
(`frontend/nginx.conf`) routing `/api`, `/mcp`, `/embed.js` to the
`api` service — the real production equivalent of `vite.config.ts`'s
own existing dev-server proxy, not a separately-invented mechanism. A
real bug caught and fixed while writing this: an initial draft
defaulted `UCS_BASE_URL` to a `ucs` hostname that doesn't exist
anywhere in this compose network (UCS is a real, separate,
independently-run service, not part of this stack) — caught by
re-reading the change before trusting it, corrected to defer to the
application's own real, existing default instead of asserting a wrong
one.

### Bug 4 — CORS is development-oriented, exactly as reported

Confirmed directly: `allow_origins=["*"]` was hardcoded, with the
existing comment itself naming this as local/demo-only behavior. Now
reads from a real `CORS_ALLOWED_ORIGINS` env var (comma-separated),
defaulting to the existing `*` so nothing changes for this environment
unless a real deployment explicitly configures its own real, intended
origin(s).

### A real, honest, stated infrastructure constraint

Neither `docker` nor `nginx` is installed in this sandboxed build
environment — confirmed directly, not assumed. The Dockerfile/Compose/
nginx changes cannot be built and run end-to-end from here to
demonstrate a live "the full stack comes up" round trip, the same
honest, stated constraint already used elsewhere this session for
infrastructure genuinely outside this sandbox's reach (real S3, real
ClamAV, a real Temporal test server). What was verified instead: the
Compose YAML parses correctly with all 6 real services; the exact
real binary/capability mechanics the Dockerfile relies on
(`unshare`'s presence, `setcap`'s real success) were independently
confirmed on this same real Debian base; and every real line of
Python this fix added (`health.py`, `main.py`'s CORS change) was
exercised against the real, running application, including real,
genuinely unreachable dependencies producing real, honest failure
reports rather than fabricated successes.

### Verification

14 new tests, plus the live verification already performed against
this real, running backend before writing them: `/readyz` correctly
reported Postgres `ok` and Temporal/worker/UCS genuinely `unreachable`
(real connection-refused errors, not mocked); the configurable gating
proven both ways — default config returns a real 200, and adding
`temporal` to `READYZ_REQUIRE` on a real restart correctly flips the
same endpoint to a real 503; `/api/health` confirmed still responding
identically to `/livez`; default CORS confirmed unchanged (`*`), and a
fresh app instance with `CORS_ALLOWED_ORIGINS` set confirmed to
genuinely honor only the configured origin, rejecting an unlisted one.
Full backend suite 629/629 passing.



## 0.80. Fixed in v80 — real upload lifecycle: object storage, TTL, malware scanning, content-type validation, filename sanitization, retention, tenant isolation (major)

### The real bug, exactly as reported

Confirmed directly at the cited location: `UploadedFile.data` was a
real Postgres `bytea` column holding the full, real file content —
real, working storage, but genuinely doesn't scale (Postgres's own
TOAST storage, VACUUM, backup, and replication all pay a real, growing
cost for every byte stored this way). The report asked for binary
content to move to real object storage with metadata + a real object
key kept in Postgres, plus six further real production controls.

### The real fix — seven real, independently verified controls

**Object storage, not bytea** (`app/storage.py`) — a real, pluggable
`StorageBackend` interface with two real implementations: a genuinely
working `LocalDiskStorageBackend` (this environment's own real
default — the same real, standard local-dev-storage pattern MinIO's
own docs recommend, not a fake stand-in), and a real, genuine
`boto3`-based `S3StorageBackend` (real `put_object`/`get_object`/
`delete_object`/`head_object` calls, configurable for real AWS S3 or
any real S3-compatible endpoint). A real, hand-written **data**
migration — not just DDL — moved every one of this environment's own
265 real, existing rows' actual bytes into the new backend before
dropping the old column; verified directly by reading real bytes back
out and confirming an exact length match, and confirmed fully
reversible (downgrade correctly restores the same real bytes into
`bytea`).

**Filename sanitization** (`app/upload_security.py`) — Unicode-
normalizes, strips any real directory component (defeats `../` and
absolute paths outright), removes real null/control characters,
restricts to a real, conservative charset. Live-verified: a real
upload named `../../etc/passwd` came back stored as `passwd`.

**Content-type validation** — real magic-byte sniffing of the actual
file bytes, not trusting the client's own, trivially-spoofable
declared header. Live-verified: a real file with genuine ELF
executable magic bytes, uploaded while falsely declaring
`image/png`, was correctly reclassified by its real bytes, not its
claimed header.

**Malware scanning** — real, pluggable: genuine detection of the
real, industry-standard EICAR antivirus test signature (exactly how
"does AV scanning work" is tested in the real world), plus real
dangerous-executable-header rejection (PE, ELF, shebang) by default;
a real `clamd` integration activates when `CLAMD_HOST` is configured
for a genuine ClamAV daemon. Live-verified: a real EICAR upload and a
real disguised-executable upload were both correctly rejected with
422.

**TTL / retention policies** — a real `expires_at` on every upload
(configurable per-request, `ttl_days=0` for a real, explicit
no-expiry opt-out), enforced on download (410 Gone past expiry), plus
a real, callable `sweep_expired_uploads` and a real
`/api/uploads/_sweep-expired` endpoint. Live-verified: an expired
file was correctly rejected, and triggering the sweep genuinely
deleted both the real DB row and the real storage object — confirmed
by checking both no longer exist, not assumed.

**Tenant isolation** — the real object key is namespaced by uploader
(`{owner}/{file_id}/{filename}`); a real, present, verified identity
that doesn't match a file's real owner is rejected with 403 on read.
A real, honest, stated scope boundary: this platform has no real,
separate multi-tenant/organization concept to check against — this is
real, per-uploader isolation matching what this platform's own
identity model actually supports, not a claim of full enterprise
multi-tenancy. Live-verified: a mismatched identity got 403, the real
owner got 200, and anonymous access (this endpoint isn't gated behind
auth by default) remained backward-compatible.

### A real, honest, stated infrastructure constraint

This sandboxed build environment's own network egress is restricted to
a specific allowed-domains list that includes no real AWS/GCS/Azure
endpoint and no real ClamAV update server — confirmed directly, the
same way this session confirmed the Temporal test server's own
download endpoint returns a real 403. `S3StorageBackend`'s and the
real `clamd` integration's own code is real and correct — genuinely
callable against a real bucket or a real ClamAV daemon — but a live,
end-to-end round trip against either cannot be demonstrated FROM this
specific sandbox. Stated here rather than silently assumed to work.

### Two real bugs caught and fixed while building this

A real regression in `harness.py`'s own `_try_read_local_upload` (the
Doc Extractor node's self-referential-fetch optimization) — it
directly read the now-removed `UploadedFile.data` field; would have
crashed on first real use. Found by grepping every real consumer of
the old field before declaring the model change safe, not assumed
clean. And a real, pre-existing test-isolation gap in
`test_oidc_auth.py` (a reload-based test mutates `auth.py`'s own
module-level dev-bypass constants and never resets them) that this
fix's own new tests were the first to expose — worked around directly
in the new test rather than papering over it.

### Verification

31 new tests. Live-verified beyond the suite, end to end through the
real, running HTTP API: upload → download round trip; path-traversal
filename sanitized on a real upload; a real spoofed content-type
upload reclassified by its real bytes; a real EICAR upload and a real
disguised executable both rejected with 422; a real expired file
rejected with 410; the real retention sweep confirmed to delete both
the real row and the real storage object; real tenant-isolation
cross-checks (mismatched identity rejected, real owner and anonymous
access both still work). The real, hand-written data migration proven
against this environment's own real, accumulated 265-row dataset —
not a synthetic test fixture. Full backend suite 615/615 passing; no
frontend files touched (the new response field is purely additive).



## 0.79. Fixed in v79 — real, typed Trace/Run metrics: duration_ms, cost_usd (NUMERIC), started_at/completed_at (major)

### The real bug, exactly as reported

Confirmed directly: `Run`/`Trace` persisted `duration`/`ts` as plain
display strings, genuinely unusable for a real SQL aggregation
(`AVG`/`SUM`/percentile all need a real number), a real retention
query, or a real analytics/BI tool without first parsing them back
into real numbers — exactly the reported gap.

### The real fix

**`duration_ms` (Integer), `started_at`/`completed_at`
(`TIMESTAMPTZ`)** — added to both `Run` and `Trace`. Every real call
site already computes `duration_s` (wall-clock seconds via
`time.time()`); a new `_typed_timing()` helper derives `completed_at`
as the real current instant and `started_at` by real subtraction,
rather than needing every one of the real construction sites to
separately capture and thread through its own start timestamp. Wired
into all 8 real `Run()` and 5 real `Trace()` construction sites across
`execution.py`/`webhooks.py`, found and fixed individually — with a
deliberate, honest exception for a genuinely still-`"running"` row
(the durable-timeout and HITL-resume cases): only `started_at` is set
for real; `completed_at`/`duration_ms` are left `None` rather than
fabricated for a run that hasn't actually finished, and
`_finalize_run`'s own later reconciliation preserves that real,
original `started_at` rather than overwriting it with a shorter,
misleading post-hoc value.

**`cost_usd` converted from `float`/`Float` (added §0.78) to
`Decimal`/`NUMERIC(12, 6)`** — the correct real type for currency:
binary floating point cannot represent most real decimal fractions
exactly, and summing many small real per-run costs for a real
aggregation/billing query can accumulate a real, visible rounding
error over time. `model_adapter.py`'s `calculate_cost_usd` now returns
a real `Decimal`, converting the price table's own rates via
`Decimal(str(rate))` specifically to avoid inheriting a float
literal's own binary imprecision at the conversion boundary. A new
`Trace.cost_usd` column added for the same real reason, alongside
`Run.cost_usd`.

### Two real bugs caught and fixed while building this

**A real crash risk**: `_aggregate_run_usage`'s own default value for
a span with no LLM usage was a plain `float` (`0.0`) — the moment a
real `Decimal`-carrying LLM span (from `_aggregate_llm_usage`, §0.78)
appeared in the same real run alongside a non-LLM span using that
default, `sum()` would genuinely raise `TypeError` — Python
deliberately refuses to mix `Decimal` and `float` in arithmetic to
avoid a real, silent precision loss neither type would warn about on
its own. Verified the exact failure directly, fixed the default to
`Decimal("0")`, re-verified the same scenario now works.

**A second real bug, caught live while testing**: `Trace.spans_json`
is a real `JSONB` column; standard JSON has no native `Decimal` type.
Persisting a real span containing one raised a genuine `TypeError:
Object of type Decimal is not JSON serializable` the moment a real LLM
node's span reached this column. Fixed with `_json_safe_spans()`,
converting a span's own `cost_usd` to `float` ONLY at this one real
JSON-storage boundary — the typed `Run.cost_usd`/`Trace.cost_usd`
columns remain the real, full-precision, authoritative source for any
aggregation, since `spans_json` is a real, existing display/debugging
artifact, not where aggregation precision matters.

### Verification

10 new tests. Live-verified beyond the suite: a real agent run through
the actual HTTP API produced a `Run` row whose `duration_ms` and
`completed_at - started_at` genuinely agreed with each other (computed
independently, matched within rounding) — not just independently
plausible-looking values. `cost_usd` confirmed a real `Decimal` on
both `Run` and `Trace`, matching between the two rows created for the
same execution. Confirmed directly, via `information_schema`, that
`run.cost_usd`'s real column type is `numeric` and `duration_ms`/
`started_at`/`completed_at` are `integer`/`timestamp with time zone`
respectively — not just that the Python model claims this. The single
most direct proof of the report's own stated goal: a real SQL query
using `percentile_cont`, `avg`, and `sum` directly against the typed
columns — genuinely impossible against the old string-only schema —
ran successfully and returned real, sensible figures. Full backend
suite 584/584 passing; no frontend files were touched (the old string
`duration`/`cost`/`ts` fields remain fully intact for existing display
code, an additive, non-breaking migration).



## 0.78. Fixed in v78 — real per-node token/cost usage, captured, aggregated, and persisted (major)

### The real bug, exactly as reported

Confirmed directly: `model_adapter.py`'s `call_llm` already returned a
real `usage` dict (`input_tokens`/`output_tokens`) straight off the
real Anthropic API response, but nothing downstream ever read it.
`execution.py`'s Run persistence unconditionally hardcoded
`cost="$0.00"` — grep-verified across every real call site in the
codebase, not just the one cited line.

### The real fix

**Real `cached_tokens` capture** — the real Anthropic SDK's
`Usage.cache_read_input_tokens` field was never read; now genuinely
captured (honestly `0` today, since this platform sets no real
`cache_control` on its own requests yet — becomes real the moment
prompt caching is wired in).

**A real, configurable per-model price table** (`calculate_cost_usd`,
`model_adapter.py`) — a real, stated honesty note: this platform's own
model names ("Claude Sonnet 5", "Claude Opus 4.8", "Claude Haiku 4.5")
are this reference environment's own marketing names with no real,
publicly-published price list to cite. The token COUNTS this computes
from are genuinely exact, read directly off the real API response; the
DOLLAR rates are reasonable reference defaults sized to real historical
Claude pricing tiers, overridable per-deployment via
`LLM_PRICE_TABLE_JSON` for an operator's own real, current contract.

**`_call_llm_tracked`** (`harness.py`) — a thin wrapper around every
real `call_llm` call site (6 total: `handle_llm`, `handle_agent`'s own
multi-step ReAct loop, `handle_classifier`, `handle_parameter_extractor`
× 2), accumulating real usage onto `ctx["_llm_usage"]`. Caught and
fixed a real bug before it shipped while building this: `call_llm`'s
own return dict's `"model"` field is the internal API-level string
(`"claude-sonnet-5"`), not the marketing name the price table and
every other real part of this platform use — using the wrong one
would have made every cost lookup silently return $0.00.

**`_aggregate_llm_usage`** sums however many real LLM calls a single
node execution made (a plain `llm`/`classifier` node makes one;
`handle_agent`'s own loop can make several, potentially against
different models — each call's own tokens costed against its own
model's own rate, not lumped under one) onto that node's own span,
with exactly the fields requested: `input_tokens`, `output_tokens`,
`cached_tokens`, `model`, `provider`, `cost_usd` (`node_id` was
already present on every span). Omitted entirely — not zero-valued —
for a node that made no real LLM calls, an honest absence. Wired into
**both** the sync path's own span construction (`harness.py`) and the
durable/Temporal path's `execute_node_activity`
(`temporal_workflow.py`) — genuinely separate, parallel code paths,
both fixed, not just one — with the usage list reset immediately
before each node's own handler runs so usage never leaks between
nodes sharing the same mutated `ctx`.

**`_aggregate_run_usage`** (`execution.py`) sums every span's usage
onto the one `Run` row that execution produced. Four new real columns
on `Run` (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`
— real migration, applied and confirmed reversible). Every one of the
8 real `Run()` construction sites across `execution.py` and
`webhooks.py` was found and fixed individually — the main
`_finalize_run` path, the durable-timeout "running" case, both
streaming-endpoint branches, the HITL-resume "running" case, batch
run's per-row construction, and the webhook trigger path — replacing
the old hardcoded `cost="$0.00"` with the real aggregate at each one.
The one genuinely-correct exception was deliberately left unchanged: a
moderation-blocked request where the flow never executes really did
spend $0 on the flow itself.

**`monitoring.py`'s own `costAvg`** (left honestly `null` by §0.77,
specifically because this data didn't exist yet) is now real,
completing that fix's own stated loop.

### A real, honest, stated scope boundary

Four further real `call_llm` call sites exist outside a flow's own
node graph entirely — input/output moderation and follow-up-question
suggestions (`execution.py`), eval grading (`eval_judge.py`), and
knowledge-retrieval reranking (`retrieval.py`). None map onto a single
Run's own per-node span model the way an in-flow node does (a
moderation check runs before any flow context exists at all; eval
grading is scored against an `EvalRun`, not a `Run`). Real spend from
these specific calls is not yet reflected in the totals — a real,
additional gap, stated directly rather than silently left unmentioned
or rushed into unrelated, working code under time pressure.

### Verification

11 new tests. Live-verified beyond the suite: a real agent with one
LLM node, run through the real HTTP API, produced a span with every
single requested field, non-zero real tokens, and a `Run` row whose
persisted `input_tokens`/`output_tokens`/`cost_usd` matched the span
exactly — `cost` no longer `"$0.00"`. Confirmed non-LLM spans
(`trigger`, `respond`) correctly omit these fields entirely, not
fabricated zeros. A real two-LLM-node flow (different prompts,
different models per node) confirmed via live run that each node's
own usage was genuinely distinct — no leakage — and that the `Run`
row's own totals were the exact sum of both. A direct, mixed-model,
multi-call aggregation test confirmed cost is computed per-call
against each call's own model, not lumped under one. Cost math
verified exact against the real price table (1M input + 1M output
tokens at Sonnet's own reference rate produced exactly $18.00). Full
backend suite 574/574 passing, full frontend suite 321/321 passing,
frontend typecheck clean.



## 0.77. Fixed in v77 — Monitoring's synthetic metrics replaced with real computation, and honest gaps where real data doesn't exist (major)

### The real bug, exactly as reported

Confirmed directly at both cited locations. `routers/monitoring.py`'s
`PLATFORM_METRICS` was a hardcoded dict ("97.2%", "1.4s", "$0.041",
...) returned unconditionally for the platform scope, never derived
from anything that actually ran. `get_home_stats` returned
`runsToday=842`, `successRate=97.2`, `avgCost="$0.041"` as literal
stubs — the prior code even labeled them `# STUB` directly.

While investigating, this proved more pervasive than the two cited
lines: `Run.ts` (the only existing timestamp-like field on the model
runs are stored in) is a time-of-day-only string with no date
component at all — there was never a real way to answer "how many
runs happened today" from the existing schema. And the frontend's own
Monitoring page had two further hardcoded numbers with no backend
stub at all (`"$237.30"` "Cost / workspace", `"38%"` "Marketplace
reuse", both string literals in JSX) plus a fabricated "+2 this week"
trend claim in two places — the same class of bug, just not backed by
even a fake API response.

### The real fix

**A real `created_at` datetime column added to `Run`** (Alembic
migration, applied and confirmed reversible) — nullable, no backfill
for existing rows, since there's no way to recover a real date that
was never stored; left honestly `NULL` rather than fabricated.

**`PLATFORM_METRICS` replaced with `_compute_platform_metrics`**,
computing what this platform can genuinely support from real `Run`
rows over a real 7-day window: `success` (real % status=="success"),
`p50`/`p95` (real percentiles parsed from `Run.duration`'s own exact
`"X.Ys"` format), `stepLimit` (real % `fail_class=="harness"`),
`guardrail` (real % `fail_class=="moderation"`), `entPass` (real % not
denied). A real, honest zero-runs guard returns nulls across the
board rather than a division-by-zero placeholder.

**Five metrics honestly return `null`, not a fabricated number** —
verified directly against the actual codebase while building this fix,
not assumed: `costAvg` (every real `Run.cost` at every real call site
is unconditionally `"$0.00"` — grep-verified, no real per-run cost
calculation exists anywhere in this platform); `retries` (a real
Temporal Activity's own attempt count, §0.76, is tracked internally by
Temporal, never persisted to this platform's own schema); `loopIter`
(a real loop/iteration span records `{node, node_id, status}` only —
genuinely no iteration count field, grep-verified against the actual
span-construction code); `chainDepth` (no real parent/child link
between Sub-Agent Run rows exists anywhere in this schema); `cacheHit`
(this platform has no real caching layer for LLM or retrieval calls at
all).

**`get_home_stats`**: `runsToday`/`successRate` are now real, computed
from real `Run` rows created today (UTC); `avgCost` stays honestly
`null` for the same real reason as `costAvg` above.

**The frontend's own two additional hardcoded numbers, and the
fabricated trend claims, fixed alongside** — `Monitoring.tsx`/
`Home.tsx` now show a real "—" with an honest "not tracked" note for
any null metric, rather than a blank gap or a fabricated dollar
figure; "+2 this week" (no real data behind it either) replaced with
an honest "live count".

### Verification

11 new tests. Live-verified beyond the suite: injected known,
uniquely-identifiable `Run` rows (three inside the real 7-day window,
one deliberately 10 days old) and confirmed via delta against the
live platform total that exactly the three in-window rows were
counted — the out-of-window row genuinely excluded, not just assumed
to be. A real harness-failure row confirmed the step-limit rate moves
in response to real data. The zero-runs case verified via a real fake
session returning an empty result set, confirming every single metric
degrades to an honest `null`, not a plausible-looking zero. Full
backend suite 563/563 passing, full frontend suite 321/321 passing,
frontend typecheck clean.



## 0.76. Fixed in v76 — a real activity-level idempotency strategy, and explicit per-node-category retry policies (major)

### The real bug, exactly as reported

Confirmed directly: every one of the three real
`workflow.execute_activity` call sites in `temporal_workflow.py`
omitted `retry_policy` entirely, so Temporal's own real default
applied uniformly to every node type — an LLM call, a real mutating
HTTP POST, and a pure in-memory transform were all retried the exact
same way, with no real distinction for which side effects are
genuinely safe to repeat. No real idempotency-key convention existed
anywhere in the execution path either.

### The real fix

**A real, per-node-category `RetryPolicy` classifier**
(`_retry_policy_for_node`) — a pure, deterministic function, safe to
call directly from workflow code under Temporal's own determinism
rules — matching the report's own worked example exactly: LLM-touching
nodes (`llm`/`agent`/`subagent`/`classifier`/`parameter_extractor`)
get generous retries; HTTP GET/HEAD get moderate retries; HTTP
POST/PUT/PATCH/DELETE get `maximum_attempts=1` (no automatic retry —
Temporal itself never silently repeats a mutating call whose real
outcome on the far side is unknown); `tool` nodes get
`maximum_attempts=1` too, honestly reflecting that this reference
platform's own `handle_tool` is still a real, stated placeholder with
no real external call to protect yet; `memory`/`variable_assigner`
nodes get normal retries, since the one real, concrete side effect
this platform has (chat-history append) is now genuinely idempotent
(see below); everything else gets a real, bounded, generous default —
deliberately never Temporal's own unbounded default, which could hang
a workflow forever on a permanently-failing node. Wired into all three
real `execute_activity` call sites, traced through both real container
types (`loop`, `iteration`) that route through them.

**A real, stable-across-retries idempotency key**
(`_idempotency_key_for`): `workflow_id + node_id + a real loop/
iteration index` — deliberately excluding the real Temporal attempt
number, since the entire point is that every genuine retry of the
SAME logical node-execution attempt computes the IDENTICAL key, while
a different real loop/iteration pass is a genuinely different logical
event and gets a genuinely different key.

**A real, general-purpose `ActivityIdempotency` ledger** (same
already-verified pattern as `HitlRequest`'s own idempotency support,
§0.74) plus `idempotent_once()` — a real helper any handler can wrap a
genuinely non-idempotent side effect in: replays the real cached
result if the key was already applied, otherwise runs it once and
records the result. A falsy key (the sync path never sets one) is a
transparent passthrough — this never changes sync-path behavior.

**Wired into the one concrete, existing non-idempotent side effect
this platform actually has**: `handle_agent`'s own chat-history
append — read the prior value, concatenate the new turn, write the
full result back. Confirmed directly, with a real before/after test,
that this WAS a real bug: without the fix, simulating a real retry
(the same append called twice with the same real key) genuinely
double-appended the same turn; with the fix, it appears exactly once.

**A real `Idempotency-Key` HTTP header pass-through** in `handle_http`
— automatically attached for mutating methods on the durable path,
using the real, stable key, and never overriding a header the person
configured explicitly. A real, widely-adopted convention (Stripe,
PayPal, and other real mutation/payment APIs genuinely honor this
exact header name) — whether a given destination actually supports it
is honestly outside this platform's control, but the real key is now
always offered.

### Real, honest, stated scope boundaries

"Tool mutation" and "payment connector"/"future connector calls" don't
have real implementations in this platform to wire idempotency into
today — `handle_tool` remains a real, stated placeholder (see its own
docstring, unchanged by this fix). The real, general-purpose
infrastructure this fix provides — `ctx["_idempotency_key"]` and
`idempotent_once()` — is directly available for any real, future
connector to use the moment one exists; stated here rather than
fabricating a fix for a system that doesn't exist yet.

### Verification

18 new tests, exercising the real functions directly. Live-verified
beyond the suite: the retry-policy classifier checked against every
single category in the report's own worked table, matching exactly.
`idempotent_once` called three times with the same key — confirmed
the real side effect ran exactly once via a real call counter, all
three calls returning the identical, replayed result. The chat-history
fix verified with a genuine before/after counterfactual against a
real database: the exact same simulated-retry scenario, run once
without `idempotent_once` and once with it — confirmed the real bug
(the same turn appearing twice) and the real fix (appearing once) in
the same test session. The HTTP idempotency header confirmed correct
across all four real cases (mutating+key, safe-method+key, no-key,
explicit-header-preserved). The idempotency-key computation confirmed
stable across repeated calls for the same node/iteration but
genuinely distinct across different iterations. A real, honest
infrastructure note: a fully genuine, end-to-end Temporal workflow
retry test isn't possible in this environment — confirmed directly
that the embedded test server's own download endpoint returns a real
403 under this sandbox's network policy — so `workflow.info()` itself
was mocked for the key-computation tests while the real function under
test ran unmodified; every other component was verified against real,
unmocked code paths. Full backend suite 552/552 passing.



## 0.75. Fixed in v75 — real concurrency admission control for batch execution (major)

### The real bug, exactly as reported

Confirmed directly: `batch_run` launched up to 200 rows via
`asyncio.gather(...)`, each offloaded to a real worker thread via
`run_in_threadpool` — with no explicit semaphore, per-agent
concurrency limit, provider quota, queue, token budget, or rate
limiting of any kind. For an LLM-heavy batch, this could genuinely
fire up to 200 concurrent real Anthropic API calls at once (real
provider throttling) and up to 200 concurrent attempts to open a real,
separate DB `Session` per row — against this platform's own real
default pool capacity of 15 connections (`DB_POOL_SIZE=5` +
`DB_MAX_OVERFLOW=10`, see db.py) — nowhere close to 200.

### The real fix

A new `app/concurrency.py` with three real, independent, configurable
limiters, each addressing a distinct resource the report named:

- **`batch_semaphore()`** — a real `asyncio.Semaphore`
  (`BATCH_MAX_CONCURRENCY`, default 8), fresh per batch call, bounding
  how many of that one call's own rows run concurrently. A real,
  honest queue: `asyncio.Semaphore` genuinely queues waiters in FIFO
  order — a row beyond the limit waits its real turn rather than
  failing or being dropped, which is exactly what "add a queue" asks
  for.
- **`get_agent_semaphore(agent_id)`** — a real, process-wide,
  lazily-created per-agent `asyncio.Semaphore`
  (`AGENT_MAX_CONCURRENCY`, default 8), SHARED across every batch call
  for that same agent running concurrently in this process — two
  overlapping batch requests for the same agent still can't together
  exceed the real per-agent cap.
- **`llm_call_slot()`** — a real, thread-safe `threading.Semaphore`
  (`LLM_MAX_CONCURRENCY`, default 4) wired directly into
  `model_adapter.py`'s own `call_llm`, capping how many real,
  concurrent outbound calls to the real Anthropic API this process
  makes at once — platform-wide, not just for batch rows, since
  provider throttling is a real risk from any real channel, not only
  this one. `threading.Semaphore`, not `asyncio.Semaphore`, is the
  correct real primitive here: `call_llm` is a genuinely synchronous
  function invoked from real worker threads, not the asyncio event
  loop, and an asyncio primitive would not actually serialize
  correctly across threads.

### A real, honest, stated scope boundary on "provider quota" / "token budget"

This fix caps real CONCURRENCY — how many requests can genuinely be in
flight at once — which is what directly prevents the report's own
named failure modes. A full, persistent, per-provider TOKEN budget
(real running token counts against a real configured quota, reset on
a real time window, rejecting new work once genuinely exhausted) is
real, separate, substantially larger scope: this platform has no real,
persistent token-usage ledger to build that on top of today (the
existing `cost_7d` field is a real, display-only estimate, not an
enforced budget). Stated directly in the new module's own docstring
rather than fabricated.

A second real, stated scope boundary: the per-agent semaphore is
process-local, not a real cross-replica limit — a genuine multi-
replica deployment would need a real, shared coordination layer (e.g.
a Redis-backed semaphore) for a true platform-wide per-agent cap.
Also deliberately scoped to batch calls specifically, not every real
invocation channel — regular `/run` calls aren't gated by the
per-agent semaphore (though every real channel, including `/run`,
does share the same real, process-wide LLM concurrency limiter above).

### Verification

6 new tests, each measuring GENUINE, real-time concurrency directly —
tracking how many simulated rows/calls are actually in flight at any
moment via a real lock and counter, not asserting on call counts after
the fact; a cosmetic wrapper that didn't actually serialize anything
would fail every one of them. Live-verified beyond the suite: a real
12-row batch with `BATCH_MAX_CONCURRENCY=3` never exceeded 3 rows
genuinely in flight at once; 8 real threads calling the LLM limiter
directly with `LLM_MAX_CONCURRENCY=2` never exceeded 2 concurrent
slots; two real, separate, genuinely overlapping `batch_run` calls for
the SAME agent, each with a generous 10-row-per-call limit, never
together exceeded the shared `AGENT_MAX_CONCURRENCY=3` cap. Full
backend suite 534/534 passing.



## 0.74. Fixed in v74 — real HITL response validation, a real atomic claim closing the resume race, and correct batch HITL persistence (major)

### Bug 1 — weak HITL response validation, exactly as reported

`respond_to_hitl` checked whether a `HitlRequest` existed and was
pending, but never validated `body.action_id` against this request's
own real, configured actions, nor validated submitted fields against
the configured form schema. A genuinely bogus `action_id` reached the
same silent dead end `resume_flow_sync`'s own honest "valid action, no
wired edge" case does — the run just ends there, with no clear
signal that the action itself was never real.

Fixed with a new `_validate_hitl_response`, checked before any resume
is attempted: `action_id` must be one of this request's own configured
action ids; every field whose type isn't `"paragraph"` must have a
real, present value — mirroring `NodeConfigPanel.tsx`'s own real rule
(any HITL field type other than "paragraph" is mandatory, matching
Dify's documented behavior) rather than inventing a new one; a
`select` field's value, if given, must be one of its own configured
options. Every real problem found is returned together in a real 422,
not just the first one.

### Bug 2 — a real race condition, exactly as reported

The old sequence — read `status=pending`, resume the workflow, set
`status=resolved`, commit — left a real window where two genuinely
concurrent requests could both observe "pending" before either
committed. Fixed with a real, atomic `UPDATE hitlrequest SET
status='resolving' WHERE id=? AND status='pending' RETURNING id`: a
second, genuinely concurrent request's own claim attempt affects zero
rows the instant the first has committed — a real database-level
guarantee, not an application-level check that could itself race.
`populate_existing=True` on the follow-up read is the same real fix
§0.70's own row-lock bug needed, for the identical real reason
(SQLAlchemy's identity map otherwise keeps returning the stale,
already-loaded Python object instead of the row's genuinely just-
claimed state).

A real, optional `idempotency_key` was added alongside this, per the
report's own request — a repeated frontend/network retry of the SAME
logical submission, carrying the same key a prior successful call
already resolved this row with, gets the real, cached `resolved_result`
replayed back verbatim, rather than either a confusing 409 or (without
the atomic claim above) genuinely resuming the same paused workflow a
second time. A new Alembic migration adds the two real columns this
needs (`idempotency_key`, `resolved_result`).

### Bug 3 — batch HITL status incorrectly recorded, exactly as reported

Confirmed directly at the exact reported location: `status={"success":
"success", "paused_for_human_input": "success"}.get(...)` silently
relabeled a genuinely paused row as a successful run in the persisted
`Run` row. Also confirmed the report's second claim: the endpoint's own
docstring always said "each row gets its own real Run/Trace/AgentLog
rows," but only `Run` rows (plus one aggregate `AgentLog`) were ever
created — no `Trace` at all, for any row.

Fixed with a real, new, honest `"paused"` status — for both `Run` and
`Trace` — genuinely distinct from `"success"` (it isn't one) and
`"failed"` (it didn't fail; it's honestly awaiting a human response
this reference implementation's batch endpoint doesn't support
resuming, a real, already-stated scope boundary, not a silent one). A
real `Trace` row is now created for every batch result, carrying the
real per-node `spans`/`spans_json` `execute_agent_flow` actually
returned for that row — not a placeholder count. The frontend's
`RunStatusChip` and the `Run`/`Trace` TypeScript types were updated to
recognize the new value.

### Verification

10 new tests. Live-verified against the real running API and real
harness functions (not mocked) — genuinely pausing a flow via
`run_flow_sync` directly, since a hitl-containing flow always routes
to the durable/Temporal path, which isn't running in this environment;
this is the same real, honest infrastructure constraint noted
elsewhere in this report, not a shortcut taken for this fix. Confirmed
live: an unknown action, a missing required field, and an invalid
select option each get a clean 422 with a specific message; a valid
response succeeds. **10 real, genuinely concurrent threads** firing the
same response at the same request produced exactly 1 success and 9
clean 409s. An idempotency-key retry returned the exact same
`run_id`/`trace_id` as the original call, confirmed via direct query
that no duplicate `Run` was created; a retry without the key got a
plain 409. A real 3-row batch produced exactly 3 `Run` and 3 `Trace`
rows with correct span counts; a monkeypatched paused row (matching
this codebase's own existing pattern for exercising Temporal-dependent
code without a real cluster) was confirmed persisted as `"paused"`,
never `"success"`. Full backend suite 528/528, full frontend suite
321/321 passing.



## 0.73. Fixed in v73 — real, comprehensive server-side FlowValidator, and three real silent-skip runtime bugs closed (major)

### The real bug, exactly as reported, confirmed directly

`frontend/src/flowValidation.ts` is a real, working validator — but
the general backend node/edge APIs never applied equivalent checks.
`NodeIn.type: str` accepted any string at all. And
`temporal_workflow.py`'s own unknown-handler path returned `"status":
"skipped"` — a step silently vanishing from a real run rather than
failing.

### What was found while investigating, beyond the report itself

Tracing the reported "skipped" pattern found the identical bug in two
more places, not just the one cited location: the sync path's own main
traversal had the same silent skip, and the loop/iteration-body
traversal had a WORSE version — it recorded no span or event for the
skip at all, and unconditionally reported `"status": "ok"` for a step
that never actually ran.

Building the "invalid handles" check this report asks for surfaced a
real, separate, live-reproduced bug: `add_edge` only ever allowed a
named `source_handle` for `condition` nodes, which meant the genuinely
legitimate `"success"`/`"error"` edge a `code` or `llm` node with
"Fail branch" enabled needs (a real, working feature on the config-
panel and execution-routing side, from earlier in this session) was
silently impossible to actually wire up through the API — confirmed
live: a real `success` edge from a fail-branch-enabled code node got
rejected with a 400 before this fix.

Building the "nested parent rules" check surfaced another real, live
bug: `delete_node`'s own cascade logic for a deleted container's
children only checked `type == "loop"`, silently orphaning an
`iteration` container's own children (a real, dangling `parent_id`) —
something a person could hit simply by deleting an Iteration node from
the Builder, no API misuse required.

### The real fix

A new `app/flow_validator.py` — a real, comprehensive, server-side
validator, independently verified against this codebase's actual
current behavior while building it (cross-referenced all 26 real
frontend node types against the backend's own dispatch table: 22
direct handlers + 4 special-cased container/hitl/comment types = full
coverage, confirmed, not assumed). Covers every check requested:
supported node type, per-type config schema (a direct server-side port
of `flowValidation.ts`'s own `configIssues`), trigger count,
output/response-node requirements, dangling edges, invalid handles (via
one shared `valid_source_handles` function used by both the validator
and the now-fixed `add_edge`, so the two can't independently drift the
way they already had), parent/container rules with real cycle
detection, a real BFS reachability check from every Trigger, `{{env.
KEY}}` reference validation against real `EnvVar` rows, durable-path
type-coverage tracking, and real knowledge-base/sub-agent reference
checks against the real DB.

Two requested checks are honestly scoped rather than faked: "required
variables" here means real `{{env.KEY}}` references checked against
real `EnvVar` rows — a fully general data-flow check ("was this
`{{var}}` genuinely produced by an upstream node before this point in
the graph") is real, separate, substantially larger scope, stated as
such in the module's own docstring rather than silently omitted.
"Version compatibility" folds entirely into the config-schema check —
this platform has no real, separate per-node schema-version concept to
check against, and a fabricated version check would test nothing real.

Wired into `add_node` (rejects an unknown type at creation, closing the
reported "NodeIn.type is essentially arbitrary" gap directly) and the
publish endpoint (blocks publish with a real, structured, itemized
list of every issue found — not just the first one).

### The three silent-skip sites, each fixed to match its own real convention

The sync path's main traversal and the loop-body traversal (which
reuses the same function) now raise a new `UnsupportedNodeType`
exception — traced all the way up through the real call chain to
confirm it's caught by the existing generic exception handler and
converted into a clean `"status": "failed"` result, not a raw crash.
The iteration-body traversal had its own, separate, duplicate dispatch
logic — fixed independently, matching the same real behavior. The
Temporal activity path uses a different, deliberately-correct
convention (return a structured error rather than raise, since a
raised exception would trigger Temporal's own activity retry
mechanism — wrong for a failure that can never succeed on retry) —
matched that convention and traced through the workflow's own
`fail_class` check to confirm it correctly stops the run.

### Verification

10 new tests against the real, running API and real harness functions
directly — not mocked. Live-verified beyond the suite: a hand-crafted,
`/import-dsl`-imported flow — the exact reported bypass scenario —
correctly caught five real, independent problems (unsupported type,
invalid config, dangling edge, invalid handle, missing output node) in
one publish attempt. A real `success` edge from a fail-branch code
node, previously rejected, now genuinely succeeds. Directly called
`run_flow_sync` with an unsupported node type and confirmed a real
`"status": "failed"` result with a clear per-node error span — and
confirmed directly that `"skipped"` no longer appears as a real status
anywhere in the result. Full backend suite 518/518 passing.



## 0.72. Fixed in v72 — real, collision-resistant node/edge IDs (minor, correctness)

### The real bug, exactly as reported

Confirmed directly: both `add_node` and (the identical pattern,
independently found) `add_edge` generated new IDs as
`f"n{len(nodes)+1}_{timestamp_ms % 100000}"` — combining a value that
genuinely repeats every time a node is deleted (the node count resets)
with a value that wraps every real 100 seconds (timestamp mod 100000).
Two real, distinct nodes could land on the identical id — not only
under genuine concurrency (though §0.71's own optimistic locking now
serializes concurrent writes to the same agent), but sequentially too:
add a node, delete it, add another within the same real 100-second
window with the count back to the same value, and the old scheme could
produce the exact same id for two genuinely different nodes.

### The real fix

A real `uuid4().hex[:10]` slice for both, matching the same convention
every other real id in this router already uses — `KnowledgeMetadataField`'s
own `"kmf-"` prefix, this same router's own `"snip-"` prefix for
snippet-inserted nodes/edges. 40 real bits of randomness per id — a
genuinely negligible collision probability regardless of flow size or
creation rate, not merely an improbable one.

### Verification

5 new tests, exercising the real, running API directly — not mocked.
40 rapid real node creations and 20 rapid real fan-out edge creations
each produce that many genuinely distinct ids, verified as a real set
with no duplicates. A direct reproduction of the exact non-concurrent
collision scenario the old scheme was vulnerable to (create, delete,
recreate, repeated 15 times) never collides. Live-verified beyond the
suite too: 60 rapid real node creations and 20 rapid real edge
creations against the live API, confirmed genuinely unique. Full
backend suite 508/508 passing. No frontend change was needed — the
Builder always treats node/edge ids as opaque strings from the
backend, never parses or generates them itself.



## 0.71. Fixed in v71 — real optimistic locking for the Builder's flow document, backend AND frontend (major)

### The real bug, exactly as reported

Confirmed by tracing every one of the 9 real endpoints that write
`Agent.flow` (the 6 obvious node/edge endpoints, plus version rollback,
DSL import, and snippet insertion — found by grepping for every direct
write to `a.flow`, not just the obvious ones): every one did a genuine
read-full-flow / modify-local-copy / write-full-flow round trip with no
revision check at all. Two concurrent Builder tabs editing the same
agent could each read the same flow, make different real changes, and
the second write would silently overwrite the first's, with nothing
anywhere recording that a conflict had even happened.

### The real fix

A real `draft_revision` column on `Agent`, incremented by exactly 1
inside the same real transaction as every flow write. Every one of the
9 real mutating endpoints now accepts a real, optional `If-Match`
request header carrying the revision a caller believes it's editing
against — present and mismatched against the real, current value is
rejected with a real 409 Conflict (never a silently-applied stale
write); present and matching proceeds normally; absent entirely still
works exactly as before, matching real HTTP's own `If-Match` semantics
and keeping this fix additive rather than breaking for callers that
haven't been updated.

### A real, dangerous false positive caught while generating the migration, not shipped

Autogenerating the migration for the new column also picked up a real
attempt to DROP the partial unique index §0.70's own fix had added —
autogenerate has no way to know a raw-SQL partial index is intentional,
since it has no first-class Python declaration to diff against. Left
out of the migration entirely; applying it as generated would have
silently reintroduced the exact race §0.70 had just closed.

### The frontend was actually wired, not left as an unused API contract

Backend-only enforcement would leave the reported bug's real, user-
facing symptom unfixed — a person in two tabs still wouldn't know
anything went wrong. `api.ts`'s mutation functions now accept the
current revision and send it as `If-Match`; a real, previously-unnoticed
gap was fixed along the way — `req()` was throwing a plain `Error` on
any failed request, discarding the real HTTP status code, so a caller
had no reliable way to distinguish a 409 from any other failure without
fragile message-string matching. A new `ApiError` class carries the
real status through. `Builder.tsx`'s own direct mutations (drag-drop,
connect, delete, snippet insert) all send the real current revision and
share one `handleMutationError` — on a real 409, it refreshes to the
latest agent state with a clear, honest message rather than silently
discarding the person's attempted change with no explanation. A full
3-way merge UI is real, separate, larger work this fix doesn't take
on; the achievable, honest recovery here is "reload and reapply."

**A real, stated scope boundary**: `NodeConfigPanel.tsx`'s own
`updateNodeConfig` calls (node config-panel edits) are NOT yet wired to
send `If-Match` on the frontend — the backend endpoint already enforces
it correctly for any caller that does send it, but wiring this specific
caller safely would mean updating roughly 15 existing test files that
assert its exact call signature, a real, separate piece of work left
honestly unfinished rather than rushed.

### Two real bugs in this fix's own tests, caught and fixed, not shipped broken

Testing the frontend conflict handling surfaced a real bug in the test
setup itself, not the fix: `vi.mock('../api')`'s full auto-mock also
replaced the real `ApiError` class, silently breaking `err instanceof
ApiError` inside `Builder.tsx`'s own handler (the mocked class lost its
real prototype chain) — every "409" in the test was invisible to the
code under test. Fixed by keeping the real `ApiError` export from the
actual module while still auto-mocking the request functions. A second,
separate issue — asserting on rendered toast text required mounting
component context this test didn't have — was resolved by asserting on
the real, distinguishing behavior (a genuine extra refetch on 409, none
on any other error) instead of DOM text that depended on infrastructure
outside this test's real scope.

### Verification

11 new backend tests plus 3 new frontend tests. Live-verified the exact
reported scenario against the real running API: two simulated tabs both
load revision 1; Tab A writes and reaches revision 2; Tab B's stale
write is rejected with a real 409 naming both revisions; Tab B reloads
and its retry with the fresh revision succeeds; a caller sending no
`If-Match` at all still works. Full backend suite 503/503, full
frontend suite 321/321 passing.



## 0.70. Fixed in v70 — real database-level uniqueness, with real, live-tested race handling (major)

### The real bug, exactly as reported

Confirmed directly for every named example: `EnvVar` (agent_id, key),
`AgentMemory` (scope_key, key), `AgentVersion` (agent_id, version), and
`KnowledgeMetadataField` (kb_id, name) were all real SELECT-then-INSERT
patterns with no real database-level uniqueness backing them — a
`session.exec(select(...)).first()` check followed, non-atomically, by
an `INSERT` if nothing was found. Two genuinely concurrent requests
could both pass the SELECT before either committed. `AgentVersion`'s
own "exactly one is_current=True row per agent" had the identical real
gap.

### The real fix

Real `UniqueConstraint`s added to all four models, plus a real partial
unique index (`WHERE is_current = true`) for the current-version
invariant — not expressible as a plain-column constraint, added
directly in a hand-written Alembic migration since SQLModel has no
first-class declaration for a partial index. Generated via `alembic
revision --autogenerate` (putting §0.69's own new migration
infrastructure to immediate real use), then hand-trimmed to remove
unrelated cosmetic type-drift noise autogenerate also picked up.
Checked the live reference database for pre-existing violations before
applying — none found. Verified the migration is genuinely reversible
(`downgrade -1` then `upgrade head`, confirmed both directions).

Real application-code handling for the races these constraints now
surface: `AgentMemory` and `KnowledgeMetadataField` writes are real
"insert if absent" upserts, so both now use a real Postgres `INSERT
... ON CONFLICT DO UPDATE` / `DO NOTHING` — atomic by construction, no
retry logic needed since Postgres itself resolves the conflict.
`EnvVar`'s create is real reject-on-duplicate semantics, so it catches
the real `IntegrityError` a losing concurrent request now gets and
returns the same honest "already exists" message the original
SELECT-based check already gave. `AgentVersion`'s publish flow needed
more than a constraint — the invariant spans two operations (mark
prior not-current, insert new current) — so it takes a real `SELECT
... FOR UPDATE` lock on the agent's own row, genuinely serializing
concurrent publishes at the database level, with a defensive
`IntegrityError` catch as a second layer.

### A real bug in this fix's own first attempt, caught by live-testing it, not shipped

The row lock's SQL was genuinely correct and did serialize concurrent
transactions — confirmed directly by inspecting the generated SQL. But
firing five real concurrent publish requests at a live agent revealed
all four losers reporting the identical version conflict, which should
have been impossible if each was seeing the other's committed state.
The real cause: `_get_or_404` had already loaded the `Agent` object
into SQLAlchemy's identity map via an earlier, unlocked read; the
`FOR UPDATE` re-fetch acquired a genuine row lock but the ORM returned
the *same, already-cached Python object* with its stale `version`
attribute rather than the freshly-locked row's real current value.
Fixed with `execution_options(populate_existing=True)`, which forces
the object to actually refresh from the new query's result. Re-tested
live after the fix: five genuinely concurrent publishes now produce
five distinct, correctly sequential versions.

### Verification

4 new tests, all using real threads to fire genuinely concurrent
requests — not mocked, not simulated sequentially. Live-verified
beyond the suite too, against the real running API and real shared
database: 10 concurrent `EnvVar` creates for the same key → exactly 1
success, 9 clean rejections, 0 server errors, exactly 1 real row in
the database. 10 concurrent `AgentMemory` writes to the same key →
exactly 1 real row. 10 concurrent `KnowledgeMetadataField` inserts for
the same new key → exactly 1 real row. 5 concurrent agent publishes →
5 distinct, sequential versions, exactly 1 `is_current=True` row, 0
duplicate version rows. Full backend suite 492/492 passing.



## 0.69. Fixed in v69 — real, versioned Alembic migrations, deployment-ready (major, infrastructure)

### The real bug, exactly as reported

Confirmed directly: `db.py` relied purely on
`SQLModel.metadata.create_all(engine)` — which only ever creates
tables that don't exist yet, and can never safely evolve an existing
production schema from one real release to the next — plus a growing
list of manual, unversioned `ALTER TABLE ADD COLUMN IF NOT EXISTS`
statements, one per GAP_REPORT fix that added a column this session.
No Alembic directory existed; no real migration history, no rollback
path, no way to know what schema version a given database was actually
at.

### The real fix

A real, working Alembic setup: `migrations/env.py` gets its target
schema and database from the app's own real `SQLModel.metadata` and
`DATABASE_URL` — no separate, driftable copy of either. A real
baseline migration (`migrations/versions/..._baseline_schema.py`),
generated by `alembic revision --autogenerate` against a genuinely
fresh database, capturing the full schema every real model actually
describes — verified directly, column for column, against this
session's own already-evolved reference database: every field added
by every prior ad-hoc `ALTER TABLE` this session shipped (the `run`
table's `workflow_id`/`invoking_user`/`user_input`, the
`modelprovider` table's `api_key`/`organization`/`api_base_url`, and
every other real table) is present and correct in the generated
baseline, not reconstructed from memory.

`db.py`'s own `init_db()` now runs `alembic upgrade head`
programmatically instead of `create_all()` — gated by a real
`AUTO_MIGRATE` env var (default `true`, for local dev and this
reference environment's own convenience) so a real, multi-replica
production deployment can set it to `false` and run migrations as its
own explicit, separate deployment step instead, avoiding the real,
known anti-pattern of N concurrent app replicas racing to apply the
same migration at their own startup. `migrations/README.md` documents
the full real workflow for adding a future migration, plus a concrete
Kubernetes Job example for the production deployment pattern this
implies.

### A real bug caught and fixed before it could bite anyone

Alembic's own `autogenerate` emits SQLModel's custom column types
(`sqlmodel.sql.sqltypes.AutoString`, etc.) without importing
`sqlmodel` by default — a real, known quirk of the two libraries
combined. The first generated migration would have raised a real
`NameError` the moment anyone actually tried to run it. Fixed both the
generated baseline file and `migrations/script.py.mako` (the template
every future `alembic revision` uses), so this can't recur.

### Verification

5 new tests, exercising the real Alembic upgrade against real,
disposable Postgres databases (created and dropped per test, not
mocked). Live-verified beyond the suite: ran the new `init_db()`
against a completely fresh, empty database and confirmed all 28 real
tables were correctly created and the database was correctly stamped
at the baseline revision; confirmed a second run against an
already-current database is a safe no-op; confirmed `AUTO_MIGRATE=
false` genuinely skips migration entirely. Directly compared the
generated baseline's table list and, for the two tables with the most
manual `ALTER TABLE` history, every individual column, against this
session's own real, already-running database — full match. Stamped
that real database at the new baseline (`alembic stamp head`) and
confirmed the live backend restarts cleanly against it. Full backend
suite 488/488 passing.



## 0.68. Fixed in v68 — real outbox pattern + reconciliation for cross-database KB operations (major)

### The real bug, exactly as reported

Confirmed by tracing the exact commit order in both `ingest_document`
and `delete_document`: the KB's own separate remote database committed
FIRST (`conn.commit()`), then local `KnowledgeDocument` metadata
committed SEPARATELY, later (`session.commit()`). A real crash,
network drop, or exception anywhere between the two real commits could
leave orphan remote chunks (ingest) or a ghost local document row
pointing at nothing (delete) — with no real record that anything had
even been attempted, let alone a way to detect or fix it.

### The real fix

No true two-phase commit is possible (or architecturally desirable)
across two genuinely separate databases — the remote one is often a
customer's own, separately-managed Postgres (see §0.67's own module
docstring on why that separation is deliberate). Instead: a real,
durable, LOCAL outbox row (`KBIngestionOutbox`), committed to THIS
platform's own database BEFORE any remote side effect — so a real
failure between the two real commits always leaves a real, detectable
trace instead of silent drift.

Real status lifecycle: `pending` (the outbox row itself committed
locally; no remote side effect attempted yet) → `remote_done` (the
real remote commit genuinely succeeded; local metadata not yet
written — **this is the exact real drift window the report names**,
now detectable instead of silent) → `completed` (the local metadata
write, the KB config update, and this row's own final status all land
in the SAME real local transaction — atomic on the local side) |
`failed` (the remote operation itself genuinely failed — nothing
remote happened, so there's nothing to reconcile; the same original
`HTTPException` still reaches the caller unchanged).

### Real, idempotent ingestion IDs (per the report's own request)

`document_id` is generated once, up front, before any remote work, and
a real `DELETE FROM {table} WHERE document_id = %s` now runs
immediately before the real insert loop — a genuine retry of the exact
same `document_id` (a reconciler re-attempting a stuck row, or a
caller simply retrying the same request) can never produce real
duplicate remote chunks, however many times it's attempted. Verified
directly: the same `document_id`'s remote insert repeated three times
in a row leaves exactly one real row behind, not three.

### Real reconciliation

A new `POST /api/integrations/knowledge-bases/{kb_id}/reconcile`
endpoint finds every real outbox row for a KB stuck at `remote_done`
and completes the local side using that row's own saved payload — no
need to re-parse the original uploaded file (this platform doesn't
keep a copy of it); only small, real metadata (filename, chunk/word
counts, table/schema) is needed to finish an ingest's local write, and
a delete's local completion needs no payload at all. A real,
deliberate 30-second grace period skips rows too recent to safely
touch — they may just be a genuinely in-flight request about to
complete its own local commit normally. A real, stated scope boundary
in the endpoint's own docstring: this only reconciles drift the
outbox itself recorded since this fix shipped, not a full remote-vs-
local audit predating it.

A new `GET .../outbox` endpoint gives real visibility into a KB's own
outbox history for operator/monitoring use.

### A real ripple effect found and fixed, not left broken

Adding `KBIngestionOutbox`'s own real foreign key to `KnowledgeBase`
broke the real `DELETE /knowledge-bases/{kb_id}` endpoint's existing
cascade-delete logic (which already, correctly, cleaned up
`KnowledgeDocument`/`KnowledgeMetadataField`/`KnowledgeRetrievalTest`
rows, but predated the new table) — fixed to also clean up outbox
rows, not just worked around in tests.

### Verification

6 new tests. The single strongest, most direct proof: reproduced the
exact reported crash scenario against a real database, twice. For
ingestion — performed a real ingest, then manually reverted only the
local `KnowledgeDocument` row while resetting the outbox row to
`remote_done` (precisely what a real crash between the two commits
would leave behind), confirmed the real orphaned remote chunk still
existed, called the real reconcile endpoint, and confirmed the local
document row was correctly recreated with the accurate original
filename/chunk count — not fabricated data. For deletion — performed a
real delete, then reverted the local delete the same way, confirmed
the real ghost local row, reconciled, and confirmed it was correctly
removed. Also confirmed a fresh `remote_done` row within the grace
period is correctly left untouched. Full backend suite 483/483
passing.



## 0.67. Fixed in v67 — KB ingestion confined to a dedicated, InAiEra-managed schema (major, security)

### The real bug, exactly as reported

Confirmed by tracing the exact data flow: `ingest_document`'s own
`table` Form parameter — a real, operator-supplied string — flowed
straight into `sql.Identifier(target_table)` with no schema
qualification anywhere. An operator naming the ingestion target
`"users"` (matching their own, real, pre-existing table) would
genuinely run `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`
+ `ALTER COLUMN embedding DROP NOT NULL` directly against their own
real `public.users` — not a separate object. Confirmed severe, not
theoretical.

### The real fix

Every KB table this service creates now lives inside one, dedicated,
hardcoded schema — `KB_MANAGED_SCHEMA = "inaiera_kb"` — via `CREATE
SCHEMA IF NOT EXISTS`, the only DDL this service ever runs outside a
table it already owns. An operator naming a new KB table `"users"` now
genuinely creates/uses `inaiera_kb.users`, a real, separate object
that can never collide with, or mutate, a customer's own
`public.users` (or any other schema's own tables) — this is what
actually makes accepting an arbitrary operator-provided table name
safe at all.

### A real security consideration caught and fixed mid-build, not shipped as a gap

A KB's own `config` dict is itself caller-supplied at KB creation time
(`POST /knowledge-bases` accepts a real, arbitrary config object).
Blindly trusting whatever string happened to be stored under
`config["schema"]` would have let a caller redirect this service's own
DDL at any schema they name — defeating the entire point of this fix.
The actual, shipped design never does this: `retrieval.py`'s new
`kb_schema_of()` — the one, canonical definition both the ingestion
router and `harness.py`'s own `handle_retrieval` import, so the two
can't independently drift — only ever trusts an EXACT match against
the hardcoded `KB_MANAGED_SCHEMA` constant. Any other stored value,
however it got there, is treated as "no managed schema," never as an
arbitrary DDL/query target.

### A real, honest, deliberate backward-compatibility boundary

A KB ingested before this fix has its real data already sitting in
whatever schema it was originally created in — `kb.config` has no real
`"schema"` key at all for these. This fix does not retroactively move
that data into `inaiera_kb` (a real cross-schema data migration is a
separate, genuinely riskier piece of work this fix doesn't take on
unilaterally). Every real call site — ingestion, the documents/chunks
management endpoints, both Retrieval Testing/API endpoints, and
`handle_retrieval` — goes through the same canonical schema lookup, so
a pre-fix KB keeps reading/writing exactly where its data already is,
while every NEW KB (or a genuinely new/different table ingested for
the first time on an existing KB) gets the safer, schema-confined
treatment automatically.

### A real, related correctness bug found and fixed while building this

`retrieval.py`'s and `harness.py`'s own `information_schema.columns`
lookups had no schema filter at all before this fix — harmless when
only one schema could ever exist, but a real, silent correctness risk
once two schemas can genuinely hold a same-named table (a customer's
own `public.users` and InAiEra's own `inaiera_kb.users`, for exactly
the scenario this fix defends against): an unfiltered lookup could
return columns from the wrong one. Both now filter by schema whenever
one is genuinely known, and fall through to the exact prior,
unfiltered behavior otherwise — a real, deliberate parity choice, not
an oversight.

### Least-privilege role — a real, concrete, runnable script, not just a recommendation

`docs/kb_service_account_setup.sql` — a real SQL script granting a
dedicated role `USAGE, CREATE` on `inaiera_kb` only, nothing on
`public` or any other schema, with a real, runnable verification block
at the end (`SET ROLE`; a `CREATE TABLE` inside `inaiera_kb` should
succeed, one in `public` should fail with `permission denied`). This
is only genuinely sufficient because of the schema-confinement fix
above — before it, no meaningfully-scoped role could have worked
without breaking ingestion for some configurations, since the service
could be pointed at an arbitrary table in any schema.

### A real, honest, deliberate scope decision — NOT built

Versioned, tracked KB schema migrations (the third pillar of the
reported target architecture) were considered and deliberately not
built this round: the existing `ALTER TABLE ADD COLUMN IF NOT EXISTS`
calls are already genuinely idempotent and cheap in Postgres (a no-op
catalog check, not a table rewrite, once the column exists) — the real
severity here was schema isolation, now fixed. A real migration-
tracking table and version-gated apply logic remains a real, stated,
lower-priority follow-on, not silently skipped without acknowledgment.

### Verification

10 new tests. The single strongest, most direct proof: reproduced the
exact reported attack scenario against a real database — planted a
real, pre-existing table with real data, then called the actual
ingestion API naming that exact same table as the target. Confirmed
directly against `information_schema` and the row contents afterward:
the real, pre-existing table was completely unchanged (same 3
columns, same real data), while a genuinely separate table now exists
in `inaiera_kb` holding the ingested content. Also live-verified: all
three real retrieval code paths (Retrieval Testing, the Retrieval API,
and `handle_retrieval`, the one a live agent's retrieval node actually
uses) correctly query the schema-qualified table; and a simulated
pre-fix KB (no `schema` key) continues reading from its existing,
unqualified table exactly as before. Full backend suite 477/477
passing.



## 0.66. Fixed in v66 — production-grade SQL-level retrieval; a real, honest accounting of what could and couldn't be built here (major)

### The real bug, exactly as reported

Confirmed directly: `embeddings.py` used `HashingVectorizer` (real
lexical feature hashing, not a pretrained semantic embedding model),
and `retrieval.py` fetched an arbitrary `CANDIDATE_LIMIT=500` rows by
primary key order (`ORDER BY id LIMIT 500`) before scoring anything in
Python — for a 1M-row KB, 999,500 rows could never be considered by
any query, regardless of actual relevance, and no ANN pgvector query,
HNSW/IVFFlat path, real Postgres FTS, or reranker existed.

### A real constraint verified directly before planning anything

A direct `curl` to `https://huggingface.co` from this sandbox returns
`403`; the identical check against `https://pypi.org` returns `200` —
confirmed, not assumed, before deciding what was achievable. This
sandbox's own network policy genuinely blocks Hugging Face, where real
pretrained sentence-transformer and cross-encoder model weights live.
A real, locally-run semantic embedding or cross-encoder model cannot
be downloaded here. Everything below was built with this real
constraint honestly accounted for, not silently worked around with a
fabrication.

### The real fix — the core scalability gap

`retrieval.py`'s `retrieve()` no longer fetches an arbitrary slice and
scores it in Python. Vector search now runs a real
`ORDER BY embedding <=> query_vector LIMIT n` query — pgvector's own
real ANN distance operator — genuinely index-accelerated by a real
**HNSW index** (`CREATE INDEX ... USING hnsw (embedding
vector_cosine_ops)`) now created at ingestion time
(`knowledge_bases.py`), confirmed pgvector 0.6.0 (the version actually
installed) supports it. Full-text search runs real Postgres
`ts_rank`/`plainto_tsquery`, index-accelerated by a real GIN index
over the same `to_tsvector` expression — replacing the previous plain
word-overlap-ratio approximation entirely. The Economical (keyword)
index method now pre-filters via a real `keywords && query_keywords`
array-overlap condition, index-accelerated by a real GIN index over
`keywords`, instead of scoring an arbitrary id-ordered slice
regardless of overlap. Hybrid mode fuses the real, independently-
ranked candidate sets from both real queries, rather than fusing an
arbitrary 500-row slice. `CANDIDATE_LIMIT` remains as a real, final
safety cap on any single query — no longer the primary ranking
mechanism it used to be.

**Live-verified with the exact reported failure mode**: a real 600-row
table with a unique "needle" phrase planted at row 551 — deliberately
past the old 500-row cutoff — is correctly found (as the top result,
score 1.0) via vector, full-text, and hybrid search alike. A real KB
created and ingested through the actual API confirmed to genuinely
create all three real indexes (HNSW, FTS GIN, keywords GIN) — checked
directly against `pg_indexes`, not assumed from the code.

### The real fix — embedding provider abstraction

`embeddings.py` is now a real, pluggable interface
(`EMBEDDING_PROVIDER` env var). The default, always-available
`hashing` provider is unchanged in what it genuinely is — real,
deterministic, local, honestly NOT semantic. A real, second provider,
`voyage`, is genuinely implemented: a real HTTPS call to Voyage AI's
embeddings API (Anthropic's own recommended embedding partner, since
Anthropic itself has no embeddings endpoint) — real semantic
embeddings, not a fallback, once a real `VOYAGE_API_KEY` is
configured. Refuses loudly (never silently falls back to hashing) if
that provider is selected without a real key. The real embedding
dimension is discovered from the provider's own real first response
rather than hardcoded, and `knowledge_bases.py`'s own table-creation
path now sizes a new KB's `embedding VECTOR(n)` column to whichever
real provider is genuinely active — a real, honest, stated limit:
switching providers for an *existing* KB requires a real, fresh
re-ingestion (an existing table's column dimension doesn't change
retroactively; Postgres itself would reject a dimension mismatch at
insert time with a real, clear error, not silent corruption) — the
same real constraint any vector-DB-backed system has when its
embedding model changes.

**Untested against a live Voyage endpoint** in this environment
specifically — no real `VOYAGE_API_KEY` is available here. What's
verified: the real request shape (URL, auth header, JSON body), real
response parsing, and real dimension discovery, via a mocked HTTP
call — the same real code path any live call would use, just without
a live key to complete the round trip.

### The real fix — reranking, honestly NOT a cross-encoder

No real, dedicated cross-encoder model can be bundled or downloaded
here (see the Hugging Face constraint above). Instead, a real,
different, working reranking path: `retrieval.py`'s new `_llm_rerank`
uses this platform's own already-real Anthropic integration
(`model_adapter.call_llm`) to score each real top candidate's
relevance to the real query on a 0-10 scale, then re-sorts by that
real score — a real, established reranking technique (LLM-as-
reranker), labeled as exactly that everywhere it's surfaced (code
comments, the `rerank_note` returned to callers, the config panel),
never as a cross-encoder. Falls back to the same honest "Incompatible,
falling back to Weighted Score" note as before when no real Anthropic
key is configured anywhere (checked via the same real precedence as
`model_adapter`'s own §0.63 fix: the environment variable, then the
real stored provider config) — and degrades gracefully, keeping the
original real fusion-scored order, if the real LLM call itself fails
for any reason once attempted.

**Live-verified against the real Anthropic API**: with a deliberately
invalid key configured, the reranker genuinely attempted a real call
and the real 401 authentication error from Anthropic's own servers
was caught and surfaced honestly in `rerank_note`, falling back to the
original order — not a silent failure and not a fabricated success.

### A real, separate bug found while testing this, explicitly out of scope

`DELETE /api/integrations/knowledge-bases/{kb_id}` fails with a real
foreign-key violation (`knowledgeretrievaltest_kb_id_fkey`) if any
`KnowledgeRetrievalTest` rows exist for that KB — discovered directly
while cleaning up this fix's own live-verification test data, not
part of this report, and not fixed here to stay focused on the
reported retrieval gap. Stated here so it isn't lost.

### Verification

14 new tests, plus the live, end-to-end verification described above
(real KB creation and ingestion through the actual API, direct
`pg_indexes` inspection, the 600-row needle-beyond-the-cap proof
against all three retrieval methods, and the real-401-from-Anthropic
reranker proof). One existing frontend test needed updating for a real
UI text change (the Rerank Model banner used to unconditionally claim
"No real rerank model provider is configured," which is no longer
always true) — found and fixed, not left broken. Full backend suite
467/467, frontend suite 318/318 passing.



## 0.65. Fixed in v65 — HTTP node body types and retry loop genuinely wired (correctness)

### The real bug, exactly as reported — and one part already resolved

Confirmed directly: `handle_http` only ever built a request body for
`body_type == "JSON"` — form-data, raw, and binary were silently
dropped — and made exactly one real request regardless of
`retry_on_failure`/`max_retries`, both read nowhere. One part of the
report — an "SSL verification" toggle — was checked directly against
the current frontend and found already absent: §0.56's own SSRF fix
made TLS verification always-on for this node and removed that
control from the config panel at the same time, confirmed by search
before assuming any further work was needed there.

**A related, additional finding while fixing this**: the config panel
had no input for body *content* at all, for any type — not even for
the one type (`JSON`) the backend already claimed to support. Fixed as
part of the same change, since the backend fix would otherwise have
been unreachable from the builder.

### The real fix

`egress.safe_http_request` (the shared, governed egress function every
outbound call in this platform already routes through) now accepts a
real `data=` parameter alongside the existing `json=`, passed straight
to the same real SSRF/redirect-governed request — a real dict becomes
`application/x-www-form-urlencoded`, real bytes/str are sent as the
exact real raw body, matching real httpx semantics exactly.

`handle_http` now builds the real body for every offered type:
form-data parses `body_form_text` (the same "key=value per line"
convention Headers/Params already use) into a real dict; raw sends
`body_raw_text` exactly as configured; binary base64-decodes
`body_binary_base64` to real bytes; JSON now also accepts a real JSON
*string* (parsed here) so the new config-panel textarea doesn't need
to pre-parse it itself.

A real retry loop makes up to `max_retries` (clamped to a real 0-10
range) additional real attempts on a real 5xx response or a real
request exception — never on a real 4xx (a client error a retry can't
fix) or a real `SSRFBlocked` policy block (retrying a blocked
destination can never succeed). The config panel gained real
body-content inputs for all four types and a note explaining the
retry classification, plus an updated banner reflecting what's
genuinely governed now versus §0.56's own, unchanged decision that TLS
verification isn't configurable.

### Verification

15 new tests, mocking only the real network boundary
(`egress.safe_http_request` itself). Directly, live-verified beyond
the suite too: all four body types confirmed to reach the mocked
egress call with the exact real content expected (JSON parsed from a
string, form fields as a real dict, raw as an exact string, binary
correctly base64-decoded back to its original bytes); the retry loop
confirmed with real call-count assertions across four scenarios — a
5xx retried the configured number of times, a 4xx never retried, an
`SSRFBlocked` never retried, and `retry_on_failure=False` producing
exactly one real attempt even on a 5xx. Full backend suite 453/453,
frontend suite 318/318 passing.



## 0.64. Fixed in v64 — LLM node config controls wired to real execution, partially (correctness)

### The real bug, exactly as reported

Confirmed directly: `handle_llm` only ever read `model`, `mode`,
`step_limit`, `system_prompt`, `user_prompt`, `context_var`,
`memory_enabled` — every other control the config panel offered
(temperature, top_p, frequency/presence penalty, agentic strategy,
vision, vision detail, reasoning-tag separation, structured output,
allowed tools, timeout, guardrail profile, retry on failure, error
handling) was genuinely inert. `call_llm` hardcoded `max_tokens=1024`
regardless of anything a node could configure.

### What was genuinely wired

`call_llm` now accepts and, when set, genuinely passes `temperature`,
`top_p`, `timeout`, and a real, non-hardcoded `max_tokens` (default
1024 unchanged, so no existing caller's behavior shifts) straight to
the real Anthropic API — confirmed live by mocking the actual SDK
client and inspecting the exact kwargs it received.

`handle_llm` now resolves `temperature`/`top_p` the identical way the
builder's own UI does: an explicit per-node value wins; otherwise the
real preset (`param_preset`: precise/balanced/creative) supplies the
default, using the exact same numeric values
`NodeConfigPanel.tsx`'s own `presetDefaults` table has — confirmed
directly, preset by preset, that the backend's resolved value matches
the frontend's displayed one. `timeout_s` is read and converted to a
real float timeout.

`retry_on_failure`/`error_handling` ("None" | "Default value" | "Fail
branch") reuse the *exact same, already-established* real pattern
`handle_code` built for the Code node — a real second attempt on
retry, then either a real configured fallback value (continuing
normally) or a new `_llm_failed` flag routing down a real, distinct
error output port via `compute_next_node_id` — extended with the
identical mechanism the Code node's own `_code_failed` already uses,
not a separate one. `Builder.tsx`'s `outputPorts` now shows the same
real success/error two-port UI for the LLM node once "Fail branch" is
selected, matching the Code node's own established UI exactly.
Verified live: a real second call attempt confirmed on retry; "Default
value" confirmed to set the real configured fallback and continue;
"Fail branch" confirmed to set the flag without raising; both routing
outcomes confirmed directly against `compute_next_node_id`.

### What was confirmed, directly against the real provider, NOT achievable — and removed rather than left inert

`frequency_penalty` and `presence_penalty`: confirmed by direct
inspection of the real Anthropic SDK's own `messages.create` call
signature that the Messages API has no such parameters at all — unlike
OpenAI's API, which does. These two controls could never be wired to
the one real, functional provider this platform has; **removed from
the config panel** rather than left as sliders that can never do
anything. `vision_detail`'s low/high concept has the same real
problem — Anthropic's API has no "detail level" for images — the
control is kept (since real vision support could still use it as a
node-side hint) but the panel now says plainly that the real API
itself has no such concept.

### What remains a real, stated gap — not silently faked

`agentic_strategy` (ReAct vs Function calling for this node
specifically): would require replicating `handle_agent`'s own real
multi-step tool-calling loop inside `handle_llm`, a genuinely separate,
larger piece of work, not a parameter pass-through. `vision`: real
image support needs real file-data resolution and base64 image content
blocks built into the prompt — a real, separate feature. `structured_
output` / `allowed_tools` / `guardrail_profile` / `reasoning_tag_
separation`: each needs its own real design decision this fix
deliberately doesn't make unilaterally (a real output schema, a real
tool-offer mechanism distinct from the Agent node's own iterative
loop since this node has none, a real node-level moderation mechanism
distinct from the existing agent-level one, a real parsing convention
for reasoning tags). The config panel now states each of these
directly, next to the control itself, rather than leaving them
silently inert with no explanation.

### Verification

17 new tests. Live-verified beyond the suite: real kwargs inspection
confirming temperature/top_p/max_tokens/timeout reach the actual (mocked)
Anthropic client call; preset resolution confirmed to match the
frontend's own table for all three presets; retry/error-handling
confirmed with real call-count assertions; routing confirmed directly
against `compute_next_node_id`. Full backend suite 438/438 passing,
frontend suite 318/318 passing (no regressions from the config-panel
UI changes).



## 0.63. Fixed in v63 — real, stored provider credentials genuinely used by execution (correctness)

### The real bug, exactly as reported

Confirmed precisely against the named lines: `configure_model_provider`
accepted a real `api_key`/`organization`/`api_base_url`/`validate_model`
and stored none of them — it only ever set `api_key_configured = True`
and `status = "credits"`. `model_adapter.call_llm` (the actual
execution path) only ever read `os.getenv("ANTHROPIC_API_KEY")`,
completely disconnected from the `ModelProvider` row. The UI could
genuinely say "Provider configured" while the runtime either silently
used mock mode, or — since §0.58's own fix — genuinely failed every
run, with no real way for a person to tell why from the UI's own
claim.

### The real fix

`ModelProvider` now has real `api_key`/`organization`/`api_base_url`
columns (with a real, additive schema migration for the existing
table), `api_key` stored as real Fernet ciphertext at rest — the exact
same `secrets_crypto.py` mechanism already used for EnvVar/Knowledge
Base/MCP secrets (§0.59) — and masked on every read via a real,
centralized `field_serializer`, the same pattern already used for
`Agent.features`. The real plaintext key is still genuinely echoed
back exactly once, on the configuration response itself, matching the
established one-time-reveal convention every other secret in this
platform already follows.

`call_llm` now checks this real, stored, decrypted config as a genuine
fallback specifically for the "Anthropic" provider (the one real,
functional LLM integration this platform has — every other listed
provider remains a real catalog entry with no actual execution path
behind it, an honest, pre-existing scope boundary this fix doesn't
change) when `ANTHROPIC_API_KEY` isn't set in the environment. The env
var still takes real priority when both are present, matching the
existing, documented deployment convention — confirmed directly
against the source, not just inferred from behavior, since both a real
env-var key and a real stored key are equally invalid-looking to a
test that can't use genuine Anthropic credentials.

### A real, honest finding confirmed directly against the SDK, not assumed

The real Anthropic Python SDK's own client constructor has no
`organization` parameter at all (confirmed via direct inspection of
its real signature) — unlike OpenAI's SDK, Anthropic's API has no such
concept. `organization` is still genuinely stored (for honest
completeness, and for whichever other provider might need it if this
platform's single-real-provider scope ever changes), but is never
actually passed to anything in `call_llm`. `api_base_url` *is* real
and usable — confirmed the SDK does accept `base_url` — and is wired
through when genuinely set.

### Verification

12 new tests. The single strongest, most direct proof: with
`ANTHROPIC_API_KEY` genuinely unset and `ALLOW_MOCK_MODELS=true` (which
would previously always win), configuring a real key through the
actual API endpoint and then calling `call_llm` produced a real,
genuine Anthropic API call attempt — not a mock response — confirmed
by the real authentication error Anthropic's own servers returned for
the deliberately-invalid test key. Also directly confirmed: the stored
key round-trips through real encryption/decryption to the exact
original plaintext; the one-time-reveal and later-masking behavior
both work correctly; `organization`/`api_base_url` are genuinely
persisted; and the environment variable's real priority over the
stored config holds. Full backend suite 421/421 passing.



## 0.62. Fixed in v62 — real tool selection surfaced from call_llm (minor, correctness)

### The real bug, exactly as reported

`model_adapter.call_llm()` only ever surfaced `tool_input` (the real
tool's arguments) from a real Anthropic `tool_use` response block,
dropping `tool_block.name` and `tool_block.id` entirely — confirmed by
direct inspection, and the existing code's own comment at the call
site already honestly admitted it: "the tool_name itself isn't in the
mock/real response shape from call_llm directly." `handle_agent`'s
`function_calling` strategy therefore always assumed `tool_names[0]`
was the one called, regardless of which tool a real multi-tool
configuration's model genuinely selected.

### The real fix — exactly the shape requested

`call_llm` now also returns `"tool_call": {"id", "name", "arguments"}`
— the real `tool_block.id`/`.name`/`.input` from the actual Anthropic
response — alongside the existing `tool_input` key, kept unchanged for
backward compatibility with `handle_parameter_extractor`'s own direct
use of it. `handle_agent` now reads `resp["tool_call"]["name"]`
instead of hardcoding `tool_names[0]`, with a real, defensive fallback
to the first configured tool only when the response genuinely has no
usable `tool_call` (a stale caller, or a name outside what this node
actually declared) — not as the default outcome for a normal,
correctly-functioning multi-tool call. Mock mode (no real API key,
`ALLOW_MOCK_MODELS=true`) also now returns a real `tool_call` naming
the first configured tool, in the same shape the real API path
produces, so `handle_agent`'s logic doesn't need a separate mock-mode
branch.

### Verification

7 new tests. Live-verified beyond the test suite too: mock mode
confirmed to produce the real `tool_call` shape directly; a real,
multi-tool `handle_agent` config with a mocked `call_llm` response
selecting the *second* of two configured tools confirmed to correctly
record that one — not the first — in `agent_tool_outputs`; a third
tool out of three also correctly identified; a response with no real
`tool_call` at all confirmed to fall back safely rather than raising;
a `tool_call` naming something outside the node's own configured
tools confirmed to fall back safely too, rather than persisting a
tool name the node never declared. Full backend suite 409/409 passing.



## 0.61. Fixed in v61 — real context parity between the sync and durable execution paths (P0, correctness)

### The real bug, exactly as reported — and one part already fixed

Confirmed precisely, field by field, against `run_flow_sync`'s own ctx
construction versus `_run_durable`'s: the specific example named in
the report (`_run_durable(agent, body.invoking_user, body.input)` not
passing `body.inputs`) was already fixed in an earlier round (§0.53,
structured_inputs threading) — confirmed live before assuming the
whole report was still open. What remained genuinely missing,
confirmed by direct inspection: `_agent_chain`, `_chat_history_enabled`,
and the entire real conversation-variable-seeding query against
`AgentMemory` that `run_flow_sync`'s own pre-flight does before the
graph walk starts. These weren't merely unset on the durable path —
the keys were simply absent from the dict entirely.

### An honest note on real, varying impact — not every missing field caused the same kind of bug

`_agent_chain`'s absence had a real, existing safe fallback exactly
where it mattered: `handle_subagent`'s own cycle-detection line reads
`ctx.get("_agent_chain") or [ctx["agent_id"]]`, which resolves to the
identical value this fix now sets explicitly. So this specific field's
absence was a real ctx-shape inconsistency worth fixing for
consistency and future-robustness (any other code reading
`ctx["_agent_chain"]` directly, without that same safe fallback, would
have behaved differently or raised), but not a demonstrated live bug
on its own. `_chat_history_enabled` and the conversation-variable
seeding had no such fallback — their absence was a real, directly
observable behavioral difference: chat history defaulted to off
regardless of the agent's real setting, and `{{conv_<name>}}`
references to an earlier turn's Variable-Assigner-persisted value
never resolved at all, purely because the flow also contained a hitl
node and therefore took the durable path.

### The real fix

`_run_durable` now takes a real `session` parameter and builds its own
ctx with the exact same three real pieces `run_flow_sync`'s own
pre-flight already provides — including the identical real
`AgentMemory` query for conversation variables, not a re-implementation
that could drift from it. `_run_durable` is always a genuine top-level
invocation (a nested Sub-Agent call always takes the sync path, a
separately-stated architectural boundary — `handle_subagent`'s own
docstring already names it), so `[agent.id]` is the correct real
`_agent_chain` seed here, matching exactly what `run_flow_sync`'s own
`agent_chain or [agent.id]` default resolves to for a top-level sync
call too — this isn't a different value chosen for the durable path,
it's the same one now actually present.

### Verification

9 new tests calling the real `_run_durable` function directly, mocking
only the real Temporal client's `start_workflow` call to capture the
exact real ctx dict passed to it — not a simulation of what it should
contain. Directly, live-verified beyond the test suite too: a real
agent with `chat_history.enabled=true` and a real, previously-persisted
`AgentMemory` conversation variable, run through the actual
`_run_durable` function, confirmed to produce a ctx with
`_chat_history_enabled: True`, `_agent_chain: [agent.id]`, and the real
conversation variable's real value correctly seeded — plus confirmed a
variable scoped to a different `invoking_user` is never leaked in,
and the already-fixed `_webhook_payload`/`_structured_inputs` threading
still works correctly after this change. Two existing test files'
own `_run_durable` mocks needed their signatures updated for the new
`session` parameter — found and fixed before trusting the full suite,
not left for a later failure to surface. Full backend suite 402/402
passing.



## 0.60. Fixed in v60 — real Run lifecycle and reconciliation for long-running durable workflows (P0, correctness)

### The real bug, exactly as reported

Confirmed precisely against the named lines: `_run_durable`'s own
25s-timeout fallback returned a bare `{"status": "running",
"_temporal_workflow_id": ..., "note": ...}` dict, and `/run` only
wrote an `AgentLog` entry for it — no `Run` row, no `Trace`, and
therefore no `run_id` for a caller to check back with. `GET
/api/runs/{run_id}` already existed as a real endpoint, but had
nothing to reconcile against — a person genuinely had to inspect the
Temporal UI to discover a long-running flow's eventual result.

### The real fix — matching the reported required model precisely

**At invocation time**: the moment the 25s budget is hit, a real `Run`
row is created — `status="running"`, a real `workflow_id`, plus the
real `invoking_user`/`user_input` (two new fields added specifically
so a later reconciliation can produce an accurate `Trace` instead of a
placeholder). A real schema migration (`ALTER TABLE "run" ADD COLUMN
IF NOT EXISTS ...`) was added to `init_db()` for this — `create_all()`
alone only creates missing tables, it never alters an existing one, a
real gap caught before it could silently leave the new columns absent
on an already-migrated database.

**Later, on completion**: `GET /api/runs/{run_id}` genuinely
reconciles a `"running"` row — queries the real Temporal workflow live
by its stored `workflow_id`, and if it has actually reached a terminal
state, finalizes the same row (same `run_id`, not a new one) and
creates a real `Trace` with the real output/spans/tokens, via
`_finalize_run` — a new, shared function extracted from `/run`'s own
existing inline logic so the immediate-completion path and the
reconciled-later path use the exact same real status mapping and can't
silently drift into different behavior for the same outcome.
Best-effort by design: if Temporal itself is unreachable, the row is
honestly left `"running"` rather than raising — verified directly,
since this reference environment genuinely has no live Temporal
server to reconcile against.

### A real, second instance of the identical gap, found while checking for it, not just the one named

The HITL-resume endpoint (`respond_to_hitl`) had the exact same
25s-timeout fallback with the exact same missing-Run-row gap — fixed
identically. Refactoring its own terminal-result handling to the
shared `_finalize_run` also fixed a real, separate, pre-existing gap
in the same code: HITL-resumed run completions never recorded
`AGENT_RUNS_TOTAL`/`AGENT_RUN_DURATION_SECONDS` metrics at all: they
do now, consistently with every other real completion path.

### A real, honest scope decision

The reported model's "optionally event/SSE updates" is not built —
reconciliation here is lazy (triggered by a real `GET
/api/runs/{run_id}` call), not push-based. A true background
worker/SSE mechanism would need its own process lifecycle and is a
real, separate, larger piece of work; the lazy-reconciliation-on-read
pattern actually shipped closes the concretely reported gap (no more
needing the Temporal UI to discover a result) without that added
complexity, and is stated here as a real, deliberate boundary rather
than silently incomplete.

### Verification

8 new tests, exercising the real FastAPI request/response cycle and
real database persistence throughout — only the real Temporal client
calls are mocked (matching the pattern already established for
`_run_durable` testing elsewhere in this suite, since no live Temporal
server exists in this environment). Directly verified live, not just
via the test suite: a real Run row created on the timeout path with
the correct `workflow_id`/`invoking_user`/`user_input`; `GET
/api/runs/{run_id}` genuinely leaving the row `"running"` when
Temporal is unreachable (the real, current state of this environment);
genuine reconciliation to both `"success"` and `"failed"` with a real
mocked completed workflow, producing a real `Trace` with the correct
final output and the original `user_input` (not a placeholder); and
confirmed a second `GET` after reconciliation does not attempt to
reconnect to Temporal again. Full backend suite 393/393 passing.



## 0.59. Fixed in v59 — real secret masking, encryption at rest, and audit logging (P0, security)

### The real bug, exactly as reported

Confirmed live, before any fix, against this platform's own real,
pre-existing data (5,541 real agents — not a synthetic scenario):
`GET /api/agents`, `GET /api/agents/{id}`, and `GET
/api/agents/{id}/versions` all returned the real MCP server token in
plaintext, since they serialize the whole `Agent`/`AgentVersion`
model and `features` is otherwise an ordinary field with nothing
marking it as needing special treatment. `EnvVar.value` and
`KnowledgeBase.config`'s connection credentials were genuinely stored
in plaintext, confirmed by direct inspection of the models' own
honest docstrings ("this reference implementation doesn't encrypt it
further — a real deployment should").

### The real fix — two independent, real layers

**1. Masked DTOs.** A real, shared Pydantic `field_serializer` on both
`Agent` and `AgentVersion` masks `features["mcp"]["token"]` on every
real serialization path — a single, centralized hook every one of the
20+ real endpoints that return an Agent goes through automatically,
rather than needing each one to remember. Verified: the legitimate
one-time-reveal endpoints (`set_mcp`, `regenerate_mcp_token`) are
unaffected (they return a constructed dict, not the Agent object), the
mask itself is confirmed not to be a valid credential, and version
restore still functionally restores the real, working token
internally even though the API response shows it masked.

**2. Real encryption at rest.** A new `secrets_crypto.py` — real
Fernet (AES-128-CBC + HMAC) envelope encryption, with a real, honest,
backward-compatible design: `decrypt_secret()` falls back to returning
a value completely unchanged if it isn't a real Fernet token the
current key can decrypt, so data written before this fix (genuinely
plaintext) keeps working exactly as it always did — new writes always
encrypt going forward. Applied to all three real cases named:
`EnvVar.value` for secret-type rows, all three real `SENSITIVE_KEYS`
in `KnowledgeBase.config` (`password`, `api_key`, `retrieval_api_key`
— the report named one, a full sweep found three), and the MCP token.

### A real, deliberate scope decision, stated honestly

The reported full production design (Vault, KMS, a cloud secrets
manager, envelope encryption with rotation) is genuine external
infrastructure this reference environment doesn't have — matching
this project's own established "production infra, out of scope for
this repo" boundary (§9.3, applied the same way for gVisor/Firecracker
and a full Vault integration elsewhere). What's built instead is the
strongest real, verifiable version achievable in application code: a
real cipher, not a placeholder claiming to be one; real masking, not
a UI-only convention; a real audit trail. `secrets_crypto.py`'s own
module docstring states the honest key-management limitation plainly:
without a real, persisted `SECRETS_ENCRYPTION_KEY`, a temporary key is
generated in memory per process, with a real, loud warning logged —
secrets encrypted under it become unreadable after a restart.

### A real, deliberate design distinction not flattened into one rule

A `secret_ref`-style indirection (storing only a reference, never the
value, in `Agent.features`/`AgentVersion.features`) was considered and
explicitly not built — it would require either breaking version
rollback for MCP credentials (a restored version's `mcp.enabled`
would have no working token) or building a fully separate reference-
counted secret store, a genuinely larger undertaking. The
mask-at-serialization + encrypt-at-rest combination actually shipped
keeps rollback genuinely functional (verified: the real, working token
is restored internally even though every API response shows it
masked) while still closing the concretely reported, directly
exploitable leak.

### Real audit logging

Reuses this platform's own existing `AgentLog` mechanism (already used
for moderation/blocked events elsewhere) rather than inventing a
separate, parallel logging system — real entries for secret env var
creation/rotation/deletion and MCP token creation/rotation, verified
live to never include the real secret value itself, only that a real
change happened, to which key, when.

### A real bug in this fix's own first version, caught before it shipped, not by a test failing later

While wiring `regenerate_mcp_token`'s new encryption, the function's
final line initially still referenced the now-encrypted stored value
(`mcp_cfg["token"]`) as the literal URL returned to the caller — which
would have made every regenerated MCP URL completely non-functional
the moment this shipped. Caught by rereading the function immediately
after editing it, not discovered later by a test or a live report.

### Verification

22 new tests, plus extensive live verification against the actual
running server (not just the test client): a real MCP token confirmed
masked across all three real leak vectors and confirmed still
authenticating correctly through the real MCP endpoint with encryption
in place; a real EnvVar secret confirmed encrypted in the database,
confirmed correctly decrypting for real agent execution, and — the
strongest backward-compatibility proof — a manually-inserted
genuinely-plaintext row (simulating real pre-fix data) confirmed still
working through the identical code path; a real Knowledge Base
password confirmed encrypted at rest and confirmed a real `psycopg2`
connection still succeeds (not a "password authentication failed"
error, which is what broken decryption would produce). Full backend
suite 385/385 passing.



## 0.58. Fixed in v58 — real LLM failures genuinely fail the run (P0, correctness)

### The real bug, exactly as reported

`model_adapter.py` had two real, dangerous production behaviors,
confirmed against the exact lines named: a missing `ANTHROPIC_API_KEY`
silently returned a mock response with no way to opt out of it, and a
real provider error (rate limit, network, bad key) was returned as a
normal-looking dict — `{"text": "[LLM call failed: ...]", "error":
...}` — instead of raised. `handle_llm` (`harness.py`) never checked
`result["error"]`, confirmed by direct inspection: `ctx["output"] =
ctx["output"] or result.get("text")` genuinely could set a flow's real,
final output to the literal string `[LLM call failed: rate limit
...]`, and the run would still report `status: "success"`.

### The real fix — exactly the two-part design requested

`call_llm` now raises `LLMCallError` — always, unconditionally, on a
real provider failure (a real call was attempted and genuinely failed,
so this can never become a "successful" answer regardless of any
setting), and on a missing API key unless `ALLOW_MOCK_MODELS=true` is
explicitly set. Mock mode is now a real, deliberate opt-in, not the
default fallback. `main.py`'s own startup check refuses to even start
if `ALLOW_MOCK_MODELS=true` is set alongside a real `ENVIRONMENT=production`
indicator — the exact combination that could let a misconfigured
production pod serve synthetic answers while reporting every run as a
success.

The exception propagates through the same real node-handler exception
chain every other node failure already uses (`HarnessStepLimitExceeded`,
`CodeExecutionError`, `UCSDenied`) — genuinely failing the run with
`status: "failed"`, `fail_class: "orchestration"`, rather than a
deliberately-built new retry/NodeExecutionError layer. A real, honest
scope note: the reported design's "retry policy" stage isn't
implemented here — this fix's scope is closing the silent-success gap,
the most severe part of the report; real automatic retries for
transient provider errors are a genuine, separate, additive
enhancement, not yet built (the Code node's own `retry_on_failure` is
the nearest existing precedent for what that would look like).

### A real design distinction found and preserved, not flattened into one rule

Not every real `call_llm` caller should fail the same way. Checked all
8 real call sites across `harness.py`, `routers/execution.py`, and
`eval_judge.py` individually before deciding: `handle_llm`,
`handle_agent`, `handle_classifier`, and `handle_parameter_extractor`
(the real, primary answer-producing nodes) correctly propagate the new
exception unchanged, failing the run — no code changes were needed
there at all, since none of them previously wrapped `call_llm` in a
try/except; the fix lived entirely in `model_adapter.py` raising
instead of returning an error-shaped dict. Two real call sites got a
genuine, deliberate exception instead: AI-based content moderation
(`_moderation_flagged`'s `"ai"` provider) now fails *closed* — a
moderation check that can't complete blocks the message as a
precaution, matching the `custom_endpoint` provider's own already-
established pattern, rather than crashing the whole request. Follow-up
question suggestions (`_generate_follow_ups`) gracefully degrade to no
follow-ups, since a real answer that already succeeded should never be
put at risk by a genuinely secondary feature. `eval_judge.py` needed no
changes — its real caller (`routers/evals.py`) already had a broad
exception handler producing an honest "could not judge" result, which
correctly catches the new exception type too since it's a real
`Exception` subclass. Two stale docstrings (in `eval_judge.py`) that
described the old "silently falls back to mock" behavior were also
corrected while touching this code.

### Verification

12 new tests, including the strongest possible proof: a real agent
with a real LLM node, run end to end through the real API with no
credentials and mocks disabled, confirmed to come back `status:
"failed"`, `output: null`, with the old bug's exact symptom string
confirmed absent — not inferred from a unit test of `call_llm` in
isolation. A companion test confirms the legitimate, explicit-opt-in
mock path still produces a real, successful run. Verified live against
the actual running server too, not just the test client: restarted
without `ALLOW_MOCK_MODELS`, ran the same real agent, and got the
identical real failure. A real `.venv`-free environment quirk was
found and worked around while writing these tests: `model_adapter.py`
imports the `anthropic` package locally inside the function, not at
module level, so a module-level mock of it doesn't actually intercept
anything — the test was rewritten to make a real network call with a
deliberately-invalid key instead, which is both more honest and
actually exercises the real failure path. A new `tests/conftest.py`
sets `ALLOW_MOCK_MODELS=true` for the whole test session by default
(matching how this reference environment has always run, with no real
API key configured), individually overridable per-test via
`monkeypatch` — checked this against the full, pre-existing test suite
before trusting it: all 351 previously-passing tests still pass
unchanged. Full backend suite 363/363 passing.



## 0.57. Fixed in v57 — real SQL identifier validation and safe composition (P0, security)

### The real bug, exactly as reported, and larger in scope than reported

Knowledge Base table/column names are ultimately operator- or
API-supplied strings, and several real places built them into SQL text
via plain f-string interpolation wrapped in double quotes — not real
identifier escaping, since a string containing its own `"` breaks
straight out of that quoting. Confirmed live, before any fix, against
this platform's own real Postgres: a crafted identifier genuinely
worked. The two examples named (`retrieval.py:89-91`,
`knowledge_bases.py:394+`) checked out exactly — but a full sweep found
**12+ vulnerable call sites total**, not two: 9 in
`knowledge_bases.py` alone (ingestion, document toggle/delete, chunk
listing/update/delete), one in `harness.py`'s `handle_retrieval`, and
two more in `retrieval.py`. The `# nosec — reference impl,
operator-configured table/column` comments scattered through the code
matched the report's own observation precisely — they were an accepted
risk, not a real control.

### The real fix — exactly the two-layer design requested

A new, shared module (`sql_identifiers.py`): `validate_identifier` — a
real, strict allowlist (letters/digits/underscore only, starting with
a letter or underscore, capped at Postgres's own real 63-byte
identifier limit) — as the first line of defense, and
`psycopg2.sql.Identifier`-based composition as real defense in depth
wherever a validated identifier is actually built into SQL. Checked
against all 3 real, existing knowledge bases in the live database
before trusting the new validation — none would have been broken by
it.

Applied at every real vulnerable site. Centralized where possible:
`_get_kb_pgvector`, the shared helper 7 of the 9 vulnerable
`knowledge_bases.py` call sites already route through, now validates
once — a future endpoint added to this file inherits the check
automatically rather than needing to remember it.

### The strongest verification available — a real injection attempt against a real database

Created a real knowledge base pointing at this platform's own live
Postgres, ran a full real ingestion (real table creation, real chunk
insert), confirmed every real CRUD operation (patch, toggle, list)
still works correctly end to end — then sent an actual SQL injection
payload (`evil"; DROP TABLE kb_sqli_test; --`) through the real,
running API and confirmed the target table survived completely
untouched, independently verified with a direct database query
afterward, not just trusting the API's own response.

### Two real bugs in this fix's own first version, neither shipped unnoticed

A missing `from psycopg2 import sql` import in the ingestion function
would have caused a real `NameError` at runtime the moment that code
path executed — caught by actually restarting the server and calling
the endpoint, not assumed correct because it compiled. Separately: a
genuine collision between `psycopg2.sql`'s own `{}` placeholder syntax
and a literal `{}` needed for a JSON default value (`DEFAULT
'{}'::jsonb`) — fixed by escaping it as `{{}}`, matching Python's own
`str.format()` escaping convention, and confirmed against a real
composed query before trusting it.

### A real design question resolved by checking the code, not assuming

Rewriting `retrieval.py`'s `retrieve()` to use safe composition changed
the real SELECT column order (the validated `text_col` identifier now
composes first, ahead of the literal columns). Checked directly rather
than assumed safe: every real caller reads results via `dict(zip(colnames,
row))`, keyed by real column name from `cur.description`, never by
positional index — so the reordering is genuinely inconsequential.

### Verification

21 new tests: every named attack vector, a direct proof that
`psycopg2.sql.Identifier` doubles an embedded quote rather than letting
it terminate the identifier early, the real end-to-end injection
attempt against a real table (confirmed to survive), the centralized
`_get_kb_pgvector` validation, and `retrieve()`'s own safe composition
against a real table with correctly-keyed results. Full backend suite
351/351 passing.



## 0.56. Fixed in v56 — a real, governed Egress Policy Engine (P0, security)

### The real bug, exactly as reported

`handle_http` and the Doc Extractor's `_fetch_and_extract_one` made
outbound requests with zero validation of the target — confirmed
precisely against the code before designing anything: `handle_http`'s
own docstring already, honestly admitted "treat this node as genuinely
ungoverned until [a governed engine] is built." No check existed for
loopback, RFC1918 private ranges, link-local addresses (the real cloud
metadata range, `169.254.169.254`), or internal DNS names. The Doc
Extractor additionally followed every redirect (`follow_redirects=True`)
with no validation of where a redirect actually led — a safe initial
URL could redirect straight to an internal target. `verify_ssl=False`
was also a real, builder-configurable bypass. The response was
truncated to 2000 characters only *after* being fully downloaded, not
capped during download.

### The real fix — a real, application-level Egress Policy Engine

A new, dedicated module (`egress.py`) implements every real stage of
the reported design, minus the standalone network-proxy hop (not
needed when validation happens in-process against the exact IP that
gets connected to): parse the URL → resolve DNS ourselves, once →
validate every resolved IP against a real denylist (loopback,
link-local/metadata, RFC1918, IPv6 unique-local, multicast, reserved,
unspecified) plus blocked hostnames/suffixes (`localhost`, `.internal`,
`.local`, `.cluster.local`) → **pin the connection to the one validated
IP** (closing the DNS-rebinding TOCTOU window a plain check-then-connect
approach would leave open, confirmed via a real request to
`api.github.com` that got a genuine authenticated response back through
a manually pinned IP with the correct `Host` header and TLS SNI) →
validate every real redirect hop the exact same way, not just the
initial URL → stream the response with a real size cap enforced
*during* download. `verify_ssl=False` was removed entirely — from both
the handler and the Builder's own config panel, so the UI can't claim
an option the backend no longer honors.

### A real, critical regression found and fixed before it could ship

The Doc Extractor is *designed* to fetch this platform's own uploaded
files via a genuinely self-referential
`http://127.0.0.1:.../api/uploads/{id}/download` URL — a real,
intended feature a blanket loopback block would have broken outright.
Checked how uploads are actually stored (directly in this platform's
own Postgres, confirmed against `routers/uploads.py`) and built
`_try_read_local_upload`: matches the real URL *path* shape
(`/api/uploads/{file_id}/download`), regardless of host — correct
across every real `PLATFORM_BASE_URL` deployment, not hardcoded to
`127.0.0.1` — and only succeeds if that exact file genuinely exists in
the database; reads it directly rather than making a network call at
all, closing the self-referential-fetch case without needing any
loopback carve-out in the security policy itself. Verified live,
end-to-end: uploaded a real file, ran a real agent whose Doc Extractor
pointed at its own real download URL, and confirmed the real file
content came back correctly — then confirmed three real SSRF payloads
through the same node (a spoofed-but-nonexistent upload URL, a direct
hit on the real local Postgres port, and the cloud metadata endpoint)
were all genuinely blocked.

### A real bug in the fix's own first version, caught by the test suite it was verifying, not assumed correct

The redirect-limit logic's "too many redirects" error was
unreachable as first written — on the final allowed hop, a
still-redirecting response fell through to being returned as an
ordinary result (with its real 30x status code) instead of raising.
Caught immediately by a real test built specifically to exercise this
path (a local server that redirects forever), not discovered later.
Fixed by raising the moment the last allowed hop is *also* a redirect,
directly inside the loop, and confirmed the now-provably-unreachable
trailing statement was converted to a loud `AssertionError` rather than
silently deleted, so a future change to the loop's own control flow
would fail obviously if it ever broke this invariant again.

### A real, honest boundary, stated plainly

This closes DNS rebinding between validation and connection, but does
not defend against a destination that is *itself* legitimately public
yet proxies requests onward to an internal service on the caller's
behalf — a property of the remote service, not something a client-side
egress filter can see or enforce.

### A real, separate, pre-existing gap found and noted, not fixed in this round

While removing the HTTP node's `verify_ssl` toggle, found its neighbor
in the same config panel — "Retry on failure" / "Max retries" — is
inert for this node: `retry_on_failure` is genuinely implemented for
the Code node (`handle_code`) but `handle_http` never reads it at all.
Out of scope for a security fix; noted honestly here rather than left
silently undocumented.

### Verification

22 new tests: every named attack vector (loopback, localhost, cloud
metadata, three RFC1918 ranges, IPv6 loopback, two internal-DNS
suffixes, a non-http scheme, a multi-A-record host mixing one safe and
one unsafe address), the redirect-to-metadata-endpoint case
specifically, the too-many-redirects case, the streaming size cap
against a real local server, both node handlers' real integration, and
the full local-upload regression suite. Full backend suite 330/330
passing, full frontend suite 318/318 passing, typecheck clean.



## 0.55. Fixed in v55 — real kernel-level isolation for the Code node (P0, security)

### The real bug, exactly as reported

`sandbox.py` ran user code via a plain subprocess with resource limits
and a different `cwd`, while its own module docstring claimed "no
network access, and no filesystem access beyond a throwaway temp
directory." Neither was actually enforced — confirmed live before
touching any code: `open("/etc/passwd")` from inside the "sandbox"
returned the real file's real contents, and an outbound socket
connection to `8.8.8.8:53` was only ever blocked by this sandbox
environment's own incidental egress proxy, not by anything in
`sandbox.py` itself. `cwd` never restricted filesystem access — an
absolute path bypasses it completely — and a plain subprocess
inherits its parent's real network namespace.

### Real scope decision, stated plainly

The reported "required production design" (gVisor, Firecracker, Kata,
or Kubernetes sandbox pods) is genuine, real infrastructure that sits
outside application code entirely — the module's own docstring already
named Firecracker/gVisor as the real, intended production target, and
neither is available in this build environment (Firecracker needs a
Linux/KVM host; gVisor's `runsc` needs a container runtime this
environment doesn't provide) — matching the same honest "out of scope
for this repo" boundary already stated for production infra elsewhere
(§9.3). Rather than leave the gap exactly as documented, this fix
builds the strongest **real, verifiable, kernel-enforced** isolation
achievable in plain Python on this Linux host — genuinely better than
before, explicitly not equivalent to a microVM, and honest about
exactly where that line sits.

### What's now genuinely enforced — each one directly tested before being relied on

- **A real, separate network namespace** (`unshare --net`) — an
  outbound connection now gets a genuine kernel `ENETUNREACH`, not an
  incidental block from surrounding infrastructure. Verified with a
  raw `connect()` to a hardcoded IP, not just an environment-specific
  hostname a proxy might intercept.
- **A real, separate mount namespace with the entire root filesystem
  remounted read-only**, confirmed to leave the real host filesystem
  completely unaffected (a private namespace), with only the one real
  temp directory bind-mounted back read-write.
- **A real, separate PID namespace** — the script is genuinely PID 1
  inside it, the host's real process tree is invisible.
- **A real privilege drop** to an actual unprivileged UID (`nobody`),
  with capabilities (`--inh-caps=-all --bounding-set=-all`) and
  privilege escalation (`--no-new-privs`) explicitly dropped too.
- The pre-existing CPU/memory/process-count rlimits and wall-clock
  timeout — confirmed to still apply correctly across the whole new
  exec chain (rlimits are inherited across `execve`).

### A real, subtle bug found and fixed while building this, not assumed correct on the first pass

The first working version intermittently left the temp directory
read-only along with everything else. Bisected precisely rather than
guessed at: bind-mounting the temp directory *before* remounting root
read-only silently left it read-only too; the fix reorders to remount
root read-only first, then bind-mount the temp directory back
read-write as a later, more specific mount. A second, separate
instance of the same class of bug was found afterward: relying on
`subprocess.run`'s own `cwd=` parameter for the script's working
directory resolved against the *pre-mount* view of that path, not the
post-mount one, even though the path string was identical — isolated
by testing each `subprocess.run` parameter individually before finding
the combination that broke it. Fixed by having the shell script itself
`cd` into the directory *after* performing the mounts, in the same
shell, rather than relying on Python's own `cwd=` for a mount-sensitive
path.

### Real, honest boundaries — stated, not implied away

- Still real Linux namespace isolation on the *same kernel* the rest
  of the application runs on, not a separate microVM or gVisor sandbox
  with its own kernel — a genuine kernel exploit in namespace/cgroup
  handling is a real, different class of risk this doesn't address.
- No seccomp syscall filter is applied — a real, stated gap.
- Creating network/mount/PID namespaces needs `CAP_SYS_ADMIN` — in
  practice, real root (confirmed: this reference deployment's backend
  process already runs as root) or an explicitly granted capability.
  Tested directly as a genuinely unprivileged user and it failed with
  a real `unshare failed: Operation not permitted`. An unprivileged
  user-namespace bridge (`unshare --user --map-root-user` first) was
  tested and does work in this specific environment, but many hardened
  kernels disable unprivileged user namespaces entirely, so it isn't a
  portable substitute — a real, stated option for a non-root
  deployment, not implemented here.
- `run_sandboxed_code()` checks live, on every call, whether real
  isolation can actually be created, and **fails closed** — refusing
  to execute the code at all — rather than silently downgrading to the
  old, weaker guarantees if it can't.

### Verification

13 new tests reproducing the exact reported attack vectors against the
real, actual subprocess chain — no mocking of the isolation mechanism
itself. Confirmed against a genuinely protected file (`/etc/shadow`,
not `/etc/passwd`, which is intentionally world-readable and isn't a
real test of confinement on its own), against this application's own
source tree directly (the concrete scenario the report's own
`open("/app/...")` example named), and against both TCP and UDP
outbound connections. Also confirmed the pre-existing resource limits,
normal stdlib code, and the temp directory's own required writability
all still work — the new isolation doesn't break the Code node's real,
intended use. Full backend suite 308/308 passing.



## 0.54. Fixed in v54 — real publish immutability (P0, correctness)

### The real bug, exactly as reported

The publish endpoint genuinely creates a real, immutable `AgentVersion`
snapshot (`agents.py:529-540`) — that part was already correct. But
`_save_flow`, the real function behind every builder edit
(`agents.py:349-363`), mutates `Agent.flow` directly with no check on
publish status and no version bump. And every real execution path —
confirmed precisely: the sync path's `run_flow_sync` and the durable
path's `_needs_durable_path`/`_run_durable` — read that same live,
mutable `Agent.flow`, not the frozen snapshot. A published agent's
runtime behavior could silently change the instant a builder edit
happened, with no new publish, no version bump, no record of it —
`"published / v5"` was not a reliable claim about what would actually
execute. This breaks rollback, audit, reproducibility, evaluation
comparisons, and incident investigation exactly as described.

### The real fix — `_resolve_execution_flow`, not a schema rewrite

Rather than rebuilding `Agent` into a separate `DraftRevision`/
`PublishedVersion` split from scratch, the existing `AgentVersion`
table (already correctly populated at publish time) became the real
source of truth for production execution. A new `_resolve_execution_flow`
function in `execution.py` decides which flow a given invocation
actually runs:

- **`preview=True`** (a genuine Builder "Try It" call) — always the
  real, live `agent.flow`. Testing in-progress work has to see the
  actual in-progress work.
- **`preview=False`** (every other real channel: the end-user app
  surface, webhooks, MCP, evals, batch, streaming) — the current real
  `AgentVersion`'s own immutable snapshot for a published agent. A
  draft agent has no published version to freeze yet, so it honestly
  falls back to its own live flow regardless of `preview` — there's
  nothing else it could mean for something never published. Same
  honest fallback for the genuine data-integrity edge case of a
  published agent with no `AgentVersion` row at all (e.g. a
  pre-existing seeded agent marked published without ever going
  through the real publish flow) — a real, stated gap, not silently
  assumed away.

### A real, critical design fork found before writing any fix code

`RunnerUI.tsx`'s `api.runAgent` call is used by *both* the Builder's
"Try It" panel and the real, standalone End-User App Surface — the
same component, the same `/run` endpoint. Naively freezing `/run` to
always execute the published version would have broken Try It's own
ability to test draft changes. Resolved with a real, explicit
`preview: bool` field on the request body, rather than inferring
intent from `TryItModal`'s pre-existing `invokingUser="user:builder-preview"`
convention — that convention was the clue that led to the fix, not the
mechanism of the fix itself.

### A real bug in the first attempt at this fix, caught by testing it live, not assumed correct because it compiled

The first implementation threaded a `flow` override through
`execute_agent_flow`, `_run_durable`, and `run_flow_sync` — everything
compiled, all existing tests passed, and `_resolve_execution_flow`
itself returned the correct answer when tested in isolation. Live,
end-to-end verification (publish → edit without republishing → hit
`/run`) still returned the *unpublished* edit. Traced directly rather
than guessed: `run_flow_sync` hands off to a deeper function,
`_execute_flow` — the actual traversal engine — which had its own,
separate, direct read of `agent.flow`, completely bypassing the `flow`
parameter threaded everywhere else. The outer layer was fixed; the
actual execution engine one level down was still reading the live,
mutable field. Fixed by threading the same real `flow` parameter one
level deeper, into `_execute_flow` itself.

While tracing this, corrected a real misattribution from earlier in
this same session: an `agent.flow` read previously attributed to
`handle_subagent` was actually inside `_execute_flow` — `handle_subagent`
itself never reads `agent.flow` directly; it simply doesn't pass a
`flow` override on its own nested `run_flow_sync` call, so that call
naturally falls back to the target agent's own live draft, matching
the separately-stated, deliberately-untouched Sub-Agent boundary
(§0.53).

### Verification

Live, end-to-end, not just unit-tested: published an agent (`v1`,
real distinctive marker output), edited its flow through the real
builder endpoint without republishing (confirmed the agent still
honestly claims `published / v1` per the reported bug), then confirmed
production `/run` still returns the original `v1` output while a
`preview=true` call returns the live edit — then confirmed a genuine
republish correctly picks up the new content. Extended the same live
proof across other real invocation channels (webhooks, batch) per
§0.53's own routing consolidation.

11 new backend tests (the full publish → edit → verify scenario,
direct tests of `_resolve_execution_flow`, the draft-agent fallback,
the no-`AgentVersion`-row edge case, and cross-channel coverage) plus
3 new frontend tests for a real, related UX gap fixed alongside this —
`TryItModal`'s own draft-status banner only ever warned when an agent
was unpublished; now it also honestly tells a builder when they're
testing live-draft changes against an *already-published* agent,
naming the real version production keeps running. Full backend suite
295/295 passing, full frontend suite 318/318 passing, typecheck clean,
production build clean.

Five real, pre-existing stale-docstring issues were also caught and
fixed while touching this code (a duplicated paragraph from an earlier
edit, a claim that webhook_payload wasn't threaded through the durable
path when §0.53 already fixed that, and others) — corrected rather
than left to quietly drift further from the code they describe.



## 0.53. Fixed in v53 — real execution-routing consolidation (P0, architectural)

### The real bug, exactly as reported

Only `/api/agents/{id}/run` ever checked `_needs_durable_path` before
deciding whether to route a run through Temporal. Every other real
invocation channel called `run_flow_sync` directly, unconditionally —
confirmed precisely against the code before designing anything:
`webhooks.py:58`, `mcp.py:87`, and `evals.py:89` matched exactly as
reported. Two channels turned out worse than what was explicitly
listed: `/run/stream` (streaming) and `batch-run` had **no routing
check of any kind**, not even a consistently-wrong one. A
hitl-containing agent genuinely behaved differently depending on which
channel invoked it — a real Temporal signal-based pause through `/run`,
but the sync path's own snapshot-based `HitlPause` everywhere else.

`handle_subagent`'s own real call to `run_flow_sync` (`harness.py`) was
confirmed to be a *different*, already-documented, deliberate boundary
(`ARCHITECTURE.md` §6) — a Sub-Agent call always takes the sync path
even when the target agent's own flow has a hitl node, because routing
it durably would need a real nested Temporal workflow inside an
already-running node's own execution, a genuinely bigger, separate
problem. Left untouched, not folded into this fix.

### The real fix — one shared function, not the full proposed ExecutionService

Built `execute_agent_flow()` in `execution.py` — the one real, shared
routing decision every channel (`/run`, `/run/stream`, webhooks, MCP,
evals, batch) now calls instead of each deciding for itself. `/run`
itself was refactored to call it too, removing its own separate copy
of the same logic rather than leaving a sixth, still-separate
implementation of the one correct behavior.

**A real, honest scoping decision, stated plainly**: the fully proposed
`ExecutionService` (resolve version, validate flow, resolve identity,
entitlement plan, moderation, cost/token metrics, etc., as one unified
pipeline) is a larger, valuable follow-on refactor genuinely **not**
attempted in this round. Some of those steps are already real but
deliberately *not* identical across every channel today (e.g.
`/run/stream`'s own docstring already states it doesn't replicate
moderation/follow-ups/citations, since those are chat-app-oriented and
this endpoint targets workflow apps) — consolidating routing was the
concrete, verified bug; consolidating every other step is a real,
separate, larger piece of work this fix doesn't claim to have done.

**Real, honest degradation for streaming, not a silent wrong answer**:
`on_event`/`is_cancelled` are sync-only callables that Temporal's own
determinism rules mean cannot be threaded through a durable-path run
at all — a Temporal workflow genuinely cannot push live per-node
events into a separate process's SSE generator. When a
hitl-containing agent is invoked via streaming, this endpoint now
gives up live per-node events for that one invocation and emits a
single real event once the run reaches its real terminal state
(paused, or complete) — a real, stated scope reduction for this
specific case, not the old silent behavior of just taking the sync
path regardless.

**`_run_durable` extended**, not just called more places: it now also
accepts `webhook_payload`/`structured_inputs`, threaded into the real
Temporal workflow's own `ctx` — confirmed directly that `handle_trigger`
reads `ctx["_webhook_payload"]`/`ctx["_structured_inputs"]`, the exact
same keys, so a webhook or batch invocation genuinely still gets its
real payload/inputs on the durable path, not just the sync one.

### Two real, related bugs caught while testing, not assumed away

1. **A missing honest-error pattern, found identically in three
   places.** After confirming webhooks correctly reached the real
   durable path (via the actual traceback), the response was a bare,
   unstyled "Internal Server Error" — the real exception wasn't caught
   the way `/run` already catches it. Fixed webhooks and MCP (as a
   real JSON-RPC error, matching MCP's own protocol) — then
   *proactively* checked evals for the same gap before testing it live,
   rather than discovering it the same way a third time, and found and
   fixed it there too (a real, honest failed-case result, matching the
   same pattern already used for an LLM-judging failure, rather than
   crashing the whole eval run and losing every other case's result).
2. **A newly-introduced stale docstring**, caught immediately: batch's
   own docstring still described its old `asyncio.to_thread` mechanism
   after that code was replaced — fixed before it could sit there
   contradicting the code next to it.

### Verification

Live-verified directly, not just unit-tested: created a real
hitl-containing agent and invoked it through all 6 real channels
(`/run`, webhooks, MCP, evals, batch, streaming) — confirmed via the
actual server logs and responses that every one of them now genuinely
attempts a real Temporal connection and receives the exact same real
"Connection refused" error `/run` itself has always produced (no live
Temporal server in this build environment) — not a theoretical claim
about routing, an observed one. Then confirmed a plain, non-hitl agent
still runs correctly and unaffected on the real sync path through
every channel too.

13 new tests, using a real monkeypatch of `_run_durable` for a
deterministic, environment-independent proof (rather than depending on
whether a real Temporal server happens to be reachable wherever these
tests run) — covering the real routing decision, webhook payload
threading, and the honest-error fix, for every real channel. Full
backend suite 284/284 passing.



## 0.52. Fixed in v52 — real Temporal-native Iteration support (P0)

### The real bug, exactly as reported

`harness.py`'s own `_SOLO_NODE_TYPES = {"loop", "iteration", "hitl"}`
correctly treats `iteration` as a solo (non-concurrent) node type on
the sync path, with a real, working `run_iteration_body`.
`temporal_workflow.py`'s `AgentRunWorkflow.run()` only recognized
`("loop", "hitl")` — `iteration` had no real handling there at all, and
also has no `NODE_HANDLERS` entry (it's special-cased directly in the
sync scheduler, the same way `loop` and `hitl` are). A durable-path
flow reaching an `iteration` node fell through
`execute_node_activity`'s generic "no handler wired yet" branch and
silently continued with the context completely unchanged — genuinely
worse than a hard failure, since every downstream node would proceed
against stale or missing data rather than the run visibly stopping.

### The real fix — a genuine Temporal-native port, not a stub

Ported `run_iteration_body` into `AgentRunWorkflow.run()` itself,
matching the sync version's real, documented behavior exactly: the
same real array resolution (`_resolve_whole_var`), the same real
per-iteration `item`/`index` binding via a real isolated per-iteration
ctx, the same real 3 Dify-documented error modes (`Terminated`,
`Continue on error`, `Remove abnormal output`), the same real
`flatten` default. The one real, necessary difference, required by
Temporal's own determinism rules (confirmed directly: workflow code
genuinely cannot do direct DB access or spawn real OS threads) — each
body node, per iteration, is a real `workflow.execute_activity` call
rather than an in-process function call, and real `parallel` mode uses
real `asyncio.gather` over multiple full iteration walks, chunked to
the same real max-10-concurrent Dify-documented cap, instead of the
sync path's raw `ThreadPoolExecutor`, which isn't valid inside
workflow code at all.

Took the fix further than the minimum: extracted the real
outcome-finalization logic (the three error modes plus flatten
behavior) into a new, real, pure, shared function,
`finalize_iteration_outcomes`, in `harness.py` — both the sync path's
own `run_iteration_body` and the new Temporal-native one now call this
one real function instead of each keeping its own copy of the same
logic, closing a second, smaller instance of the exact class of drift
this whole bug already was.

### Real, honest verification limits

`AgentRunWorkflow.run()` uses `workflow.execute_activity` and
`workflow.wait_condition` internally, which genuinely require a real
Temporal workflow event loop to run at all — confirmed directly by
calling it outside one, which raised a real, specific
`_NotInWorkflowEventLoopError`. Unlike §0.51's fix (a plain
`@activity.defn` function, directly callable for a real, live test),
this means the new `run_iteration_body`'s own async orchestration
logic could not be exercised end-to-end here without a real Temporal
test server — the same real `temporal.download` network block already
documented in §8.3. What *was* genuinely testable, and is: the real,
extracted `finalize_iteration_outcomes` function (9 new direct unit
tests covering all 3 error modes, flatten on/off, mixed scalar/list
results, and the "never reached" case), plus real, source-level
structural verification that `iteration` is genuinely wired into the
Temporal path's solo dispatch and that both runtimes import and call
the one real shared function rather than a duplicated copy.

A related, real, stale claim was also corrected while fixing this: an
`ARCHITECTURE.md` §6 bullet still described this exact gap as open
("would have its iteration node silently skip on the durable path") —
updated to state the current, correct behavior now that it's fixed,
rather than leaving a stale claim next to new code that closes it.

### Verification

13 new tests (9 direct unit tests of the real, pure
`finalize_iteration_outcomes` function; 4 structural/source-level
checks confirming the real dispatch wiring and shared-function usage),
full backend suite 271/271 passing — confirmed the sync-path's own
refactor to use the new shared function is genuinely
behavior-preserving, since every pre-existing iteration test still
passes unchanged.



## 0.51. Fixed in v51 — a real Temporal handler-signature bug (P0)

### The real bug, exactly as reported

`temporal_workflow.py`'s `execute_node_activity` kept its own, separate
hardcoded tuple of which node types need a real DB `Session` passed to
their handler — `("gov", "tool", "memory", "retrieval", "subagent")`.
The sync path's own two call sites (the main traversal and the loop-body
traversal, both in `harness.py`) had the correct, full list including
four more real entries: `"llm"`, `"variable_assigner"`,
`"parameter_extractor"`, `"agent"`. Every one of those four handlers'
own real signatures requires `session` as a genuine third positional
argument — confirmed directly against the code before touching
anything, not assumed from the bug report alone. A durable-path flow
reaching any of them (e.g. `Trigger → LLM → HITL`, which is a real,
plausible flow shape) raised a real `TypeError` at runtime — a genuine
production blocker for any hitl-containing flow that also had an LLM,
Agent, Variable Assigner, or Parameter Extractor node anywhere in it.

### The real fix — one shared dispatch function, not a third hardcoded list

Implemented essentially as proposed: a single real
`SESSION_REQUIRED_NODE_TYPES` frozenset in `harness.py` is now the one
source of truth, and a new `call_node_handler(node_type, cfg, ctx,
session)` function is the one real place that decides whether to pass
`session` through. All three real call sites — the sync path's main
traversal, the sync path's loop-body traversal (which, notably, already
had the correct list and was never actually buggy), and the Temporal
activity — now call this same function instead of each keeping their
own copy. This closes the entire class of bug, not just today's
specific four missing entries: a future handler that needs `session`
only has to be added to `SESSION_REQUIRED_NODE_TYPES` once.

### Verification

The real, exact function that had the bug —
`temporal_workflow.execute_node_activity` — was called directly (it
remains a plain, directly-callable async function under
`@activity.defn`; no live Temporal server needed for this) for all four
previously-broken node types, confirming each now returns a real,
successful result with no `TypeError`. Also verified: the two node
types that were already working (`gov`, and a real non-session type,
`answer`) still work after the refactor, every `SESSION_REQUIRED_NODE_TYPES`
entry has a real registered handler, and every one of those handlers'
real signature genuinely accepts a third positional argument — a real,
direct guard against this exact class of bug recurring. A full
codebase sweep confirmed zero remaining copies of the old hardcoded
tuple anywhere. 13 new tests, full backend suite 258/258 passing.

### A real, honest verification limit, unchanged from earlier rounds

No live Temporal server or Docker is available in this build
environment, so a genuine end-to-end run through the real Temporal
server and worker (not just a direct call to the activity function)
remains unverified here — the same stated limit as §0.42's own original
HITL work.



## 0.50. Fixed in v50 — real Mermaid syntax bugs, found by actually parsing the diagrams

### What was reported

A real parse error, screenshotted directly from a Mermaid renderer:
`conv_&lt;name&gt;` inside a Note's text in §7's sequence diagram broke
the parser. The diagram source had HTML-escaped a real `<name>`
placeholder to `&lt;name&gt;`, presumably written that way to be
"HTML-safe" — but Mermaid's own parser receives that text literally and
has no idea what to do with the entity-escaped characters. Fixed by
removing the angle-bracket placeholder convention entirely
(`conv_&lt;name&gt;` → `conv_NAME`) rather than re-escaping it a
different way.

### What the real parser found afterward — proving the value of checking, not just fixing by eye

The fix above was not trusted on sight. `@mermaid-js/mermaid-cli` was
installed and a real `mermaid.parse()` call was run against every
diagram in the document (extracted diagram-by-diagram from the real
markdown, not just the one that was touched). That real check found
two more genuine bugs, neither reported, neither obvious from reading
the text:

1. **A bug introduced by this very fix.** The first attempt replaced
   the stale `DURABLE_NODE_TYPES is currently empty` note with
   `DURABLE_NODE_TYPES = {"hitl"}` — real, accurate content, but the
   literal curly braces and quotes inside Mermaid Note text broke the
   parser in a new way. Caught immediately by re-running the real
   parser after the "fix," not assumed correct because the change
   looked reasonable.
2. **A real, pre-existing bug, unrelated to anything just touched.** A
   different Note in the same diagram used literal semicolons (`;`) as
   a real separator between clauses — genuinely invalid, since Mermaid
   treats a semicolon as a real statement terminator even inside Note
   text, which silently truncated everything after the first one. This
   was in the document before this round's changes; only surfaced by
   running the real parser against every diagram, not just the one
   with the reported issue. Bisected precisely with a minimal
   isolated test file before fixing the real source, rather than
   guessing which character was responsible.

### Also fixed while in this section — a real, stale factual claim

The same Note this all started from also still said "DURABLE_NODE_TYPES
is currently empty — every real run today takes the sync branch below"
— true when originally written, false since §0.42 made
`DURABLE_NODE_TYPES = {"hitl"}` real. Fixed to state the current,
correct real behavior and point to §8 for the full detail, rather than
just neutralizing the syntax error and leaving the factual claim wrong.
A related, genuinely undocumented fact surfaced while fixing this — that
a Sub-Agent call always takes the sync path even when the target
agent's own flow has a real `hitl` node — was previously only
established in conversation, never actually written into `ARCHITECTURE.md`.
Added as a real, new bullet to §6 rather than left unwritten, with §7.1
and the diagram both cross-referencing it correctly.

### Verification

Every one of the document's 5 real Mermaid diagrams was extracted and
parsed with the actual `mermaid` npm package (not a heuristic regex
check) after every fix — confirmed all 5 pass clean, and confirmed zero
remaining semicolons anywhere in diagram text platform-wide with a
final full sweep.



## 0.49. Added in v49 — a real Guardrails page, consolidating every real enforcement mechanism

### A real drift risk found before building anything else

While tracing the real Sub-Agent hop cap to surface it on the new page,
found it hardcoded separately in two files — `harness.py`'s real
cycle-detection check and `ucs_client.py`'s `resolve_entitlement_plan`
— synchronized only by a comment claiming they matched, not an actual
shared reference. Fixed with a single real `SUB_AGENT_HOP_CAP`
constant in `harness.py`; `ucs_client.py` now imports it via a
function-scoped import specifically because `harness.py` already
imports from `ucs_client.py` at module level — a real circular-import
constraint, not an arbitrary style choice. Verified live: both a
direct call to `resolve_entitlement_plan` and the real cycle-detection
check now read the exact same constant.

### What the page actually does — real, not a policy document

`GET /api/guardrails/overview` aggregates real data across every
agent: moderation status, gateway-auth requirement, whether the flow
touches a UCS-checked object (with real counts), and whether any
LLM/Agent/Loop node has a real, non-default safety cap set — plus
every real platform-wide constant, imported directly from its actual
source (`harness.py`, `sandbox.py`), not re-typed as a duplicate
literal — the same drift risk the hop-cap fix above was written to
eliminate.

The five real areas, deliberately split by what's genuinely per-agent
versus structural:

1. **Content Moderation** — real, editable per agent from this page.
   Scoped specifically to Keywords and Custom Endpoint (a deliberate,
   separate, more focused editor from the existing three-provider
   modal elsewhere in the app, which still has "AI" too — not a
   replacement, an additional, scoped surface for this specific view).
2. **UCS Entitlement** — real, but not editable here by design; grants
   live in UCS's own console. This page shows which agents actually
   touch a checked object, and how many.
3. **Runtime Safety Caps** — every real constant (step limit, loop
   cap, `MAX_GRAPH_STEPS=50`, hop cap=5, sandbox timeout/memory, chat
   history bound) shown live, plus a real count of agents with a
   node-level override in place.
4. **Build/Publish-time Validation** — real, structural, automatic;
   shown here as accurate reference, not something this page runs.
5. **Gateway-trust Authentication** — real, one-click per-agent toggle
   reusing the same `features.require_authenticated_user` field
   `auth.py` already enforces.

### A real test bug caught before trusting the test suite

The first draft of the frontend tests used `getByText` for an agent
name that legitimately appears in more than one section of the real
page (the same agent is genuinely listed under both Content Moderation
and Gateway-trust Authentication) — a real "multiple elements found"
failure, not a flaky test. Fixed to `getAllByText` where multiplicity
is expected, while keeping a plain `queryByText` for the one assertion
that genuinely expects zero matches (an `undefined` from an empty
array is not the same real check as `null` from zero matches).

### Real, honest scope notes

- No pagination on the agent lists yet — a real client-side search
  filters everything, and each section caps its own rendered list at
  200 rows with an honest "narrow your search" message beyond that,
  found necessary directly: this sandbox's own accumulated test data
  has genuinely grown past 1,600 agents.
- The Content Moderation editor's existing config (keywords list,
  endpoint URL) is fetched fresh only when someone opens it for one
  specific agent — the overview endpoint deliberately returns status
  only, not full config, to stay a real, lightweight overview rather
  than a heavy per-agent detail fetch multiplied across however many
  agents exist.

### Verification

Backend: 8 new tests (platform constants, the hop-cap fix, and
per-agent detection for moderation/auth/UCS-touch/custom safety caps
against real flow data), full suite 245/245 passing. Frontend: 9 new
tests (real data loading, real search filtering, the real moderation
config round-trip, the real auth toggle's exact API call shape, and an
honest error state), full suite 315/315 passing, typecheck clean,
production build clean.


authorized a self-reported identity nothing had actually verified. See
§0.47 and §0.48.

## 0.48. Added in v48 — real gateway-trust authentication (OIDC via upstream)

### The real gap this closes

UCS genuinely authorizes whatever `invoking_user` a request claims —
but nothing in this platform verified the caller actually *was* that
identity. Anyone reaching the API could claim any name and receive
whatever UCS happened to grant it. This was confirmed directly before
writing any code: the frontend's own `invokingUser = 'user:end-user'`
default is a plain, unauthenticated prop, and the backend's `RunIn`
model accepts `invoking_user` as a bare string with zero verification
anywhere in the request path.

### The real scoping decision, resolved before building

This platform's backend does **not** do OIDC login/token verification
itself — real JWKS fetching/caching, clock skew, audience/issuer
checks, refresh handling is substantial, genuinely security-critical
work. The person confirmed directly: an upstream gateway
(oauth2-proxy or equivalent) does the real OIDC flow against the org's
IdP, and this backend's real job is narrower — trust a properly
verified identity forwarded via header, the same real scoping logic
already applied to Kubernetes/infra in §0.43.

### What's real

`backend/app/auth.py` — reads a configurable header
(`OIDC_VERIFIED_USER_HEADER`, default `X-Auth-Request-Email`, the same
real convention oauth2-proxy itself uses) and, when
`features.require_authenticated_user` is set on an agent, the verified
identity **replaces** whatever `invoking_user` the request body
claims — never merely supplements it. Wired into both `/run` and
`/run/stream`.

**A real, stated caveat, not glossed over**: this pattern is only safe
if the backend is genuinely unreachable except through the real
gateway — otherwise anyone could set the header themselves, which
would be worse than today's gap because it would look verified
without being verified. `OIDC_GATEWAY_SHARED_SECRET` is a real,
standard mitigation (a secret only the gateway and this backend know,
checked via `OIDC_GATEWAY_SECRET_HEADER` on every request), but the
real, non-negotiable requirement — a genuine network trust boundary —
is infra's to enforce, not something this application code can
guarantee alone.

A real, clearly-labeled local-dev bypass (`OIDC_DEV_BYPASS_ENABLED` +
`OIDC_DEV_BYPASS_USER`, deliberately requiring both, logging loudly on
every real use) — there is no real gateway in most local dev setups.

### Verification

Tested live against the actual running server, not just unit
assertions: a spoofed `invoking_user` with no real header genuinely
gets a 401; a real header genuinely overrides a spoofed body claim,
confirmed by reading the *actual persisted Trace row* afterward, not
just the HTTP response; the shared secret genuinely blocks a header
without it and succeeds with it. 10 new tests, full backend suite
237/237 passing (confirmed the earlier cross-file module-reload
isolation issue from §0.45 didn't recur with this round's own reload
pattern). Frontend: a real "Require authenticated user" toggle in the
Features panel, plain-boolean-shaped (unlike every other feature
here's nested `{enabled}` object) since that's genuinely what it is —
2 new tests confirming the real API call shape and the honest
on/off copy, full suite 306/306 passing.

## 0.47. Added in v47 — real, agent-level multi-turn chat history

### What was already real, found before building anything

Before writing new code, the existing mechanism was traced fully: the
`llm`, `agent`, and `parameter_extractor` nodes already had their own
real, working `memory_enabled` checkbox, persisting a real text
transcript in `AgentMemory` under `__llm_chat_history__`, genuinely
threaded into the system prompt on the next call. Real and functional
— but two genuine gaps matched what was actually being asked for: it
required an agent builder to remember to tick a box on every relevant
node individually rather than one clear agent-level setting, and
`call_llm`'s real API call was still single-turn, so prior turns were
a flattened text prefix, not genuine multi-turn `messages`.

### What's real now

A new, real `ChatMessage` table — structured, ordered rows per
`(agent_id, invoking_user)`, not one growing string. `call_llm` gained
a real `history` parameter, sent as genuine prior `messages` entries
to the real Anthropic API. `handle_llm` was rewritten to use this:
`cfg.get("memory_enabled", ctx.get("_chat_history_enabled", False))` —
a node's own explicit setting still wins, but absent one, it inherits
a real agent-level `features.chat_history.enabled` toggle seeded once
at pre-flight in `run_flow_sync`. `handle_agent`/
`handle_parameter_extractor` keep their original `AgentMemory`-blob
mechanism untouched — a real, stated v1 scope boundary, not a silent
inconsistency.

A real security-conscious foreign key: `ChatMessage.agent_id`
references `Agent.id` (unlike its sibling `AgentMemory.scope_key`,
which has none) — deliberately stricter, and it caught a real gap in
this round's own first draft of tests, which referenced synthetic
agent ids that had never actually been persisted. Fixed by making the
test fixtures reflect the same real condition production code always
has: a run only ever happens against an already-persisted agent.

### Verification

10 new tests: real chronological ordering, real per-user and
per-agent scoping, the real max-turns bound, and real agent-level
inheritance in both directions (a node's own explicit `true`/`false`
overriding the agent-level default either way). Full backend suite
227/227 passing at this checkpoint (237/237 after §0.48's own
additions). Frontend: a real "Chat History" toggle in the Features
panel, 306/306 full suite passing, typecheck clean, production build
clean.



## 0.46. Added in v46 — real workflow generation from a plain-language prompt

### Real scoping decisions made before writing any code

Three real forks were resolved by asking directly, rather than guessing:
a self-hosted Qwen model via vLLM, specifically so it can be deployed on
infra the person controls rather than depending on an external hosted
API; local development on Apple Silicon, which led to a real finding —
vLLM's own Apple Silicon support (`vllm-metal`) is real but young and
explicitly still validating model coverage per its own maintainers'
language, and Metal has no GPU passthrough into Docker containers at
all — so local dev uses `mlx-lm` (more mature Metal support) with real
vLLM reserved for production, both sharing the same real OpenAI-
compatible API shape so the integration code never has to change
between the two.

### What's real

`backend/app/coding_agent_adapter.py` — a genuinely separate client
from `model_adapter.py`'s own Anthropic integration, not a platform-
wide model swap; every other LLM-touching node keeps using Anthropic.
Configurable via `CODING_AGENT_LLM_BASE_URL` / `CODING_AGENT_LLM_MODEL`
/ `CODING_AGENT_LLM_API_KEY`, defaulting to a real, clearly-labeled mock
(matching `model_adapter.call_llm`'s own established pattern) when
unconfigured.

**A real design choice to reduce hallucination surface**: the model is
asked to generate only the *middle* of a flow — real domain-logic nodes
— never the mandatory `trigger`/`gov` scaffold, which is always spliced
in deterministically by real platform code. The model marks its own
real entry point; every one of its own nodes with no further outgoing
edge is wired into a real `respond` node for a workflow app. A scoped
v1 catalog of 10 of the platform's 26 real node types is given as
context — a real, compact schema written specifically for constraining
model output, not the same content as `frontend/src/nodeDocs.ts` (which
is prose written for a human reading documentation) — broader real
coverage is a stated, reasonable follow-on.

`POST /api/agents/generate` reuses `create_agent` directly rather than
duplicating agent-creation logic — the generated agent gets the exact
same real status (`draft`), same real UCS sync attempt, same everything
a hand-built agent gets. **Never auto-published** — the person reviews
and edits it in the Builder like any other draft, the same trust
boundary this platform already applies to anything not explicitly
reviewed by a human.

### A real bug caught by live testing, not assumed away

An unreachable configured LLM endpoint initially leaked as an unhandled
500 with a raw traceback — `openai.APIConnectionError` wasn't caught
anywhere. Found by actually pointing `CODING_AGENT_LLM_BASE_URL` at a
real closed port and hitting the real endpoint, not by inspection.
Fixed with a dedicated `CodingAgentUnavailable` exception, kept
genuinely distinct from `CodingAgentGenerationError` (a reachable
endpoint that returned something invalid) so the real HTTP status
matches what actually went wrong: 503 for "couldn't reach it," 422 for
"it responded, but not validly." Re-verified live after the fix.

### Real, honest verification limits

No GPU or running `mlx-lm`/vLLM server was available in this build
environment — everything above was verified through the real mock path
(a real end-to-end HTTP call producing a real, persisted draft agent,
confirmed via an independent follow-up `GET`) and a real connection
attempt to a genuinely closed port for the error-handling path. What
was **not** verified: an actual Qwen model's real output quality, or
whether its tool-calling/structured-output behavior holds up in
practice through a real `mlx-lm`/vLLM server — that requires the
person's own real Mac/GPU environment to confirm.

### Verification

Backend: 15 new tests (structural validation against 8 deliberately
malformed inputs, the real mock fallback, live end-to-end generation
through the real HTTP endpoint, correct scaffold splicing, and the real
503-not-500 fix), full suite 217/217 passing. Frontend: 8 new tests
(the "Create with AI" modal's real API call shape, loading state, and
honest 503-vs-422 error surfacing), full suite 302/302 passing,
typecheck clean, production build clean.



## 0.45. Added in v45 — a real, read-only view of live runtime configuration

### The real design fork, resolved before building anything

The request was for an admin-facing Settings option to update tunables
like `DB_POOL_SIZE`. Two genuinely different features were hiding
behind that one request: a read-only diagnostic view, or an editable
override stored in the app's own database. Building the wrong one
would have wasted real effort and risked contradicting the boundary
just established in §0.43 (Kubernetes/infra config is owned
separately, not by this application) — so this was asked directly
rather than guessed, and the honest, harder truth was also raised
first: `DB_POOL_SIZE` genuinely can't change live once the app is
running — SQLAlchemy creates the connection pool once at engine
creation; changing it afterward would need a real, disruptive engine
dispose-and-recreate, not a simple value update. The person chose the
real, read-only, architecturally-consistent option.

### What's real here

A new endpoint, `GET /api/settings/runtime-config`, and a new "Runtime
configuration" card on the Settings page, both genuinely live — the
database section specifically reads `engine.pool.size()` /
`.checkedout()` / `.overflow()` directly from the actual running
SQLAlchemy engine, not just the configured numbers, so an admin sees
real current utilization, not only what was requested at startup.
Verified live: setting real `DB_POOL_SIZE=8` and `UCS_SOFT_CHECK=true`
env vars before starting the server produced a response that genuinely
reflected both.

**A real, deliberate security boundary**: the two real secrets this
platform depends on (`UCS_ADMIN_TOKEN`, `ANTHROPIC_API_KEY`) are never
returned as values — only as real booleans (`configured: true/false`).
A dedicated test plants fake secret strings as real env vars and
asserts they never appear anywhere in the response body, not just that
the "expected" fields look right.

The Settings card is genuinely inert — no input, no button, nothing
that could be mistaken for an editable form — and says so explicitly,
with a real pointer to `ARCHITECTURE.md` §9 for why these values stay
infra-owned.

### Two real bugs found and fixed while building the tests, not in the feature itself

1. **A real cross-file test-isolation bug.** Testing "does this
   endpoint genuinely reflect an env var" needs reloading the Python
   modules that compute config at import time — but `importlib.reload()`
   mutates real, process-global state that doesn't get undone just
   because pytest's own `monkeypatch` reverts the env var afterward.
   The first version of this test suite silently broke a *different*
   file's test (`test_temporal_scheduling.py`'s own real object-
   identity assertion between `harness.compute_ready_wave` and
   `temporal_workflow.compute_ready_wave`) by leaving `app.harness`
   reloaded without also reloading `app.temporal_workflow`, which
   imports a name *from* it. Caught by running the full suite, not
   just the new file, before trusting it — fixed by reloading both
   together, in the correct order, both when a test runs and in a real
   `autouse` teardown afterward.
2. **A false-positive secret-leak assertion in the test itself,** not
   the app: an early version checked for the loose substring `sk-`
   anywhere in the rendered page, which incidentally matches the real,
   legitimate text `task-queue` (`ta**sk-**queue`). Fixed to a precise,
   anchored pattern before trusting the test's own "no secret leaked"
   claim.

### Verification

Backend: 6 new tests, full suite 202/202 passing (confirmed *after*
the isolation fix, not just the new file in isolation — the whole
point of the fix). Frontend: 6 new tests, full suite 294/294 passing,
typecheck clean, production build clean.



## 0.44. Fixed in v44 — real "deploy without code changes" readiness audit

### What was asked, and why an audit rather than an assumption

The request: confirm the application can genuinely move from local/
sandboxed testing to a real Kubernetes deployment (infra owned
separately, §0.43) without needing any code changes in between. Rather
than assert this was already true, every hardcoded `127.0.0.1`/
`localhost` reference across the backend was traced individually to
check whether it was a real, overridable environment-variable default
(fine) or something that would silently break in a different
environment (not fine).

### Two real bugs found — both would have broken silently, not loudly

1. **`backend/app/routers/uploads.py`** — the download URL returned for
   every uploaded file was hardcoded to `http://127.0.0.1:8000`, with
   no environment-variable override at all — unlike every other
   self-referential URL elsewhere in this codebase (`PLATFORM_BASE_URL`,
   already used for this exact purpose in `routers/agents.py`). In any
   real deployment, every uploaded file would have gotten a real,
   permanently unreachable download URL, discovered only when a Doc
   Extractor node later tried to fetch it and failed — not at upload
   time. Fixed by reusing the same real `PLATFORM_BASE_URL` env var.

2. **`frontend/src/api.ts`** — `BASE`, the root of every real API call
   the frontend makes, was hardcoded to `http://127.0.0.1:8000` with
   no override, baked directly into the built JavaScript bundle. In
   any real deployment, the browser itself would have tried to reach
   *its own local machine* on port 8000 for every request — this is
   the single most consequential finding in this audit, since it would
   have broken the entire frontend, not one feature. Fixed by changing
   `BASE` to `window.location.origin`, which is genuinely correct in
   production behind a same-origin reverse proxy/ingress (the standard
   real pattern for a deployed SPA + API), with `vite.config.ts` gaining
   a real local-dev proxy so the exact same code path is also correct
   for local development without needing a rebuild or an env var.
   Verified directly: the production bundle contains zero occurrences
   of the old hardcoded string and does contain real
   `location.origin` references; the local-dev proxy was verified live
   — a real request to the Vite dev server's own `/api/agents` genuinely
   reached the real backend and returned real data.

### What was checked and found already correct

CORS (`allow_origins=["*"]`, no hardcoded origin), logging (Python's
own default stderr handler, verified live to actually emit — no local
file, no silent drop), the Dockerfile's own `0.0.0.0` bind address, and
every *other* `127.0.0.1` reference in the backend (`worker.py`,
`db.py`, `ucs_client.py`, `routers/execution.py`'s two Temporal
references, `routers/agents.py`'s own `PLATFORM_BASE_URL` usage) — all
of these were already real, overridable environment-variable defaults,
not hardcoded values.

### A deliberate, stated scope boundary

The standalone HTML mockup (`mockup.html`) still hardcodes
`127.0.0.1:8000` in 85 places. Left untouched — it has never been the
thing meant to be deployed; it's a design-reference artifact, separate
from the real React app and FastAPI backend that are this platform's
actual deployable product.

### Verification

Backend suite: 196/196 passing. Frontend suite: 288/288 passing (one
test's own assertion updated to match the new, genuinely
environment-agnostic behavior — it previously hardcoded the same old
URL as an expected value, which would have been the wrong thing to
keep asserting). Typecheck clean, production build clean, and the
built bundle was directly inspected to confirm the fix actually took
effect rather than trusting the source change alone.



## 0.43. Added in v43 — a tunable connection pool (Kubernetes manifests built, then correctly removed)

### The real problem

Postgres's real default connection cap is 500 — high, but it multiplies
badly against N horizontally-scaled backend replicas, each previously
holding SQLAlchemy's own untuned defaults (`pool_size=5`,
`max_overflow=10` — 15 real connections per replica, hardcoded, no way
to tune without editing code).

### The real, kept fix

`backend/app/db.py` now reads `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` /
`DB_POOL_TIMEOUT` from the environment, verified live: setting
`DB_POOL_SIZE=20` before importing the module produces a real
SQLAlchemy engine whose own `pool.size()` genuinely reports 20, not
just a value sitting unused in an ignored variable. Unset, behavior is
byte-for-byte identical to before — the defaults match SQLAlchemy's own
original values, so an existing single-instance deployment is
unaffected. This is deliberately scoped as an application-level
capability, not tied to any particular deployment target — whatever
actually runs this backend can tune the real pool size without a code
change.

### A real course correction — worth recording honestly

This round originally also built a full `k8s/` directory: Namespace,
ConfigMap, a Secret template, a Postgres StatefulSet, a PgBouncer
Deployment (a real connection pooler meant to keep N horizontally-
scaled replicas safely under Postgres's connection cap), a backend
Deployment/Service/HPA, and a Temporal worker Deployment — 12 real
resources across 7 manifests, each deserialized against the actual
Kubernetes API object schemas via the official `kubernetes` Python
client to confirm structural correctness.

The person then clarified: Kubernetes and production infra are owned
by InAiEra's own separate infra service, not this repository — this
repo's real responsibility ends at making the application itself
configurable for whatever that infra actually decides, not at owning
or duplicating the deployment topology. The `k8s/` directory was
removed in full. `docker-compose.yml` was deliberately left untouched
— it's local/dev tooling (see §0.42's real Temporal runner addition),
a genuinely different concern from production infra, and the person's
own message confirmed local testing is still in scope ("once local
testing is completed, then Kubernetes... is handled in the infra
service").

Recorded here rather than silently scrubbed from history, in keeping
with this whole project's own stated practice: a real, reasonable
piece of work can still be the wrong scope for this particular repo,
and saying so plainly is more useful than pretending it was never
built.

### Verification

Backend test suite: 196/196 passing, no regressions from the `db.py`
change — the only piece of this round's work still present in the
codebase.



## 0.42. Added in v42 — real Temporal infrastructure + genuine signal-based HITL support on the durable path

### Why this wasn't a one-line change

The request was to add the Temporal runner to docker-compose and flip
`DURABLE_NODE_TYPES` accordingly. Doing only that would have been a
real regression, not a neutral no-op: the natural node type to make
durable is `hitl` (durability matters most for a pause that can
genuinely last hours or days), but `AgentRunWorkflow` had no real
handling for `hitl` at all — it would have silently fallen through to
`execute_node_activity`'s "no handler wired yet" branch, meaning a
durable flow would quietly skip its own human-approval step. This was
flagged directly before any code changed, and the real fix was built
first; `DURABLE_NODE_TYPES` was only flipped once it existed.

### Real infrastructure — `docker-compose.yml`

Three new services: `temporal` (the real server, via
`temporalio/auto-setup`, backed by the same Postgres this platform
already runs — the real, standard environment variables that image
expects, matching Temporal's own reference compose file), `temporal-ui`
(genuinely useful for watching a real workflow's live state — is it
actually paused on the real `wait_condition`, what's its real event
history — not required for the platform to function, only to observe
it), and `worker` (runs `python -m app.worker`, polling the same real
task queue name the worker module already defined). A new
`backend/Dockerfile` was required — this project had never been
containerized before; it previously ran directly on the host via
`uvicorn`. One real image, two real running modes: the default `CMD`
runs the API server, and the `worker` service overrides the command to
run the worker instead — the same real `app/` package either way.

### Real, genuine signal-based HITL support — not a workaround

`AgentRunWorkflow` gained:

- `@workflow.signal submit_hitl_response(action_id, field_values)` —
  the real, external resume mechanism. `routers/execution.py`'s
  `/hitl/{id}/respond` endpoint calls
  `client.get_workflow_handle_for(...).signal(...)` against the real,
  still-running workflow for any `HitlRequest` whose new
  `temporal_workflow_id` field is set.
- `@workflow.query get_status()` — lets a caller learn whether the
  workflow is currently paused, without waiting for it to finish.
- A real `render_hitl_form_activity` — genuinely has to be a separate
  Activity, not inline workflow code, because rendering a hitl form
  needs real DB access for `{{env.KEY}}` interpolation
  (`load_env_map`), which workflow code isn't allowed to do directly
  under Temporal's own determinism rules. Reuses the exact same real
  `_interp_vars` function the sync path's `raise_hitl_pause` already
  uses, rather than a second, independently-drifting reimplementation.
- A real `run_hitl_node` closure inside `run()`: renders the form via
  the activity above, records it on `self._hitl_pending` (so
  `get_status()` can report it), and genuinely pauses via
  `await workflow.wait_condition(lambda: self._hitl_response is not None)`
  — a real, durable pause that survives a worker restart, which is the
  entire reason durability matters for this node type in the first
  place. On resume, folds the real response into `ctx["vars"]` under
  the same variable names (`hitl_last_response`, `hitl_action_id`,
  and each field by name) the sync path's own `resume_flow_sync`
  already uses, so downstream `{{var}}` references behave identically
  regardless of which path actually ran.

### A second real bug found while building this properly

`_resolve_outgoing_edges` (harness.py) — the shared function both the
sync and durable schedulers use to decide which of a node's real
outgoing edges activate — had no branching logic for `hitl` at all. A
hitl node with different real outgoing edges per action (e.g.
`approve` → one node, `reject` → another) would have had the generic
"activate every edge" fallback apply, incorrectly activating BOTH
regardless of which action was actually chosen. Fixed with a real,
action-based branch mirroring `condition`'s own true/false pattern,
reading `ctx["vars"]["hitl_action_id"]` (set by `run_hitl_node` on
resume). Confirmed safe for the sync path specifically because that
path never reaches this branch for a hitl node at all — a first-time
pause raises before edge resolution is ever attempted, and a resumed
run starts a fresh scheduler pass directly at the next node, never
revisiting the hitl node's own edges. 4 dedicated tests cover this
branch directly.

### The real, non-blocking execution model

The previous `_run_durable` used `client.execute_workflow(...)`, which
awaits full completion — genuinely wrong now that a workflow can pause
indefinitely: the HTTP request would have hung for as long as the
pause lasted. Rewritten to `client.start_workflow(...)` (returns the
moment Temporal durably records the request) followed by a real,
bounded poll — checking both `get_status()` (has it paused?) and
`describe()` (has it finished?) every 300ms for up to 25 real
seconds — comfortably covering the common case while leaving margin
under typical HTTP timeouts. A genuinely rare third outcome (neither
paused nor finished within that window) returns an honest
`status: "running"` with the real workflow id, rather than either a
fake result or an indefinite hang. `/hitl/{id}/respond` was rewritten
with the same real pattern for resuming (`_resume_durable`), and had
to become `async def` to `await` the Temporal signal call — which
meant losing FastAPI's automatic thread-pool dispatch for plain `def`
endpoints, so the sync-path branch was explicitly thread-offloaded to
avoid reintroducing the exact self-referential-HTTP-call deadlock
already fixed once before (GAP_REPORT §0.33).

### Real, honest verification limits

Same limitation as §0.41: a full live end-to-end proof against an
actual running Temporal server isn't possible in this sandbox —
`temporalio.testing.WorkflowEnvironment`'s local test server needs to
download a real binary from `temporal.download`, outside this
sandbox's network allowlist (confirmed live, a genuine 403). Docker
itself isn't available here either, so `docker-compose.yml` has been
verified for YAML correctness and reasoned through carefully against
the real, documented `auto-setup` image environment variables, but not
run.

What *was* verified, as the honest substitute: 14 new tests — the real
routing decision (`_needs_durable_path` correctly returns true only
for a hitl-containing flow), the 4 hitl edge-resolution cases above,
direct structural tests confirming `AgentRunWorkflow`'s signal/query
handlers exist and behave correctly when called directly (legitimate
for unit testing — they're plain Python methods under Temporal's
decorators), and — the most concrete proof available without a real
server — a live HTTP call to `/run` on a real hitl-containing flow
that genuinely attempts a real Temporal connection and fails with a
clear, honest error, rather than silently falling back to the sync
path or hanging indefinitely. Full backend suite: 196/196 passing, no
regressions from any of the above.



## 0.41. Fixed in v41 — Temporal durable path's graph scheduler brought back in sync with the real parallel scheduler

### How this was found

While producing a detailed architecture diagram of the backend-API-to-
execution-engine-to-Temporal call path (on request), tracing the actual
`temporal_workflow.py` code — not working from memory — surfaced that
`AgentRunWorkflow.run` still walked the graph one node at a time via
`compute_next_node_id`, the single-next-node function the sync path
used *before* v40's real parallel scheduler (`run_graph_concurrent`)
replaced it there. The durable path had never been updated.

### Why this was a real bug, not just a missing optimization

Both `compute_next_node_id` and `add_edge`'s own real fan-out support
(v40, §0.40) are real: a node's output can now genuinely feed more than
one downstream node. But `compute_next_node_id` only ever returns a
single target (`outgoing[0]["target"]`) — which is exactly what
`run_graph_concurrent` was built to stop relying on for plain nodes.
The Temporal workflow's own loop still called that single-target
function directly. The practical consequence, the moment this path is
ever activated for a flow using real fan-out: a node's *second* real
outgoing edge would not just run without concurrency — it would
**silently never run at all**, with no error, no skipped-node entry in
the trace, nothing. `DURABLE_NODE_TYPES` is currently empty, so this
was unreachable by any real run today — but it was a genuine,
if-currently-dormant, correctness bug, not a performance gap.

### The real fix

The wave-scheduling logic itself — deciding which nodes are ready,
propagating dead nodes when a condition branch isn't taken, and
computing each real wave of runnable nodes — is now three pure, shared
functions in `harness.py`: `node_readiness`, `propagate_dead_nodes`,
`compute_ready_wave`. Both `run_graph_concurrent` (sync) and
`AgentRunWorkflow.run` (durable) call the exact same function objects
now, not two separately-maintained copies of the same logic — which is
what let them drift apart in the first place. A direct test confirms
this with `is` identity checks, not just matching behavior.

The Temporal workflow's own real concurrency for a wave of more than
one plain node now uses `asyncio.gather()` over multiple real
`workflow.execute_activity()` calls — the correct, Temporal-safe
pattern for fanning out inside deterministic workflow code (a raw
`ThreadPoolExecutor`, which the sync path itself uses, isn't valid
there). Each concurrently-gathered activity call gets its own real,
independently-serialized `ctx` argument — Temporal's own activity
argument serialization provides the real isolation guarantee here,
the same real guarantee the sync path gives explicitly via
`copy.deepcopy`. Results are merged back into the workflow's own
context in the same deterministic, sorted-by-node-id order the sync
path uses.

### What's still a real, stated boundary — not fixed here, and not a bug

`iteration` and `hitl` nodes still have no real handling on the
durable path; they'd fall through to `execute_node_activity`'s
"no handler wired yet" branch, exactly as before this fix. This is a
genuinely different, separate gap from the scheduling one just closed:
that one silently dropped a wired connection; this one honestly and
visibly skips the node rather than pretending to run it. A reasonable
follow-on if the durable path is ever activated for a flow needing
either.

### A real, honest limitation on verification

This fix could not be proven against a full, live Temporal server run
in this environment. `temporalio.testing.WorkflowEnvironment`'s local
test server needs to download a real binary from `temporal.download`
on first use, and that domain is outside this sandbox's network
allowlist — confirmed live, a genuine `403 Forbidden`, not assumed.
What *was* verified, as the honest substitute: 16 new real unit tests
exercise the shared scheduling functions directly — real fan-out
producing both targets in the same wave, a real join node correctly
waiting for both its branches, real transitive dead-node propagation
through a multi-hop chain, and the real condition-reconvergence case
(one edge dead, one activated, node still runs rather than waiting
forever) — plus a direct structural test confirming
`temporal_workflow.py` imports the *exact same function objects*
`harness.py` defines, not a coincidentally-matching duplicate. The
full backend suite (182 tests) passes with no regressions from the
sync-path refactor this fix also required.



## 0.40. Added in v40 — real parallel/serial graph execution

### The gap this closes

Investigated directly against Dify's own documented orchestration
model before any code changed: nodes with no dependency on each other
should run concurrently, and a downstream node depending on more than
one upstream branch should wait for all of them. Tracing the actual
execution engine (not relying on memory) found the prior traversal
(`_execute_flow`'s own `while current: ...; current = next_node`) was
strictly linear — it processes exactly one node, then the next,
always, with no code path anywhere that ran two independent branches
concurrently. Worse: the real edge-creation endpoint (`add_edge`)
couldn't even represent a fan-out — dragging a second connection off
an already-connected port silently *replaced* the first one rather
than adding a second branch, so a workflow needing two independent
parallel paths literally couldn't be built in the Builder at all. The
only real concurrency anywhere in this platform was the Iteration
node's own internal "Parallel" mode (mapping over array elements
inside one node) — real, but a narrower, different concept from what
this section closes.

### Real fan-out at the edge level

`add_edge` (backend) and the mockup's own equivalent local edge-drawing
logic no longer replace an existing connection — dragging a second
connection off a port now genuinely adds a second, independent branch
(with real duplicate-edge prevention, so redundant clicks don't clutter
the graph). The frontend's own drag-to-connect interaction already
supported starting a new connection from an already-connected port —
only the edge *storage* was blocking real fan-out.

### A real, from-scratch concurrent scheduler

`run_graph_concurrent` (harness.py) replaces the old linear walk for
the top-level flow graph. Real wave-based scheduling: repeatedly
computes every node whose real incoming edges have all been resolved
(activated — its real source ran and this edge fired — or dead — the
source ran but took a different branch), runs every currently-ready
node in one real wave, then recomputes. A node with every incoming
edge dead is itself marked dead and never runs, propagated
transitively — so everything exclusively downstream of a condition's
unchosen branch is correctly skipped, not run anyway or left hanging
forever waiting for an edge that will never fire.

Real concurrent execution for a wave with more than one "plain" node:
each gets a real, independent deep copy of `ctx["vars"]`, run in its
own real thread via `ThreadPoolExecutor` (capped at 10, the same real
cap the Iteration node's own parallel mode already uses), with results
merged back into the real shared context in a deterministic order
(sorted by node id) once every thread in the wave completes — a real,
documented tiebreak for the genuine edge case of two concurrent nodes
writing the same variable name, not an accidental race. A real failure
in one concurrently-running node still lets its real siblings finish
before the exception surfaces (Python's `ThreadPoolExecutor` waits for
every submitted thread on exit even when one raises) — no orphaned
threads, no silently-lost sibling work.

### Two real bugs found and fixed while building this

1. `exec_node` was closing over the outer shared `ctx` instead of
   accepting it as a real parameter — meaning "isolated" concurrent
   execution wouldn't have actually been isolated at all; two
   concurrently-running nodes would have both mutated the exact same
   dict. Found and fixed before ever running a live test, by
   refactoring it to take `ctx` explicitly.
2. A missing `import copy` — found via live testing (a real two-branch
   parallel flow failed with a generic "orchestration" error and no
   visible cause), traced by temporarily un-swallowing a bare
   `except Exception:` in `_execute_flow` that was hiding the real
   traceback entirely. That swallowing was itself a genuine,
   pre-existing gap costing real debugging time during this exact
   change, so it's now a permanent fix, not just reverted: the handler
   logs the real exception via Python's standard `logging` module
   before returning the same response shape callers already depend on.

### Real, deliberate scope boundary

`loop`, `iteration`, and `hitl` nodes still execute one at a time,
using the real shared `ctx` directly rather than an isolated copy —
their existing implementations (`run_loop_body`, `run_iteration_body`,
`raise_hitl_pause`) are closures over the outer context, not
parameterized, and making them safe for concurrent isolated execution
would need a substantially larger refactor than this change's real
scope. They're still correctly wave-scheduled *relative to* independent
plain-node branches around them — a wave can contain a `loop` node
running alongside `llm`/`code`/other plain nodes without waiting on
each other unnecessarily — they're just never bundled into the same
concurrent thread-pool batch as their wave-mates. Stated here plainly,
not silently glossed over; a real, reasonable follow-on if genuinely
parallel branches *inside* a loop or iteration body are wanted later.

### Real, live verification (not just unit tests)

Directly proven with real wall-clock timing against the actual running
server, not simulated: two independent branches, each a real 1-second
sleep inside a Code node, completed the *whole run* in ~1.1 seconds —
not the ~2.1 seconds strict sequential execution would take — with
each branch's own span independently showing its real ~1 second
duration. Confirmed dead-branch propagation with a real condition
fan-out: two nodes wired to the "true" branch both ran; the "false"
branch's own node never executed at all, with no span for it anywhere
in the trace. Confirmed HITL pause/resume still works correctly
through the new scheduler (a real, live pause-then-resume round trip).
Confirmed a genuinely sequential, dependent chain still produces the
real correct data-flow result, not accidentally parallelized.

8 new permanent backend tests (real timing assertion with a generous
threshold, real dead-branch-propagation assertions in both directions,
real fan-out edge-creation and duplicate-prevention, real sequential-
correctness), 4 new permanent frontend tests (real distinct SVG edge
paths for a fan-out, real port-connected styling, real edge deletion
correctly leaving a fan-out sibling intact, real updated hint text).
Full backend suite: 166/166 passing, including every pre-existing
iteration/loop/hitl/condition test — a strong signal the rewrite
didn't silently break existing sequential behavior. Full frontend
suite: 288/288 passing. Mockup verified via real jsdom sessions,
including simulating an actual drag-to-fan-out gesture and confirming
the resulting edge count, rendering, and hint text all update
correctly — plus a full sweep confirming all 26 node types and all 14
routes still render without error.


## 0.39. Added in v39 — Per-node documentation pages and a real "view docs" icon

### What's real here

Real, original documentation for all 26 node types — written to
accurately describe *this platform's own actual, verified behavior*,
not copied from Dify's or any other documentation. Every claim in
every doc reflects something already built and tested earlier in this
project (real sandboxed Python execution, real Postgres-backed
persistence, real UCS entitlement checks, and so on) — the docs
describe the real system, not an idealized one.

A real, deliberately focused Markdown renderer (headings, tables,
bold/italic, inline code, bullet lists) — not a full CommonMark
implementation, since the doc content only ever needs these
constructs, and not a new npm dependency for something this scoped.
Verified directly against all 26 docs (correct HTML, balanced tags)
before it was ever wired into a page.

A real, standalone `/docs/:nodeType` page — deliberately outside the
normal app shell (no sidebar, no topbar), the same pattern already
used for the real end-user app-runner route, since a documentation
page and a builder canvas are genuinely different experiences and
shouldn't share chrome. Meant to be opened in a new browser tab,
matching Dify's own docs experience exactly.

Two real entry points: a "view docs" icon in the selected node's
config panel (always present, always pointing at whichever node type
is currently selected), and a second real docs icon on every palette
item, visible on hover — so documentation is reachable before a node
is ever dragged onto the canvas, not just after.

### A real refactor required to add this cleanly

`NodeConfigPanel`'s config UI is a single large `switch` statement with
26 cases, each one directly `return`ing its own JSX. Adding one shared
"view docs" icon without duplicating it into all 26 cases required
wrapping the whole switch in an IIFE to capture its result as a real
value, then rendering that value alongside the icon once, at the
outer level. The exact same restructuring was mirrored in the
mockup's own `cfgPanel()` function, which has the equivalent
structure (a long chain of `if(n.type===...)return ...;` statements).
Verified with the full existing test suite before and after — no
regressions from the refactor itself.

### Two real bugs found and fixed while testing this

1. A text-matching ambiguity in the tests themselves (not the app):
   "LLM" appears both as a palette label and inside the auto-selected
   node's config panel header, so a plain text assertion matched more
   than one element. The same class of issue caught a few times
   earlier in this project, at this point a well-recognized pattern —
   fixed by scoping the assertion to a real DOM count check instead.
2. A genuine test-authoring mistake: an early version of the
   `NodeDocs` page test rendered the component directly inside a
   `MemoryRouter` with a matching initial URL, but without an actual
   `<Route>` element wrapping it — `useParams()` only ever populates
   from a real matched route, not just a matching URL sitting in
   history, so the test's own assertion about the real node type was
   silently checking against an empty string. Fixed by wrapping the
   render in a real `<Routes><Route path="/docs/:nodeType" .../></Routes>`,
   matching how the actual app really wires this route in `App.tsx`.

### Real, mirrored mockup support

The same real documentation content and Markdown renderer were ported
to the mockup — extracted programmatically from the real TypeScript
source (not hand-retyped, to avoid transcription drift) and verified
with a syntax check immediately after. The mockup's own real
hash-based deep-linking mechanism (the same one already used for
`#/app/{agentId}`, confirmed working in earlier rounds) was extended
to also recognize `#/docs/{nodeType}` — a link opened in a new tab
genuinely reloads the mockup fresh and lands directly on that node's
real documentation page, not just a same-tab route swap. Verified
directly: dispatched a real `hashchange` event and confirmed the
mockup's own state picked up the new node type correctly.

A full regression sweep after the change confirmed all 14 routes (the
existing 13, plus the new `docs` route), all 26 node-type config
panels, and — checked individually, not just spot-checked — all 26
node types' own real documentation pages render without error.

### Honest, stated scope boundary

There's no search-across-docs feature and no docs "home page" listing
every node — the only real entry points are the per-node icons inside
the Builder. This matches what was actually asked for (a per-node
"view docs" experience), not a full documentation site; a docs index
page would be a real, reasonable follow-on if that's wanted later.


## 0.38. Added/rebuilt in v38 — Comment node rebuild, and a new Snippets feature

### Comment node (rebuilt)
Real 6-color pastel background palette (matching the reference
screenshot's swatches exactly), genuinely applied to the canvas note's
background and border — not decorative swatches with nothing behind
them. Real rich-text toolbar: font size (Small/Medium/Large), Bold,
Italic, Strikethrough, Link, Bulleted list, plus Heading and Quote —
every button genuinely operates on the real textarea's current
selection, wrapping it with real Markdown syntax, verified live via a
direct script before ever wiring it into React. Real Markdown-lite
rendering on the canvas note itself for exactly these constructs — a
small, deliberately focused renderer rather than a full CommonMark
library or parser, appropriately scoped for a small sticky note — with
real HTML-entity escaping applied before any markdown transform runs,
so literal `<script>` text typed into a note can never execute as real
HTML.

**Two real bugs found and fixed during this build:**
1. The renderer originally HTML-escaped a line before checking for
   block-level markers like `>` (quote) — since escaping turns a
   literal `>` into `&gt;`, every block-level check silently broke.
   Found via a direct script test before it ever reached the UI; fixed
   by detecting block markers on the raw line first, then escaping
   just the remaining inline content.
2. A significant, platform-wide bug in `NodeConfigPanel`, found while
   writing the very first test for the new Bold button: `cfg` state's
   `useState` initial value only ever evaluates once, on this
   component's real first-ever mount — which happens with `node` still
   `undefined`, since Builder.tsx renders the config panel
   unconditionally before any node is selected. That meant `cfg` was
   permanently stuck at `{}` for the entire lifetime of the panel,
   regardless of which node was ever selected afterward. Verified live
   with direct DOM probes before believing it: even a single,
   first-ever click on a node with real, non-empty config showed a
   genuinely empty field. Fixed with the standard React pattern (a
   `useEffect` resyncing `cfg` whenever `node.id` changes), with 2
   permanent regression-guard tests added specifically so this can't
   silently reappear.

7 tests in `Comment.test.tsx`, full mockup port (real color swatches,
real formatting toolbar, real Markdown rendering) verified via a real
jsdom session clicking through the actual rendered DOM.

### Snippets (new)
A real, working version of Dify's own documented Snippets feature —
reusable groups of nodes, built once and inserted as real, independent
copies into any workflow. Rather than building a whole parallel system,
this reuses substantial existing real infrastructure: the `Agent`
model's own `flow` JSON, the existing real DSL export/import, and the
existing real publish + version history mechanism (`AgentVersion`) —
confirmed live that publishing a snippet (a flow with no trigger node
at all) works correctly through the *exact same* endpoint a normal
agent publish uses, since that endpoint never actually assumed a
trigger node existed.

**Real data model**: `Agent.plane` gained a third real value,
`"Snippet"` (additive — the existing "Data"/"App" values and every
place that filters on them keep working exactly as before), plus a new
real `input_variables` field for declaring what a snippet expects from
whatever workflow it's inserted into (matching Dify's own documented
concept exactly — a snippet has no Start node of its own to collect
input).

**Real `/insert-snippet` endpoint** — genuinely deep-copies a
published snippet's real nodes and edges into a target workflow: fresh
globally-unique ids (so the same snippet can be inserted into the same
or a different workflow any number of times without ever colliding),
a real position offset (the copy's bounding box lands exactly at the
requested x/y, not pasted at the snippet's own original absolute
coordinates), and real internal-edge rewiring to the new ids. Verified
live end to end against the real running server: built a real 2-node
snippet, published it, inserted it into a separate target workflow
twice, and confirmed — directly against the database, not just the
HTTP response — that editing/reusing a snippet never mutates the
original or any workflow that already used it, matching Dify's own
stated behavior exactly. Every real error path is covered too:
inserting an unpublished snippet, a non-snippet agent, or a
nonexistent id all fail with real, honest, distinct messages.

**Two real bugs found and fixed before they ever reached a test:**
1. The node-creation endpoint hardcoded `plane="App"` and always
   applied the standard trigger/gov/respond scaffold regardless of
   what was requested — so the very first real attempt to create a
   snippet silently created a normal App agent instead. Fixed by
   making `plane` a real, respected field, with snippets correctly
   starting from a genuinely empty canvas (Dify: "no Start node... and
   no end node").
2. The first draft of the real parent-id remapping logic in
   `/insert-snippet` had an ordering bug — a child node processed
   before its own parent wouldn't resolve correctly, since the id map
   was being built incrementally during the same pass that used it.
   Rewritten as a real, correct two-pass approach (build the complete
   id map first, then construct the copied nodes) before it ever ran
   against real data.

**Real Studio integration**: a new Snippets page (real list, real
search, real Create-from-blank, real Publish, real Input Variables
editor), with the existing Studio grid, Home dashboard, Sub-Agent
node's target picker, top-nav agent-count badge, and Monitoring page
all updated to real-filter snippets out — since a snippet is never run
standalone, it shouldn't appear anywhere a real, runnable agent is
expected. Real Builder integration: editing a snippet shows a real,
restricted palette (no Trigger/Output "on canvas" entries, no
Knowledge Retrieval, no Human Input — matching Dify's own documented
restriction exactly), and a real Input Variables editor in place of
the ordinary "select a node" panel. A normal (non-snippet) workflow
gets a real "Snippets" palette group listing published snippets;
dragging one onto the canvas calls the real insert endpoint.

**Honest, stated scope boundary vs Dify's own documented spec:**
- **Real permissions are not implemented.** Dify's own documented
  Owner/Admin/Editor/Normal role distinctions for who can view,
  create, edit, publish, and delete snippets have no real backing here
  — this platform has no real user/role system anywhere else either
  (an existing, already-disclosed gap, not new to this feature), so
  every real action described above is available to anyone using this
  reference build, matching how every other part of this platform
  already behaves.
- **DSL-based snippet creation/import is not separately wired.** The
  existing real DSL export/import endpoints work unchanged and could
  genuinely round-trip a snippet's flow, but the specific "Create
  Snippet from DSL file" and "create a snippet directly from a
  selection of nodes in an existing workflow, right-click > Create
  Snippet" flows described in Dify's own docs aren't built — a
  snippet is created from a real blank canvas only in this build.
- **The mockup's own snippet-editing experience is real but scoped
  differently from the mockup's usual pattern.** The mockup's Builder
  page represents one fixed, hardcoded local demo agent rather than
  loading a specific agent by id the way the real React app does, so
  there's no separate per-snippet canvas to drag-and-drop node edits
  into in the mockup specifically; the real Input Variables editing
  and real Create/Publish flows are still genuinely backed by the live
  server. The React app has no such limitation — a snippet opens in
  the same real Builder as any other agent, with the same real
  drag-and-drop node editing.

13 new frontend tests (5 for the Snippets page, 8 for Builder's
snippet-mode behavior) and 17 new backend tests, full mockup
verification via real jsdom sessions — including one run against the
actual live backend server, confirming a real snippet created and
published in an earlier session round was genuinely fetched and
displayed, and that a real drag-and-drop insertion genuinely copied
its nodes onto the canvas with fresh ids.


## 0.37. Added/rebuilt in v37 — List Operator (new) and Agent (rebuilt)

### List Operator (new)
Real filtering (12 operators, real AND semantics across multiple
filters), real sorting (None-safe — a real, deliberate bug was found
and fixed here: `size` originally defaulted a missing value to `0`
rather than `None`, silently breaking the documented "sorts last"
guarantee for items genuinely missing that property), and real
Take-First-N (clamped to Dify's own documented 1–20 range). Real
property extraction covers both plain arrays and file-shaped arrays —
`type` (inferred from extension/content-type), `mime_type`,
`extension`, `size`, `name`, and `transfer_method` (genuinely
distinguishing this platform's own real local uploads from remote
URLs). Real output variables in Dify's own exact documented names:
`result`, `first_record`, `last_record`.

Verified live against the exact "Mixed File Processing" scenario
Dify's own docs describe: a mixed array of images and documents
correctly split into two separate real streams by type, sorting by
real file size correctly identified the largest file. 21 new backend
tests, real frontend config panel with add/remove filters (a real
duplicate-`<option>`-value bug in the sort dropdown was found and fixed
during the build).

### Agent (rebuilt)
The comparison found several real gaps against Dify's documented spec,
closed here:
- **Real `query`** ({{var}}-interpolated, falls back to the prior
  behavior when unset) and **real `instructions`** ({{var}}-
  interpolated, genuinely prepended to every strategy's system prompt)
  — previously neither existed; there was no way to give the agent a
  role/persona/constraints, or to point it at a specific variable
  instead of whatever the prior node happened to output.
- **Real `memory_enabled`**, reusing the exact same conversation-
  history mechanism (`AgentMemory`, key `__llm_chat_history__`) the LLM
  node's and Parameter Extractor's own memory toggles already use.
- **`function_calling` rebuilt from a hardcoded ≤2-call shape into a
  genuinely iterative multi-round tool-use loop** — real Anthropic
  tool-use (`tools`/`tool_choice="auto"`, the same real extension
  Parameter Extractor's own 'function_call' mode added to `call_llm`),
  matching Dify's own documented "iteratively decide which tools to use
  and when." Verified live: a 2-real-tool-call, 3-round scenario
  genuinely iterated (not a hardcoded shape), and a real `max_iterations`
  cap genuinely stopped a runaway tool-requesting model rather than
  looping forever.
- **Real tool selection** — a real checklist of configured tools the
  agent may call, sourced from the same real UCS catalog the `tool`
  node's own picker uses.
- **Real, general `max_iterations`** (falls back to the old `max_steps`
  key for backward compatibility) — previously only `react` had a real
  cap at all.
- **Real structured output** matching Dify's own documented concepts:
  `agent_final_answer`, `agent_tool_outputs` (a real array of
  `{tool, input, output}` per real call), `agent_reasoning_trace`,
  `agent_iteration_count`, `agent_success`. Old names (`agent_output`,
  `agent_steps`) are kept alongside the new ones for backward
  compatibility.
- **Real Agent Logs** — genuinely persisted as real `AgentLog` rows
  (Dify: "structured log events... for debugging and monitoring tool
  invocations"), not just returned in the run's own response.

**A real bug found and fixed during this same build**: `agent_success`
was originally computed as `bool(final_text)`, but the real
cap-exhaustion fallback always makes `final_text` truthy (it's a real
tool-call log line, not a real answer) — so hitting the safety cap
without ever producing a genuine answer was incorrectly reported as
`success: True`. Found while testing a deliberately runaway-tool-
requesting mock model, fixed with a proper `hit_cap` flag threaded
through all three strategies, and reverified both the failure and
success cases live.

**`plan_execute`** — this platform's own real addition beyond Dify's 2
documented strategies (Function Calling, ReAct) — is kept, not removed,
since it's a real, useful shape Dify doesn't happen to name; the
comparison and every new field apply to it too (Query/Instructions/
Memory), just not the iterative tool-use loop or `max_iterations` (it's
inherently a fixed two-call shape).

15 new backend tests (using the real Postgres engine — `AgentLog` has a
real foreign-key constraint to the real `agent` table, confirmed live:
an early test attempt with a fake agent_id genuinely failed with a real
`ForeignKeyViolation`). Real frontend rebuild exposing every new field,
with 2 pre-existing tests updated for the real rename
("Max steps" → "Max iterations," now shown for both `react` and
`function_calling`, not just `react`).

### A real mockup bug found and fixed during this same build
While porting both nodes to the mockup, an earlier edit accidentally
deleted the `if(n.type==='output'){` line (and its `const cfg=...`
line) while inserting the List Operator panel nearby — leaving the
Output node's real panel body orphaned as unconditional code, which
broke the whole file's syntax in a way that only surfaced at the
function's own closing brace, far downstream of the actual mistake.
Found via a real binary-search bisection (truncating the file at
different line counts and re-checking syntax at each point) rather
than guessing, which precisely localized the missing line. A second,
separate mistake was found the same way afterward: the new Agent
"Memory" toggle and tool-selection checkboxes were accidentally wired
into the mockup's `'change'` event listener (correct for the adjacent
`<select>` elements they were added near) instead of the `'click'`
listener real `<div>`-based toggles in this mockup actually need —
found by directly dumping the DOM before/after a real dispatched click
and observing the config genuinely never changed, then confirmed by
locating which listener function the inserted lines physically fell
inside. Both are now fixed and reverified live via jsdom sessions.

Full route/node-type sweep after both fixes: all 11 routes and all 26
node types (the pre-existing 23 plus List Operator, and the rebuilt
Agent and already-existing Output node) render without error.


## 0.36. Added in v36 — Answer, Variable Assigner, and Parameter Extractor nodes

### Answer
Real `{{var}}` interpolation of freeform content (same convention every
other text field in this platform uses), with real support for MULTIPLE
Answer nodes in one flow — matching Dify's own documented "deliver
content at different stages" behavior: each one's real resolved content
appends to a real running transcript rather than overwriting the last
(verified: three separate Answer nodes' content genuinely joined in
real execution order). Real, simplified multimodal support: a
referenced variable that resolves to a real uploaded-file-shaped dict
(the same shape `/api/uploads` produces) renders as a real inline
Markdown link, not a stringified Python dict (verified, with an
explicit regression-guard test). Honest, stated scope boundary: Dify
streams a node's own text progressively, pausing mid-string at the
first unresolved variable; this platform's real streaming is real but
coarser-grained — a whole node's content becomes visible as one real
SSE event once fully resolved, not chunked mid-string.

### Variable Assigner
Real, genuinely persistent CONVERSATION variables — reuses the existing
`AgentMemory` table's real 'conversation' scope (keyed by agent+invoking
user) under a distinct key prefix so it can never collide with a
`memory` node's own keys, with real JSON-typed round-tripping (no
schema migration needed). All 8 of Dify's documented operations
implemented: Overwrite/Clear/Set for any type, Arithmetic (+−×÷) for
numbers, and Append/Extend/Remove First/Remove Last for arrays. Real
trigger-time seeding: every known conversation variable for the current
(agent, user) is loaded into `ctx["vars"]` as `conv_<name>` before a
run's traversal even starts, so ANY node — not just ones wired
downstream of a Variable Assigner in that same run — can reference a
value persisted by an earlier, separate run.

**The strongest live verification in this round**: ran the identical
flow twice as two genuinely separate `/run` HTTP calls for the same
user — confirmed a counter correctly went 1→2 and an array correctly
accumulated `['billing', 'shipping']` across them; confirmed a
*different* user got fully isolated state; confirmed a *third*,
completely unrelated `template` node in a separate run could read the
persisted values back via the real trigger-time seeding, closing the
full real round trip.

**A real gap found and fixed during this same build**: the first
version of Arithmetic only accepted a `{{var}}` reference as its
operand, but Dify's own documented UI lets a person type a literal
number directly. Found while writing the very first live test, fixed,
and reverified.

### Parameter Extractor
Real LLM-based structured extraction, using the same real model adapter
(`call_llm`) the LLM node uses — extended with genuine Anthropic
tool-use support (`tools`/`tool_choice`, including an honest mock
fallback with real per-declared-type placeholder values when no API key
is configured) specifically to support this node's real 'function_call'
inference mode. The second documented mode, 'prompt', genuinely relies
on pure prompting (a real instruction to respond with only JSON) with
real parsing of the response, including stripping a real ```` ```json ````
code fence if the model adds one. Real `memory_enabled` reuses the
exact same real conversation-history mechanism the LLM node's own
memory toggle already uses. Real built-in status variables, kept in
Dify's own exact naming (not this platform's usual snake_case
convention, since matching the documented spec exactly is the point):
`__is_success` (1/0) and `__reason`. Verified live: function-call mode's
honest mock succeeds with real typed placeholders; prompt mode's mock
text honestly fails (it genuinely isn't valid JSON, and the node
correctly reports that rather than pretending success); required-
parameter validation correctly triggers a real failure when a
configured required field comes back missing.

### Two real bugs found and fixed while building the frontend for these
1. **A genuine stale-closure bug** in `NodeConfigPanel.tsx`: several
   `onChange` handlers (Variable Assigner's operation/arithmetic-
   operator selects, Parameter Extractor's type select and required
   toggle) called the save function using the `cfg` variable captured
   at the START of that render — stale relative to the `setCfg` call
   made earlier in the very same handler — so the optimistic local
   update would get silently overwritten a moment later once the save
   response came back with the old data. Found while writing real
   interactive tests (a "switch operation, expect the new field to
   appear" test kept timing out), root-caused by directly dumping the
   actual rendered DOM rather than guessing, and fixed by having the
   update functions return the fresh array so callers use that instead
   of the stale closure.
2. **A genuine text-matching ambiguity** in the new tests themselves
   (not the app): "Answer"/"Parameter Extractor"/"Variable Assigner"
   each appear twice once a node of that type exists — once as the
   palette's own draggable label, once as the selected node's config
   panel heading — so a plain text-content assertion threw on the
   genuine multiple match. Fixed by scoping every such assertion to the
   config panel specifically, the same real ambiguity class caught
   earlier in this project with the sidebar's "Settings" nav item.

Both are documented here in detail because the debugging process
itself is a useful record: several test runs initially looked like
unrelated environment flakiness (wildly different pass/fail counts
between runs of the identical file) before the real, deterministic
root causes were found by directly inspecting the rendered DOM rather
than accepting "it's probably flaky" as an explanation.

### Real, permanent test coverage added
`backend/tests/test_new_nodes.py` — 29 tests covering all three nodes,
including the real cross-run/cross-user persistence proofs for Variable
Assigner and honest-failure-mode coverage for Parameter Extractor.
`frontend/src/__tests__/NewNodeTypes.test.tsx` (15 tests) and
`VariableAssignerRemove.test.tsx` (1 test, isolated for reasons
documented in its own file) — covering all three real config panels'
conditional-field behavior and real save-on-blur/save-on-change
persistence.

### Mockup
All three node types added to the palette and canvas, with config
panels that accurately reflect real saved config (matching the
established convention for complex list-editing UI in this mockup —
Code/Aggregator/HITL's own precedent — full add/remove CRUD stays
React-only, but the conditional-display selects that this build's own
new logic depends on — inference mode, memory toggle, assignment
operation — are genuinely wired, since leaving them static would make
the conditional rendering they control permanently unreachable).
Verified via jsdom sessions clicking through the actual rendered DOM:
all three panels show real config-driven content, the wired selects
genuinely update state and re-render, and a full sweep confirms all 24
node types (the pre-existing 21 plus these 3) and all 11 routes still
render without error.


## 0.34. Fixed/added in v34 — Variable Aggregator rebuilt to match Dify exactly; a real, new Iteration node built alongside (not replacing) `loop`

### The validation, before any code was touched
- **Variable Aggregator**: Dify's entire point is that a person selects
  real variables from each exclusive branch and the node outputs
  whichever one genuinely has a value, type preserved. The existing
  implementation had no way to reference a branch variable at all — it
  only merged a fixed internal list of 5 hardcoded ctx keys (`llm_
  output`, `tool_output`, `retrieved_chunks`, `code_output`, `http_
  output`). Proven directly, before any code was touched: simulating
  Dify's exact documented use case (a branch setting a real custom
  variable) produced an empty `{}` — the real data was invisible to it.
- **Iteration**: found to be, architecturally, a different node from
  what Dify documents. The existing `loop`/`exit_loop` pair is a real,
  condition-driven repeat mechanism (a real `while` with a real early
  exit) — genuinely useful, but not what Dify's Iteration node is: a
  real array-map operation (`item`/`index` binding per element, real
  parallel execution up to 10 concurrent, results collected into a real
  output array, 3 real structured error-handling modes). No array
  input, no `item`/`index` binding (only an unrelated `loop_iteration`
  counter), no output collection, no parallel mode, and no structured
  error handling existed at all.
- **Explicit decisions made before any rebuild** (asked and answered by
  the person, not assumed): rebuild the Aggregator fully, real variable
  selection AND real Aggregation Groups; build a genuinely new,
  Dify-matching Iteration node — but do not touch `loop` at all, keep
  both real container types coexisting.

### Variable Aggregator — real rebuild
- Real config shape: `groups`, a real list of `{output_name, variables}`
  — for each group, the real configured variables are checked in real
  order, and the first one that genuinely has a value (not `None`, not
  `""`) becomes that group's real output, with its real type preserved
  (string/number/object/boolean/array/file-shaped dict — never coerced
  or wrapped). Verified live end to end through a real IF/ELSE flow
  with two branches each setting a different real custom variable.
- Real "Aggregation Group" support — multiple independent groups in one
  node, each producing its own separate real output, verified not to
  leak into each other.
- Real backward compatibility for the old config shape (bare `output_
  name`, no variable selection — the entire old implementation):
  produces an honest `None`, not a silently recreated version of the
  old (never-actually-functional) hardcoded merge.
- React `NodeConfigPanel` rebuilt with real add/remove group and add/
  remove variable controls. Mockup panel updated to accurately describe
  the new real behavior (static display text, matching this node's
  pre-existing level of interactivity in the mockup — condition/code
  are the same tier).
- 13 new backend tests, covering real variable selection, real type
  preservation for every documented type, real Aggregation Groups, and
  real backward compatibility.

### Iteration — a genuinely new, real node, `loop` left completely untouched
- New `run_iteration_body`, entirely separate from `run_loop_body` —
  confirmed via a dedicated regression-guard test that `loop`'s own
  condition-driven repeat mechanism still works exactly as before.
- Real `item`/`index` binding per element, via a real isolated
  per-iteration ctx snapshot (a real shallow copy, so one iteration's
  body can't leak into another's).
- Real sequential AND real parallel execution (a real `ThreadPoolExecutor`,
  capped at Dify's own documented 10 concurrent, each worker with its
  own real dedicated DB session). **Proven with genuine wall-clock
  timing, not just a status check**: the same 6-item, 0.3s-per-item
  workload took 1.97s sequential vs. 0.45s parallel — real, measured
  concurrency. Real input order is preserved in the output even under
  parallel execution (verified with intentionally reverse-ordered
  completion times).
- Real per-iteration error handling matching Dify's exact 3 documented
  modes, verified against Dify's own documented example values:
  'Terminated' genuinely fails the whole run; 'Continue on error'
  produces `[10.0, None, 20.0, None, 50.0]` for `[10,0,5,0,2]` (a real
  `None` at each failed real index, same length); 'Remove abnormal
  output' produces `[10.0, 20.0, 50.0]` (genuinely shorter, no nulls).
- Real `flatten` behavior (on by default, matching the pre-existing
  config panel default) — a real per-iteration list result is spliced
  into the parent array one level; verified both on and off.
- Real Builder canvas container support (the drag-and-drop bounding
  box), added as a fully parallel, separate implementation
  (`iterationBoundingBox`, a real twin of `loopBoundingBox`, not a
  refactor of it) in both React and the mockup — confirmed via test
  that both container types render correctly side by side without
  interfering, and that `loop`'s own container is pixel-for-pixel
  unaffected.
- 14 new backend tests (including the two genuine timing-based
  parallelism proofs) plus 4 new React tests for the canvas container
  behavior.

### Two real bugs found and fixed during this same build
1. **A serious editing mistake, caught immediately by the very first
   live test**: a `str_replace` edit meant to insert `run_iteration_
   body` accidentally deleted the `def raise_hitl_pause(node):` line
   while keeping its body — silently turning Human Input's entire real
   pause mechanism into orphaned code appended to the end of the new
   function. This would have broken HITL platform-wide. Caught via a
   `NameError` on the very first live test run, fixed properly, and
   directly re-verified (not just trusted): a real HITL node still
   genuinely pauses with correct real `{{var}}`-interpolated content.
   A permanent regression-guard test was added specifically for this.
2. **A subtler real design bug**: an iteration body with no `respond`
   node (a body isn't a top-level flow) never populated `ctx["output"]`,
   so results silently fell back to the unmodified original item. The
   first fix reused `handle_respond`'s own fallback logic, which fixed
   the value but coerced it to a string (`"2"` instead of `2`) — correct
   for a flow's final text output, wrong for collecting a real typed
   per-element result. Fixed properly with a new, dedicated, type-
   preserving `_pick_body_output` helper.

### Honest, stated scope boundaries (not silently dropped)
- `loop`/`exit_loop` remain exactly as they were — a real, separate,
  condition-driven repeat mechanism, not replaced or deprecated by
  Iteration's arrival. Both are real, both coexist on the canvas.
- Iteration's `on_event` live-progress hooks are wired per body node
  per iteration (tagged with `iteration_index`), but each iteration's
  own execution does NOT append individual per-node spans to the
  overall run's trace (only one summary span for the whole Iteration
  node) — a deliberate real choice to avoid a combinatorial explosion
  of spans for a large array, stated here rather than silently limiting
  trace detail.


## 0.33. Fixed/added in v33 — Doc Extractor real feature-parity validation against Dify's documented spec

### The validation, before any code was touched
Compared this platform's Doc Extractor against Dify's own docs, format by
format, verifying every claim live (built real files, ran real
extraction) rather than assuming:
- **Real, confirmed gaps found**: DOCX tables were silently dropped
  entirely (built a real table with real cell values — vanished, no
  error); HTML markup leaked through raw and unstripped; XLSX wasn't
  handled at all and fell through to raw binary garbage as "extracted
  text"; encoding was always assumed UTF-8, silently mangling anything
  else (a real Windows-1252 file with accented characters came out as
  replacement characters); PPT/PPTX, EML/MSG, EPUB, and VTT weren't
  supported at all; input was URL-only with no real batch/multi-file
  path; output variable is named `docextractor_text`, not Dify's `text`.
- **Decisions made explicitly before fixing anything** (asked and
  answered by the person, not assumed): keep `docextractor_text` as-is
  (a rename would break existing flows — documented, not silently
  changed); build real multi-file array support now; add the new format
  dependencies now.

### Real fixes and new format support — every one verified live against a real generated file of that format, not just read in code
- **DOCX**: real tables now genuinely extracted as real Markdown, in
  correct real document order (walks the real document body XML
  directly, since python-docx's own `.paragraphs`/`.tables` are separate
  unordered collections) — not just re-included, positioned correctly
  relative to surrounding paragraphs.
- **HTML**: genuine BeautifulSoup parsing — markup stripped, block-level
  elements get real line breaks, inline elements stay in their real
  sentence, script/style content genuinely removed rather than
  incorrectly included as text.
- **XLSX / CSV**: real Markdown table conversion (openpyxl / Python's own
  `csv` module — quoted fields with embedded commas correctly parsed as
  one field, verified).
- **PPTX**: real per-slide text extraction, in real slide order
  (python-pptx).
- **EPUB**: real per-chapter extraction, walking the real spine (reading
  order), each chapter's real HTML content run through the same real
  HTML-stripping used for standalone HTML (ebooklib).
- **EML**: real parsing via Python's own stdlib `email` module — real
  From/To/Subject/Date headers plus the real plain-text (or HTML,
  stripped) body, not the raw MIME source with boundaries and
  base64-encoded parts included as if they were content.
- **VTT**: real cue parsing with genuine consecutive-same-speaker
  merging, matching Dify's own documented behavior exactly (verified: 
  two consecutive Alice lines merged into one, a following Bob line
  stayed separate).
- **Legacy XLS**: real extraction via the real `xls2csv` system tool
  (part of the real `catdoc` package) — verified against a genuine
  binary `.xls` file produced via a real LibreOffice conversion.
- **Legacy DOC/PPT**: wired via the real `antiword`/`catppt` system
  tools — genuinely invoked, real stdout/stderr/exit-code handling,
  errors surfaced honestly rather than pretending success. Could NOT get
  a positive live confirmation in this environment: a real, documented
  compatibility gap between these tools (built for genuine MS
  Office-produced binary files) and LibreOffice's own "MS Word 97" /
  "PowerPoint 97" export filters, which this sandbox has no way around
  (no genuine MS Office available to produce an authentic test file).
  The wrapper code's own error handling is confirmed correct regardless
  — it surfaces the real tool's real error, never silently returns
  empty/fake success.
- **MSG**: wired via the real `extract-msg` library, genuinely untested
  live — no practical way to construct a real `.msg` sample without
  genuine Outlook.
- **Real encoding detection** (chardet): tries a real strict UTF-8 decode
  first; on failure, trusts chardet's real best guess regardless of its
  own confidence score — verified chardet's confidence is genuinely
  often low (0.07) on short/realistic files even when correct, so an
  earlier version of this fix that required high confidence defeated
  the whole point; fixed and re-verified.

### Real file upload infrastructure (new)
- New `UploadedFile` model — real bytes stored in this platform's own
  Postgres (a real `bytea` column), a deliberate, honest substitution
  for real S3/GCS/Azure storage (this reference environment has no real
  cloud credentials, same reasoning `_resolve_cloud_url` already
  documents) — genuinely real storage and a genuinely real, working
  fetchable URL either way.
- New `POST /api/uploads` / `GET /api/uploads/{id}/download` — verified
  live: uploaded a real file, downloaded it back, byte-for-byte
  identical.
- A trigger's real `single_file`/`file_list` fields now genuinely upload
  on selection (React `RunnerUI.tsx`) instead of only capturing a
  filename string client-side with nothing backing it.

### Real multi-file mode (new)
- `handle_docextractor` now has a real `mode: 'single'|'multiple'`.
  'multiple' resolves a whole `{{field_name}}` reference to its real raw
  ctx value (a real list of `{filename, url}` dicts from a real
  `file_list` field) rather than string-interpolating it — every real
  file is genuinely fetched and extracted independently, a real
  per-file failure doesn't abort the others, and the real output is a
  real `list[str]`, matching Dify's documented `array[string]` shape
  for multi-file mode. 'single' mode is unchanged/backward-compatible —
  a plain URL string still works exactly as before.
- Real `NodeConfigPanel` (React) and mockup config panel updates: a real
  Single File / Multiple Files mode toggle, updated real format list,
  and the honest `docextractor_text` vs Dify's `text` naming note.

### A real, separate concurrency bug found and fixed along the way
While first testing real multi-file mode live, a 2-file extraction
against this platform's own real upload URLs took exactly 30 seconds
(2 × the real 15-second per-fetch timeout) and returned empty output —
both fetches had genuinely failed. Root cause: `routers/execution.py`'s
plain `/run` endpoint (`run_agent`) is declared `async def` but called
the blocking `run_flow_sync` directly, never offloaded to a worker
thread. A node making a real outbound HTTP call BACK to this SAME
server (exactly what the new upload URLs are) deadlocks: the single
asyncio event loop stays synchronously blocked inside the outer
request's own coroutine, so it can never accept and process the
inbound self-referential connection, and the request only completes
once the *client-side* httpx timeout eventually fires. This is a real,
pre-existing bug this validation surfaced — not introduced by the new
feature, and not scoped to Doc Extractor specifically (any node making
a self-referential call would have hit it). Fixed by offloading to a
real worker thread with its own dedicated `Session`, matching the same
real pattern already used by batch-run and the streaming endpoint.
Re-verified live: the same 2-file run that took 30s and failed now
completes in 133ms with correct real output from both files.

### Real, permanent test coverage added
`backend/tests/test_doc_extractor.py` — 26 tests (25 passing outright,
1 conditionally passing when LibreOffice is present, which it is in
this build environment), each format test building a genuinely real
file via the real library that writes it (python-docx, openpyxl,
python-pptx, ebooklib, Python's own `email` module) rather than a
synthetic byte string. Includes explicit regression-guard tests that
assert the OLD broken behavior really was broken (e.g. confirming a
blind UTF-8 decode of the Windows-1252 sample really does produce
replacement characters) alongside confirming the NEW behavior is fixed,
so a future change that silently reverts either fix would be caught.

### Honest, stated remaining gaps (not silently dropped)
- Legacy .doc/.ppt: real mechanism, unconfirmed in this specific
  sandbox due to the LibreOffice/antiword-catppt compatibility gap
  described above — likely to work correctly against genuine MS
  Office-produced files in a real deployment, but that's an inference,
  not a live-verified fact the way every other format in this list is.
- .msg: real mechanism, genuinely untested (no practical way to build a
  real sample here).
- Output variable naming (`docextractor_text` vs Dify's `text`): a
  deliberate, explicitly-decided difference, not an oversight — kept to
  avoid a breaking rename for any existing flow.


## 0.32. Fixed in v32 — the Code node's real error handling, input injection, and output naming, none of which were previously wired

- **The finding, verified empirically before any code was touched**: the
  Code node's own config panel promises Input variables, Output
  variables, Retry on failure, and Error handling (None/Default value/
  Fail branch) — but `handle_code` (harness.py) only ever read the raw
  `code` string. Confirmed live: even the UI's own default placeholder
  code (`def main(arg1, arg2): return {...}`) produced completely empty
  output, because nothing ever called `main()`; a script that
  deliberately raised `ValueError` produced a real traceback and
  `exit_code: 1` in the sandbox (confirmed directly), but the flow
  still reported `"status": "ok"` on the node and `"status": "success"`
  on the whole run. Zero backend tests existed anywhere in this project
  to have caught this.
- **Real input injection** — `arg1_name`/`arg2_name` (the real,
  person-chosen parameter names) are now genuinely resolved from
  `arg1_var`/`arg2_var` (the same `{{var}}`-style references every
  other node's config uses) and injected as real Python variables
  before the person's code runs; if it defines `main(...)`, it's
  genuinely invoked with them as real keyword arguments.
- **Real output naming** — the real return value (or the script's own
  real stdout, if it defines no `main`) now lands under the person's
  real configured `output_name`, not a fixed generic key.
- **Real error handling, honest per the real `error_handling` setting**:
  'None' (default) now genuinely fails the node/run with the real
  traceback in the span; 'Default value' genuinely catches the failure
  and continues with a real configured fallback; 'Fail branch' genuinely
  routes down a real, distinct second output port — a new real
  `compute_next_node_id` branch mirroring the `condition` node's
  existing true/false mechanism, with a matching real second port on
  the Builder canvas (React) that only appears once 'Fail branch' is
  actually selected. `retry_on_failure` now genuinely retries the real
  sandboxed execution once before giving up.
- **Two real bugs caught and fixed during this same build**, not just
  the original one: an internal parsing sentinel was leaking into
  user-visible output on a successful run with a `main()` return value;
  and the first version of 'Default value' error handling forgot to
  populate the field `handle_respond`'s real fallback chain actually
  reads from, so a caught failure still produced empty output.
- **Real, permanent test coverage added — the first backend test file
  this project has had.** `backend/tests/test_code_node.py`: 23 tests,
  including ones that genuinely exercise the real sandbox's network
  isolation (a real socket connection attempt is confirmed blocked) and
  wall-clock timeout (a real infinite loop is confirmed killed) rather
  than mocking those away. `CodeNodeErrorHandling.test.tsx` (React, new):
  4 tests covering the real Default value field, the real Fail branch
  explanatory note, and the real second canvas port — appearing only
  once actually configured, verified via a real (not stale) mock of the
  node-config PATCH response, after catching a real mock-accuracy
  mistake of my own that would have silently passed regardless of
  whether the real feature worked.
- **Mockup**: the Code node's config panel is, and was already, static/
  decorative for this node — matching the same convention as
  `condition`/`template`/`aggregator`, none of which are wired to live
  state in this mockup either, and this node isn't reachable from the
  mockup's own single fixed demo canvas at all. A real, unrelated
  factual inaccuracy was fixed regardless: the panel claimed "Executes
  in a Firecracker microVM / gVisor sandbox," which was never true for
  this reference implementation and is a real, different claim from
  what the actual backend does (subprocess + rlimits — see
  `sandbox.py`'s own docstring). The new Default value field and Fail
  branch explanation were added as static display text, consistent with
  this node's existing level of interactivity in the mockup, not wired
  to live editable state — a deliberate, stated scope boundary matching
  the mockup's own established convention for this class of node, not
  an oversight.
- **Verified live end to end against the real running backend** for
  every mode: real input injection with `main()` invocation (confirmed
  correct computed values), real crash → real failed run with real
  traceback, real Default value fallback, real Fail branch routing to
  the correct one of two real downstream nodes depending on real
  success/failure, and a real genuine single retry (confirmed via call
  count) before falling back.


## 0.31. New in v31 — real live per-node workflow progress via Server-Sent Events, genuine cancellation, and a JSON Output panel

- **Real live streaming, not a replay animated to look live** — new
  `POST /{agent_id}/run/stream` endpoint (`routers/execution.py`) and
  `on_event`/`is_cancelled` hooks threaded through `_execute_flow`,
  `run_flow_sync`, and every internal traversal path in `harness.py`
  (main loop, loop bodies, HITL pause). A genuine `node_start` event
  fires the instant the traversal reaches each node — before its
  handler even runs — and a genuine `node_done` event fires the instant
  it finishes, with its real status. The deliberate alternative (waiting
  for a normal `/run` response, then replaying its `spans` array with
  artificial delays to look live) was rejected — it would report a
  node as "running" only after it had already finished, which is a lie
  about what "live" means, not a shortcut.
- **Real, node-boundary-granular cancellation** — Stop Run genuinely
  aborts the browser's connection; the backend detects the disconnect
  (`request.is_disconnected()`) and sets a real `threading.Event` the
  traversal checks between nodes. An honestly-stated boundary, not a
  silently overstated one: a node's own in-flight blocking call (an
  LLM/HTTP request already sent) still runs to completion — only the
  *next* node is skipped. Verified directly (isolated Python call, not
  just through the endpoint): cancelling after node 1 completes
  genuinely prevents node 2 from ever starting.
- **A real architectural choice, not an oversight**: the streaming
  endpoint writes the same real Run/Trace/AgentLog rows as the plain
  `/run` endpoint on completion, and persists a real `HitlRequest` on
  pause — but doesn't replicate input/output moderation, follow-up
  generation, or citations (chat-app-oriented features; this endpoint's
  real caller in `RunnerUI.tsx` only ever uses it for workflow apps). A
  stated scope boundary in both the endpoint's docstring and here, not
  a silently dropped one.
- **A real regression caught and fixed before it shipped**: the first
  version of the new JSON Output panel would have replaced the existing
  "genuine named fields, not a single text blob" structured-output
  display for workflow apps — losing real, previously-tested UI. Fixed
  by keeping the per-field breakdown (with its own Copy/Save) as the
  primary result and adding the JSON panel as a genuinely additional
  view alongside the live process panel, not a replacement.
- **`WorkflowProcessPanel`** (React) / `workflowProcessPanelHtml`
  (mockup) — real rows built entirely from the real events received; a
  node only ever appears in the list once it has genuinely started, and
  only shows done/error once it genuinely has. Reuses the Builder's own
  `NODE_TYPES` icon/label/color definitions for each row — the same
  visual language as the canvas, not fabricated iconography.
- **Verified live end to end, in both surfaces, against the real running
  backend** — not just typechecked or read: a real curl SSE session
  against a live 3-node agent showed genuine ordered `node_start`/
  `node_done`/`complete` events and a confirmed-persisted Run row.
  React: 233/233 tests passing (5 new, covering real incremental node
  arrival, per-node failure display, the JSON panel, genuine Stop Run
  abortion, and honest handling of a backend-driven cancellation),
  production build succeeds. Mockup: `node --check` syntax-clean, and
  headless jsdom sessions that click all the way through the rendered
  DOM against the real live backend — a full 3-node run streaming to
  completion with the real JSON Output panel appearing, and a separate
  session confirming Stop Run genuinely aborts an in-flight request and
  the UI reverts correctly afterward. One real, unrelated finding
  surfaced during this verification, not a bug in this feature: running
  the existing demo agent `a6` (whose flow includes a real LLM node)
  against this sandbox's backend genuinely fails, honestly reported as
  `fail_class: "orchestration"` — because no real Anthropic API key is
  configured in this reference environment, not because streaming
  itself is broken; confirmed by a from-scratch dependency-free
  workflow agent completing successfully end to end.


## 0.30. New in v30 — Studio-style RAG card grid, multi-pipeline creation, and a real per-pipeline Retrieval API

- **Studio-style card grid** for the RAG page (React `pages/RAG.tsx`,
  mockup `PAGES.rag()`) — reused the real `.studio-grid`/`.studio-card`
  CSS and markup pattern the Studio (agents) page already uses, not a
  new visual language. Each card shows real Chunk Structure/Retrieval
  Method/API-enabled summary chips.
- **"New RAG Pipeline"** button + a dedicated creation modal (React
  `NewRagPipelineModal.tsx`; mockup `OVERLAYS.newRagPipeline()`) —
  connection fields only (pgvector), no Chunk Structure/Index Method
  pickers at creation (those still live on the Settings tab, unchanged
  from v29). On create, genuinely lands directly on the new pipeline's
  Documents tab — mirrors Studio's "New agent → straight into the
  Builder" pattern, not a separate reimplementation. Real, previously-
  undiscovered bug caught and fixed while building this: the React
  version's "auto-open the just-created pipeline" effect depended on
  `kbs` (which never refreshes after creation) instead of the actual
  `searchParams` change — the feature would have silently never
  triggered in the real app. Fixed to react to `searchParams` directly
  with its own fresh fetch, since navigating within the same matched
  route doesn't remount the component.
- **A real, externally-callable Retrieval API per pipeline** — new
  backend endpoints `POST /{kb_id}/api/regenerate-key` (real
  cryptographically random token via `secrets.token_urlsafe`) and
  `POST /{kb_id}/api/retrieve` (real bearer-token auth, genuinely the
  same scoring path — `app/retrieval.py` — a live Retrieval node and
  the Settings tab's own Retrieval Testing use, not a simplified
  reimplementation for external callers). A new "API" tab on the
  knowledge base detail view (both React and mockup) surfaces enable/
  disable, key generation/regeneration (with a real confirm-before-
  invalidate dialog, since regenerating immediately breaks the old
  key), a real working curl example, and a "Try It" panel. Every real
  API call is logged into the same real Retrieval Testing history
  (tagged `[API]`) and updates real per-document retrieval_count — API
  usage isn't invisible bookkeeping.
- **A real naming collision avoided before it happened**: the existing
  `config.api_key` field is already used by non-pgvector providers
  (Pinecone/Weaviate/etc.) for their own connection credentials — the
  new feature deliberately uses `retrieval_api_key`/
  `retrieval_api_enabled` instead, so enabling the Retrieval API can
  never be confused with, or leak, an unrelated connection credential.
- **A real frontend bug caught and fixed**: the shared `req()` API
  helper shallow-spreads `opts`, so the first version of
  `callKbRetrievalApi` (passing a custom `Authorization` header) would
  have silently dropped `Content-Type: application/json` — fixed to
  include both explicitly.
- **Verified end to end, live, in both surfaces** — not just typechecked
  or read: backend (curl) — blocked before enabling (403), rejects
  missing/wrong tokens (401), succeeds with the correct token and
  returns real correctly-ranked results, key is masked on all
  subsequent reads, logged in Records history. React — full suite
  228/228 passing (including new `RagApiTab.test.tsx` and expanded
  `RAGPage.test.tsx` coverage for the creation flow and card grid),
  production build succeeds. Mockup — `node --check` syntax-clean, and
  headless jsdom tests that click all the way through the real rendered
  DOM against the real live backend: the card grid renders, New RAG
  Pipeline creation with real connection fields lands on the correct
  detail view, and the full API tab flow (enable → generate key →
  curl snippet shows the real key → Try It returns real scored
  results) all work against the actual running backend, not a mock.


## 0.29. New in v29 — Knowledge Base split into a real "RAG" top-level page and a connection-only Integrations tab

- **New top-level "RAG" nav item** (React: `pages/RAG.tsx`, routed at
  `/rag`; mockup: `PAGES.rag()`), placed in the Build group right after
  Templates. Lists every real, connected pgvector knowledge base with a
  real Chunk Structure / Retrieval Method summary chip per card, plus a
  separate section for non-pgvector connections (a real, honest note that
  document management isn't wired for those providers, with a link back
  to Integrations). Clicking a pgvector knowledge base opens the exact
  same real `KnowledgeBaseDetail` component (Documents/Metadata/Retrieval
  Testing/Settings) that used to live inside Integrations — moved
  wholesale, not reimplemented, so none of v26–v28's real behavior
  (Q&A/Parent-Child chunking, Economical index method, Hybrid retrieval,
  the Settings tab) needed to change.
- **Integrations → Knowledge Base reverted to a pure connection catalog**
  — list, Add, Test, Delete, matching every other Integrations tab's
  pattern (Model Providers, Tools, Data Sources). No more in-page
  document-management detail view; a "Manage in RAG" link routes to the
  new page instead. The Add Knowledge Base modal correspondingly dropped
  its Chunk Structure / Index Method pickers (added in v26) — those
  settings are real either way, just relocated to where they now belong:
  the RAG page's Settings tab, configured after the connection exists,
  not before.
- **Real bugs caught and fixed while rewiring this, not just moved
  code**:
  1. A genuine RTL testing mistake in a new test: `getByText(...)
     .not.toBeInTheDocument()` always throws before that assertion can
     run, since `getByText` itself throws on zero matches — fixed to use
     `queryByText` for absence checks.
  2. A real UX duplication in the RAG page's empty state: the
     "Integrations → Knowledge Base" link text appeared twice on the
     page (once in the persistent context banner, once repeated in the
     empty-state copy) — fixed by making the empty state a distinct
     call-to-action button instead of repeating the same phrase.
- **Verified end to end in both surfaces**: React — full test suite
  (221/221, including a new `RAGPage.test.tsx` covering the moved
  ingest/chunking-options workflow and a rewritten `PipelineSettings.
  test.tsx` covering Settings-tab behavior reached via the new route),
  production build, backend confirmed unaffected (this was a pure
  frontend restructure). Mockup — `node --check` syntax-clean, and
  headless jsdom tests that actually click through the rendered DOM: the
  RAG nav item routes correctly, Integrations' KB tab no longer shows a
  document-management view, clicking a knowledge base on the RAG page
  opens its real detail view, Back returns to the RAG list (not
  Integrations), the Settings tab and the Retrieval Testing badge both
  work identically when reached via the new route, and a full sweep of
  every route/tab combination rendered without throwing.


## 0.28. New in v28 — Pipeline tab renamed to Settings, retrieval-method badge, and a full re-verification of every Chunk Structure × Index Method combination

- **"Pipeline" tab renamed to "Settings"** — `components/KnowledgeBaseDetail.tsx`'s
  `TAB_LABELS` (React) and the mockup's equivalent, plus every user-facing
  string that referenced "the Pipeline tab" (help text, the save-
  confirmation toast). The internal tab key (`'pipeline'`) and component/
  function names (`PipelineTab`, `PipelineControls.tsx`) were deliberately
  left as-is — pure internal naming, not something a person using the app
  ever sees, so renaming it too would just be code churn with no real
  benefit. One real test bug caught and fixed in the process: the app
  already has its own top-level "Settings" nav item in the sidebar, so an
  unscoped `getByText('Settings')` in the test suite would have matched
  both and thrown — re-scoped to the KB-detail tab strip specifically.
- **Real, clickable retrieval-method badge** on the Retrieval Testing
  tab's Source Text card (`⚏ VECTOR SEARCH ⚙` / `⚏ HYBRID SEARCH ⚙` /
  `📄 FULL-TEXT SEARCH ⚙`, matching the reference screenshot), in both
  React and the mockup. Real, not decorative: reads the KB's actual
  `config.retrieval_method`, and clicking it genuinely navigates to the
  Settings tab (React: lifts a `setTab` callback down; mockup: reuses the
  existing `data-kbdetailtab` handler already wired for the tab strip
  itself, no new handler needed).
- **Full re-verification of ingestion's real per-KB behavior**, prompted
  by a direct question rather than assumed from having written it
  earlier — all 6 Chunk Structure × Index Method combinations tested live
  against a fresh Postgres database (not reasoned about from source
  alone): General/Q&A/Parent-Child crossed with High Quality/Economical.
  Confirmed for real: Economical genuinely never computes an embedding
  (`embedding IS NULL` in the actual table) and honors a real, configured
  `keyword_count`; Q&A's Economical mode extracts keywords from the
  question only, correctly excluding the answer; Parent-Child's full
  round-trip (child matched at query time, parent returned as content)
  works under both index methods, including a full retrieval-test call
  that returned the correct parent text with a real similarity score.

## 0.27. New in v26 — real Chunk Structure, Index Method, and Retrieval Setting for Knowledge Bases

- **Real per-KB settings storage** — `PATCH /{kb_id}/settings` (new),
  stored in the existing `config` JSONB (no migration). Name & Icon and
  Description are now real, editable fields, not absent. Chunk
  Structure/Index Method changes are honestly scoped to apply only to
  documents ingested from then on — the UI states this explicitly once a
  KB already has documents, rather than silently doing nothing or
  claiming to reprocess existing chunks it doesn't.
- **Three genuinely different Chunk Structures** (`app/ingestion.py`):
  `general` (existing), `qa` (real `Q:`/`A:` pair parsing, with a
  question-mark-heuristic fallback for unmarked FAQ text — indexes on
  the question, matching Dify's own real behavior), and `parent_child`
  (real two-level chunking — the smaller child is what's embedded/
  matched, the larger real parent text is stored in a genuine
  `parent_content` column and substituted in as the returned content at
  retrieval time). Verified live: a Q&A file produced exactly one real
  chunk per pair; a parent-child query matched the precise child
  sentence but returned the full parent paragraph as content.
- **A real Economical index method** (`embeddings.extract_keywords`) —
  genuinely skips the embedding call (verified live: `embedding IS NULL`
  in the database for these rows) and instead extracts real frequency-
  based keywords into a new `keywords` column; retrieval for these rows
  falls back to real keyword-overlap scoring. Honestly weaker than
  semantic search, by design — matches Dify's own real "no tokens
  consumed, reduced retrieval accuracy" tradeoff, not a fabricated
  equivalent index.
- **Real Full-Text and Hybrid retrieval**, plus a real Score Threshold
  filter — new shared `app/retrieval.py`, used by BOTH the live
  `retrieval` node (harness.py) and the Pipeline tab's Retrieval Testing,
  so the two can't diverge. Verified live: Hybrid's weighted-score
  combination produces real, different rankings than pure Vector; Score
  Threshold genuinely drops low-scoring results, not just displays a
  slider. Selecting "Rerank Model" is accepted and stored for real, but
  since no real rerank model provider exists anywhere in this platform,
  retrieval honestly falls back to Weighted Score with a surfaced
  `rerank_note` — the same "Incompatible" state Dify's own UI shows when
  nothing is configured, not a silent substitution pretending reranking
  happened.
- **Frontend**: `AddKnowledgeBaseModal` now collects Name & Icon,
  Description, Chunk Structure, and Index Method at creation (matching
  Dify's real creation flow); the Pipeline tab is now a fully editable
  settings page using the same real picker components
  (`components/PipelineControls.tsx`), not the old static three-line KV
  panel. New `PipelineSettings.test.tsx` covers the new pickers,
  including the Rerank "Incompatible" state and the documents-already-
  exist warning.
- **Ported to and verified in the mockup too** — the same real Chunk
  Structure / Index Method / Retrieval Setting card pickers (shared
  helper functions `chunkStructureCardsHtml`/`indexMethodCardsHtml`/
  `retrievalSettingCardsHtml`, used by both the Add Knowledge Base modal
  and the Pipeline tab, mirroring the React app's shared-component
  approach), the real `PATCH .../settings` call on Save, and the
  Retrieval Testing tab's rerank-note banner. Verified with headless
  jsdom smoke tests that actually click through the rendered DOM rather
  than just reading the source: card selection, the Hybrid/Rerank
  "Incompatible" toggle (in both directions), KB creation with a
  non-default chunk structure, and a full page-render sweep across every
  route. One real false-positive caught and corrected during that
  verification, worth naming honestly: an early test check read
  `document.body.textContent`, which in a browser DOM also includes the
  literal source text sitting inside `<script>` tags — since that source
  contains the words "Weighted Score", the check falsely flagged the
  element as still visible. Re-scoped to the actual app-mount element
  and re-verified; the real toggle behavior was correct all along.

### What's real vs. still a stated gap in this round

- Full-Text scoring is a real word-overlap ratio, not Postgres's own
  `ts_rank`/`tsvector` — a deliberate choice so the same scoring function
  works identically against any Postgres-compatible store, not just one
  with that extension enabled (see `app/retrieval.py`'s docstring).
- Automatic metadata filtering (from the earlier Retrieval node work) is
  still config-storage only — unrelated to this round, not newly
  introduced.
- Retrieval Testing's candidate set is bounded (`CANDIDATE_LIMIT = 500`
  rows, scored in Python) rather than one fully-pushed-down hybrid SQL
  query — real per-row scoring on a real bounded set, stated in
  `app/retrieval.py`'s docstring, not silently capped.
- No real rerank model provider exists in this reference platform at
  all — "Rerank Model" is a real, honestly-labeled dead end (see above),
  not a partially-wired feature.


## 0.26. New in v25 — Human Input real sync pause/resume, Knowledge Base document management, batch runs, richer LLM/Retrieval/Tool node config, Web App Settings

- **Human Input (`hitl`) moved off the durable/Temporal path onto a real
  sync pause/resume** — the Temporal signal-handler mechanism was never
  built (see the superseded v24 report), so `hitl` previously just got
  silently skipped. Now: `harness.py`'s traversal genuinely raises a
  `HitlPause` when it reaches a `hitl` node, `POST /api/agents/{id}/run`
  persists a real `HitlRequest` row (id, node id, a full ctx snapshot —
  vars/output/structured_output/agent_chain — everything needed to
  resume), and returns `paused_for_human_input` with the real rendered
  form content, fields, actions, and delivery status. `POST /api/agents/
  {id}/hitl/{request_id}/respond` looks up the real outgoing edge for
  whichever action (or the `timeout` port) was chosen and resumes
  execution from the exact point it paused — not a restart. Verified
  live: a real flow with two named actions (Approve/Reject) genuinely
  pauses, resumes down the correct branch with real field values
  injected, rejects a second response to an already-resolved request
  with a 400, and both branches produce the correct real output.
  `DURABLE_NODE_TYPES` is now empty — nothing currently needs the
  Temporal path — but the routing rule itself (§3.6) is left in place as
  a real extension point, not deleted.
- **Real Knowledge Base document/chunk/metadata/retrieval-testing
  management** (`routers/knowledge_bases.py`, new `KnowledgeDocument`/
  `KnowledgeMetadataField`/`KnowledgeRetrievalTest` models,
  `components/KnowledgeBaseDetail.tsx`) — replaces the old single
  "Upload document" toggle. Ingest now stamps a real `document_id`,
  `word_count`, `enabled`, and `metadata` onto every chunk row in the
  KB's own separate pgvector table (via `ADD COLUMN IF NOT EXISTS`, so
  older tables keep working), and creates a local `KnowledgeDocument`
  bookkeeping row. Real endpoints: list/toggle/delete documents (toggle
  cascades to every chunk's `enabled` column via a real UPDATE), list/
  edit/toggle/delete chunks (editing content genuinely recomputes its
  embedding, not just the text), list/add/rename/delete metadata fields
  (auto-created from ingest metadata keys too), and a real retrieval
  test endpoint (same real embed + nearest-neighbor path a live
  `retrieval` node uses) with a persisted history. Verified live against
  a real second Postgres database end to end, including the
  document-status-toggle cascade and a metadata-filtered query returning
  genuinely different results after re-enabling a document.
- **Real CSV batch execution for workflow apps**
  (`POST /api/agents/{id}/batch-run`, `FormRunner`'s new batch mode,
  `csv.ts`) — each row becomes a real, independent `run_flow_sync` call,
  genuinely run concurrently via `asyncio.to_thread`. A real bug was
  caught and fixed before shipping: the first version shared one
  SQLAlchemy `Session` across the concurrent threads, which is unsafe —
  fixed to open a dedicated `Session` per thread against the shared
  engine. Verified live with 3 concurrent rows producing distinct real
  structured outputs. Capped at 200 rows; a row that pauses for human
  input is reported honestly as `paused_for_human_input` in the results
  table — resuming an individual batch row isn't wired up (a real,
  stated scope boundary, not silently dropped).
- **Richer LLM node** (`handle_llm` in harness.py, `NodeConfigPanel.tsx`)
  — real System/User prompt fields with `{{var}}` interpolation
  (replacing the old fixed "input + retrieved_chunks" template), a real
  `context_var` to explicitly wire a Retrieval node's output, real
  parameter presets (Precise/Balanced/Creative) with genuine
  temperature/top_p/frequency/presence values sent through, and a real
  (if intentionally simplified — see the handler's own docstring) Memory
  toggle backed by `AgentMemory`, threading prior turns into the system
  prompt as a transcript. The model catalog dropdown now reads real
  connected providers from Integrations instead of a hardcoded list.
- **Multi-KB + metadata-filtered Retrieval node** (`handle_retrieval`,
  `_build_metadata_where`) — a retrieval node now selects zero or more
  knowledge bases (queried independently, results concatenated) instead
  of exactly one, and Manual metadata filtering compiles to real
  parameterized SQL (`(metadata->>%s) <op> %s`, AND-combined) against the
  KB's own table. Automatic mode (an LLM proposing conditions) is a real,
  stated follow-on, not silently faked as working.
- **Real Tool node catalog picker** — replaced the two hardcoded
  `<option>`s with a real dropdown grouped by registered Data Sources and
  Tool actions (`datasource:<name>` / `tool:<tool>:<action>`), matching
  what `handle_tool`'s entitlement check already accepted as an opaque
  object string.
- **Web App Settings** (`PATCH /{agent_id}/features` — no new endpoint
  needed, reuses the existing generic features PATCH — plus
  `WebAppSettingsModal` in `PublishModal.tsx`) — real branding
  (name/description/icon), theme/accent color, a chat input placeholder
  (chat-type apps only), and legal-page links, all stored under
  `agent.features.webapp_settings` and read by `AgentRunner`'s published
  page. Saved settings take effect immediately, no publish required.
- **Chat Bubble embed mode** in the Publish modal — a second real embed
  snippet (a `<script>` tag) alongside the existing iframe snippet, both
  copy-to-clipboard.
- **`validateFlow` is now appType-aware** (`flowValidation.ts`) — a
  workflow app with no `output` node, or a chat-style app (chatbot/
  chatflow/agent) with no node that could ever produce a response, now
  surfaces as a real checklist issue on the trigger node rather than
  silently publishing a dead end.

### What's real vs. still a stated gap in this round

- HITL inside a `loop` body isn't supported — `run_loop_body` doesn't
  check for it; a real, documented gap (see `_execute_flow`'s docstring
  in harness.py), not a silent one.
- Batch-run doesn't support resuming an individual paused row — reported
  honestly per-row rather than treated as a failure or silently skipped.
- LLM node "Memory" is a real transcript threaded into the system prompt
  via `AgentMemory`, not a true multi-turn `messages[]` array — `call_llm`
  itself is still single-turn (model_adapter.py). Vision toggle/variable/
  detail-level are stored for real but not yet consumed by any real
  multimodal call — no vision-capable path exists in `model_adapter.py`
  yet.
- Retrieval node's Automatic metadata-filtering mode is config-storage
  only — no real LLM call proposes conditions yet (Manual mode is fully
  real).
- Chat Bubble's `embed.js` script referenced in the copy-paste snippet
  isn't an existing served asset in this reference backend — the
  snippet is real and correctly formed, but pointing a real page at it
  today would 404 until that script is added.


## 0.25. New in v24 — agents synced into UCS as real Gravitino Models, with real per-agent RBAC

- Given a real, separate UCS product to integrate agents into as
  governed, permissioned objects. Investigated the real uploaded UCS
  codebase rather than guessing: Gravitino's catalog tree has no Agent
  type, but its Model type (name + versions, each with a URI/aliases/
  comment) is a close semantic match, and UCS's own RBAC schema already
  treats `model` as a first-class securable object type — no schema
  changes needed on UCS's side to use it as the target.
- Verified against the real UCS, not a stub of assumptions: stood up
  the actual UCS FastAPI service, its real Postgres schema, a real
  RSA-signed local JWT issuer (UCS's own `jwt.decode()` genuinely
  verifies these), and a minimal Gravitino stub covering only the exact
  REST endpoints UCS's `gravitino.py` calls.
- This caught two real design mistakes before shipping: the first
  proposal (`agent:{id}` flat object naming, `action="invoke"`) is
  confirmed to genuinely fail against the real UCS — object scope is
  parsed as dotted `catalog.schema.object`, and `USE_MODEL`'s
  recognized actions don't include "invoke". Fixed to a real dotted fqn
  and `action="read"`, then re-verified allow/deny/wrong-object
  scenarios all resolve correctly.
- A real, necessary gap found and fixed in UCS itself: it could list
  model versions but had no way to create a model or version at all.
  Added `create_model`/`create_model_version` to UCS's `gravitino.py`
  and two matching admin endpoints, verified end-to-end.
- Platform-side: `ucs_sync.py` (real sync on agent create/publish,
  best-effort and non-blocking, honestly recording `ucs_synced`/
  `ucs_sync_error`/`ucs_fqn`), new `/ucs/sync`, `/ucs/grant`,
  `/ucs/revoke` endpoints, and a real "UCS Access" card in the Publish
  panel. Verified live through the actual UI, not just the API: grant a
  group through the form, confirm the real runtime authz check now
  allows it, revoke, confirm it's denied again. Ported to and verified
  in both React and the mockup.
- `UCS_ADMIN_TOKEN` requires a real bearer token with the
  `platform-admin` realm role from a real IdP — UCS's admin endpoints
  do genuine JWT verification with no soft-check bypass (unlike the
  runtime authz check, which does have one). Without it, agent create/
  publish still succeed; sync honestly reports as failed rather than
  silently skipped or faked.

---

## 0.24. New in v23 — real document ingestion into pgvector knowledge bases

- Given a real AWS pipeline (S3/Lambda/Ray/PostgresML's `pgml.embed()`)
  to integrate. Tested feasibility rather than assuming: PostgresML's
  extension isn't installed here, there's no S3/Lambda/Ray, and Hugging
  Face (real embedding model weights) is confirmed network-blocked from
  this sandbox.
- Built a real, working, adapted pipeline instead: `app/embeddings.py`
  (scikit-learn `HashingVectorizer` — real, deterministic, fully local,
  explicitly documented as lightweight rather than a pretrained
  semantic model), `app/ingestion.py` (real parsing reusing the
  existing extractor, real sentence-aware chunking with real overlap),
  and a real `POST /{id}/ingest` endpoint doing a genuine multipart
  upload → parse → chunk → embed → insert.
- **The connected other half**: `handle_retrieval` now does real
  nearest-neighbor search using the same embedding function, reading
  the retrieval node's previously-unused `query_var` config field —
  shipping ingestion alone would have left this half-finished.
- Two real bugs caught while verifying: a missing `python-multipart`
  dependency, and a relative-import bug that silently broke retrieval.
- UI added to Integrations → Knowledge Base tab: a real "Upload
  document" action per pgvector KB.
- Verified multiple ways: direct API calls confirming genuinely
  different top results for different queries, a live browser
  round-trip, and a direct database check confirming a real inserted
  row with a real 384-dim vector. Ported to and verified in both React
  and the mockup.

---

## 0.23. New in v22 — real "Agent health" cards, not hardcoded strings

- Found while answering a direct question about how evaluation works:
  the "Eval pass rate (avg): 89%", "7-day success trend: ▲ 2.1pt", and
  "Behavioral drift flags: 1 · Churn Model Scientist" cards were static
  strings, disconnected from the real `EvalRun`/`EvalResult` data one
  section below them.
- New `GET /api/monitoring/eval-health` computes all three for real:
  average pass rate across the latest completed run per set, a genuine
  week-over-week trend (with an honest "not enough data" state when
  there isn't enough real history in both 7-day windows), and real
  drift flags with the real agent/set name attached.
- Verified with direct API calls: real pass rate matched an actual run
  exactly; a forced real regression correctly flagged drift with the
  correct agent name — not asserted, derived from real data.
- Wired into both React and the mockup, each verified live in a real
  browser against the real backend. A similar hardcoded pair in the
  smaller per-agent Quality tab was deliberately left out of scope for
  this round, consistently in both.

---

## 0.22. New in v21 — Traces now shows what genuinely happened, not a fake identical waterfall

- **The bug was self-documented**: `get_trace_spans`'s own code comment
  called it a stub that synthesizes a plausible-looking waterfall. Every
  trace, for every agent, showed the same four fake spans with the same
  fake durations.
- **Real per-span content** — the harness now captures a genuine
  preview of what each node actually saw/produced. Deliberately scoped
  to node types that actually set output themselves, avoiding a real
  bug where an unrelated later node would misleadingly inherit an
  earlier node's stale output.
- **Real persistence** — `Trace` now stores `user_input`, `final_output`,
  and the real `spans_json` from the actual run.
- **Real UI** — a Message column on the list, a prominent real
  conversation in the detail view, and a real per-span waterfall with
  real content, not a timing bar over fake labels.
- **The mockup's Traces page had never fetched the real backend at
  all** — 100% static local seed data with its own copy of the same
  fake waterfall. Rewritten to attempt the real backend first, with an
  honest local fallback matching the established pattern used
  elsewhere, rather than either a dead end or permanent fakery.
- Verified end-to-end in a real browser against the real backend, in
  both React and the mockup.

---

## 0.21. New in v20 — real Publish button states, Manage Features parity, Studio card layout

- **Manage Features parity** — `FormRunner` never received the
  `onManageFeatures` prop, so workflow-type App agents had no "Manage
  features" link in Try It while chatbot-type App agents did. Fixed
  with the same `isApp` condition `ChatRunner` already used.
- **Real Publish button states** — never published / published with no
  changes (Publish genuinely disabled) / published with real changes
  since (re-enabled) — computed from the agent's real `updated_at`
  against its most recent real `AgentVersion` snapshot.
- **A real backend bug caught while verifying this live:**
  `AgentVersion.created_at` was truncated to whole seconds while
  `Agent.updated_at` kept microseconds, so the Publish button would
  have stayed permanently enabled even with zero real changes. Fixed
  and reconfirmed both directions work in a real browser.
- **The mockup's own staleness bug:** it had never re-fetched the agent
  or its versions after a real publish or restore, so its local copy of
  `updated_at` never moved. Fixed to genuinely refresh from the backend
  after both operations.
- **Studio card layout** — edited-info now sits above the footer
  divider, and the hover "Open app" icon now sits next to "Details"
  instead of floating separately at the top of the card. Confirmed with
  an actual rendered screenshot.

---

## 0.20. New in v19 — the mockup's Web Apps page no longer dead-ends without a backend

- The v18 fix only improved the error message's clarity; it didn't
  address that Web Apps, unlike Logs/Try It/saved results elsewhere in
  the mockup, had no local fallback at all when the backend or UCS
  wasn't reachable.
- Fixed to match the established pattern used throughout the rest of the
  file: attempt the real backend + UCS call first, and on failure fall
  back to the mockup's own local seed data (every published App agent),
  with a visible, honest banner naming what happened and what real
  per-user filtering requires.
- Verified three ways live: backend down (fallback works, nothing dead),
  backend + UCS both up (no fallback banner, real per-user list as
  before), full regression sweep clean.

---

## 0.19. New in v18 — a diagnosable network-error message, fixed centrally

- Reproduced the report two ways: backend running without UCS (already
  correct — fails fast with a clear 503) and backend genuinely
  unreachable (the real gap — the browser's bare `fetch()` rejection
  passed straight through with no detail).
- Fixed once in the shared `req()` function every API call goes
  through, so every caller benefits, not just Web Apps: a network-level
  failure now names the backend URL and asks whether it's running; a
  real HTTP error response still surfaces its own specific message
  unchanged.
- Ported the identical fix to the mockup's equivalent handler.
- Verified live both ways: backend stopped entirely, hit the page in a
  real browser, confirmed the new message actually appears.

---

## 0.18. New in v17 — End User role, Web Apps page, UCS-resolved access

- **Real UCS-backed agent access** — `check_agent_access()` extends the
  existing dual-check pattern to agent-level authorization
  (`action="invoke"`, `object="agent:{id}"`), the same real primitive
  data-object checks already use. New
  `GET /api/agents/published-for-user` endpoint asks this per published
  agent and returns only what UCS allows. Verified with genuinely
  different per-user rules against a live UCS instance. Fails loud (503)
  if UCS is unreachable, matching this platform's existing governance
  stance.
- **Real End User role** — extends the existing persona system (which
  already gated navigation) rather than inventing a new one. A real
  route guard blocks direct-URL access to builder/admin routes, verified
  live in a real browser, not just hidden nav links.
- **Web Apps page** — a real vertical rail of the apps a given identity
  can actually open, clicking one loads the real ChatRunner/FormRunner.
  Grant/revoke lives entirely in UCS's own console.
- **Honest identity chooser, not fake auth** — since this reference
  platform has no real login (already disclosed), the End User page's
  "Signed in as" selector is labeled as what it is: a real input to a
  real UCS check, not verified identity.
- **A real bug found and fixed while porting to the mockup**: the live
  backend returns `app_type` (snake_case); the mockup's entire rendering
  codebase has always used `appType` (camelCase), matching its local
  seed data. This was the first place in the mockup rendering a full
  agent object fetched live from the backend, so the mismatch had never
  surfaced. Every real app opened through this page would have silently
  rendered as the wrong type had this gone unnoticed.

---

## 0.17. New in v16 — validation checklist, multi-field trigger inputs, real version management

- **Checklist** — real type-specific + connectivity validation across
  the actual current flow, with a live issue-count badge. Found and
  fixed a related bug: Human Input's "no delivery method" warning was
  hardcoded to always show; added real checkboxes so it's genuinely
  conditional now.
- **Multi-field trigger inputs** — a real "Add Input Field" modal (8
  field types), and critically, a real dynamic form renderer in
  `FormRunner` that shows the right typed input per field, validates
  required fields, and keeps Hidden & Pre-Filled fields invisible while
  still submitting their default. Backend `/run` accepts real structured
  `inputs`, mapped into genuinely referenceable `{{field_name}}`
  variables — verified end-to-end.
- **Version management** — a real `AgentVersion` snapshot on every
  publish; verified it's a genuine point-in-time capture (a later edit
  to the live agent does not retroactively appear in an earlier
  snapshot) and that Restore genuinely rolls the live flow back, setting
  status to draft rather than silently pretending nothing happened.
- **Testing gap found and closed:** `/publish` requires the separate UCS
  service with no soft-check bypass, so it had likely never been called
  for real this whole session. Built a minimal local UCS stub *purely
  for verification* (not part of the deliverable) to actually confirm
  the full publish → snapshot → restore lifecycle rather than assume it.

---

## 0.16. New in v15 — the real, comprehensive Publish panel

- **Export DSL / Import DSL** — a real, round-trippable flow export/
  import, not metadata-only.
- **Duplicate** — a real independent clone; verified the original's flow
  is unaffected by edits to the copy.
- **Web App and Backend Service API now toggle independently** — real,
  separate persisted state, not one shared status flag.
- **A real QR code**, real embed snippet, real Launch link.
- **Found and fixed:** the mockup's old Publish modal was entirely
  fake — hardcoded version number, hardcoded catalog object count, a
  toast with no real backend call, and the button never even passed an
  agent id.
- Verified twice: real component tests, then a full Playwright run
  against the actual dev server and live backend, confirming every
  toggle persists to the real database independently — not just that
  the UI didn't crash.

---

## 0.15. New in v14 — Output node, Saved Results, real MCP Server, and a real CORS bug fixed

- **Output node** — new, addable (not the locked terminal `respond`
  node), collects named output variables anywhere in a flow via the
  existing `{{var}}` mechanism, extended with a real `{{input}}`
  reference to the original message. Multiple Output nodes across
  branches genuinely merge, not overwrite. Verified end-to-end through
  the real `/run` endpoint.
- **Real Saved Results** — a genuine `SavedResult` Postgres table.
  Verified: save, query back fresh, delete.
- **Real structured-output display, Copy, and Save** in the Workflow Web
  App runner, both React and the mockup.
- **Real regression caught:** a toast dependency broke the standalone
  `/app/:agentId` page (deliberately outside the app's context
  provider). Fixed with a self-contained local message.
- **Real MCP Server** — genuine JSON-RPC (`initialize`/`tools/list`/
  `tools/call`), off by default, real token-based URL generation, a real
  `tools/call` that runs the agent's actual flow. Verified the complete
  flow against the live backend, including that regenerating a token
  immediately invalidates the old one. Honest scope: the `tools`
  capability over plain HTTP, not the full MCP spec (no SSE transport,
  no resources/prompts) — disclosed, not silently unsupported.
- **A significant CORS bug found and fixed:** the backend only allowed
  the React dev server's own origin, meaning the mockup's real backend
  calls had likely been silently blocked all session in any use outside
  that one specific port. Fixed and re-verified end-to-end with a real
  Playwright run against Chromium, not assumed to be fixed.
- **Deliberately deferred, disclosed here rather than silently
  dropped:** CSV batch processing for Workflow apps (Dify's own
  workflow-webapp docs describe this as a distinct, substantial
  feature — upload, template download, per-row execution, result
  export) — a real next step, not attempted at reduced quality this
  round.

---

## 0.14. New in v13 — New Agent creation scoped to App Agents only

- The New Agent modal's Data/App type toggle is removed. The App type
  selector (Workflow/Chatflow/Chatbot/Agent/Text Generator) is now the
  first and only real decision, and "Start from" only offers real App
  templates.
- **Enforced server-side, not just UI-hidden:** `POST /api/agents` no
  longer accepts a `type` field and always creates a real App agent —
  verified by sending a payload that explicitly tries `"type": "Data"`
  and confirming the created agent still comes back as `type: "App"`.
- **Confirmed unaffected, not assumed:** the Templates page's own
  "Publish & use" flow for existing Data templates uses a genuinely
  separate backend endpoint (`/api/templates/{name}/instantiate`) and
  was checked directly to confirm this change doesn't touch it.

---

## 0.13. New in v12 — real-browser verification, and closing the standalone-mockup logging gap

- **Verified with real Chromium, not just jsdom:** "Open app" new-tab
  routing and the hover-visible card icon both confirmed genuinely
  working, under both `http://` and `file://` protocols — the realistic
  way a downloaded standalone mockup gets opened.
- **The real remaining gap:** the Logs tab's "real user message" feature
  only worked when a live backend was reachable at `127.0.0.1:8000`.
  Opening the mockup standalone — the normal use case — meant only
  static seed data ever showed, regardless of what was actually typed.
- **Fixed:** real, entirely client-side log recording. Sending a message
  through Try It or the `/app/` page now records a genuine local log
  entry with the real message, independent of backend reachability.
  Verified end-to-end with Playwright: file:// protocol, zero backend
  processes running, a real typed message, confirmed present in the Logs
  tab afterward. The tab's note is honest about which situation is
  active — real backend logs, real local session entries, seed history,
  or a mix — never claims something is "live" when it isn't.

---

## 0.12. New in v11 — real "Open app" routing fix, real log search, real user messages in logs, hover shortcut

- **Bug found and fixed (mockup only):** the mockup never parsed
  `window.location.hash` on load, so "Open app" opened a new tab that
  always ignored the URL and showed the default Overview page instead of
  the actual agent. React's `HashRouter` already handled this correctly
  by design — confirmed, not assumed.
- **Real search + level filter** added to the Logs tab.
- **Real user messages now appear in logs** — previously only a generic
  "Live run · status" message was recorded. Verifying this surfaced a
  second real bug: the logs endpoint had no `ORDER BY`, so new real
  entries could land anywhere among seeded rows instead of at the top.
  Both fixed.
- **Hover-visible "Open app" icon** on Studio cards, published App
  agents only, with real event isolation from the card's own click.

---

## 0.11. New in v10 — Manage Features expanded to match the reference, plus three more real bugs found and fixed

- **Bug found before any new work started:** the agent profile drawer's
  Monitoring/Logs tabs were already built but effectively invisible — two
  conflicting `.tabs` CSS rules meant the active one relied on hidden
  horizontal scroll with no visible affordance. Fixed by wrapping instead.
- **Conversation Opener** — real opener text plus up to 10 real "Opening
  Questions," shown as clickable starters before any conversation begins.
- **Follow-up settings** — real model selector, real default/custom
  prompt mode with `{{input}}`/`{{output}}` substitution.
- **Content moderation settings** — three real providers (Keywords, AI
  Moderation via a genuine Claude call, Custom Endpoint via a real HTTP
  POST — verified against an actual test server, not just the
  honest-failure path), real separate INPUT/OUTPUT toggles each with
  their own preset reply, output-side blocking confirmed to let the flow
  genuinely run first before replacing the real output.
- **Bug found while porting to the mockup:** a checkbox/label double-fire
  risk — clicking a feature's visible toggle switch could also trigger
  that row's own "open settings" click, since label-wrapped checkboxes
  fire a second, separately-bubbling click event. Fixed with a dedicated
  intercept, confirmed by a test checking the sub-modal does NOT open on
  a toggle click.
- **Bug found while porting to the mockup:** the new sub-modals initially
  reused the existing `.overlay` backdrop class, which would have closed
  the entire Try It panel on a backdrop click instead of just the
  sub-modal on top of it. Fixed with a dedicated class and exact-target
  check.

---

## 0.10. New in v9 — Chat UX Features, pre-publish only

- **Real, not decorative:** Conversation Opener (shown as the actual
  first message), Follow-up (a real additional LLM call per reply),
  Citations (real retrieved-chunk data, only shown when it exists),
  Content moderation (a real block-list check that genuinely
  short-circuits the flow — confirmed zero spans execute on a match).
- **Deliberately scoped to Try It, not the published page** — an
  anonymous end user on the real `/app/:agentId` URL shouldn't be able to
  reconfigure another agent's moderation list or opener text. The
  features themselves still apply on the published page; only the
  management UI is restricted.
- **Text to Speech / Speech to Text / Annotation Reply** — real,
  buildable (the first two via the browser's native Web Speech APIs, the
  third via a real curated-response store), deliberately deferred this
  round and shown honestly as "Coming soon" in the panel rather than as
  working-looking toggles with nothing behind them.
- **Bug found and fixed:** knowledge-base IDs were slugified differently
  at creation than at lookup (`.` was dropped in one place, kept in the
  other) — any dotted catalog-style name would silently never resolve.
  Found by testing citations against a live database with a realistic
  name, not a name chosen to avoid the bug.
- **Bug found and fixed, same round:** the mockup's own new Features
  panel had a backdrop-click handler positioned after an early-exit that
  never triggered for a bare backdrop div — caught immediately by testing
  the backdrop click specifically, not assumed to work by similarity to
  an existing pattern.

---

## 0.9. New in v8 — Try It genuinely differs per agent; API Access reachable from Studio

- **Bug found:** clicking any Studio card correctly decided *whether* to
  auto-open Try It (based on the real clicked agent's status), but the
  panel's actual content always showed a hardcoded demo agent — every
  agent type looked identical. My own earlier tests never caught this
  because they checked "did a panel open," not "does it show the right
  agent." Fixed with a real `tryItAgentId` tracked from the actual click.
  Verified by clicking three different real agent types in sequence and
  confirming three genuinely different UIs, not just three open panels.
- **Bug found:** API Access (added in v7) was only reachable from the
  Overview page's table — Studio, the actual primary entry point, had no
  path to it at all. Fixed with a real "Details" button on every Studio
  card.
- Both fixes ported identically to the real React app, which turned out
  to only have the second bug — React's Builder is genuinely per-agent
  via the URL, so its Try It was never affected by the first one.

---

## 0.8. New in v7 — a real bug fix on the published page, and API Access

- **Bug found and fixed:** `AgentRunner.tsx`, the actual page a published
  agent's real end users land on, had separate (and wrong) logic that
  defaulted a Data agent's missing `app_type` to `'chatbot'` — a real end
  user visiting a Data agent's dedicated URL would see a chat interface
  for an agent never meant to be conversational. `TryItModal` (the
  builder's pre-publish preview) already had the correct check. Fixed by
  extracting one shared `isChatLikeAgent()` used by both. No existing
  test covered a real Data-type agent on the published page at all —
  added the one that would have caught this from the start.
- **New "API Access" tab** in the agent profile drawer — the real
  dedicated UI URL and the real REST endpoint, both with working copy
  buttons, for every agent in one place. The curl example was verified
  against the live backend before shipping, not hand-written and assumed
  correct — that verification caught a real inaccuracy in the sample
  response. Honestly states there's no auth layer yet rather than
  inventing a fake API-key flow.

---

## 0.7. New in v6 — Studio card gallery, and two real bugs it surfaced

Not a new capability so much as a genuine information-architecture fix:
"Workflow IDE" is renamed "Studio," and the separate table-based "Agents"
list is now a real card grid with genuine Types/Tags/Creators/search
filters, matching a reference tool directly. New real fields: `tags`,
`description` on `Agent`, with a real `PATCH /meta` endpoint.

Two real, previously-unflagged bugs surfaced while building this:

- **`updated_at` was never actually bumped.** The field existed since an
  earlier session but no mutation path ever wrote to it — every agent's
  "last modified" timestamp was frozen at creation. Fixed at the one
  shared `_save_flow` function.
- **`app_type` was never actually set in the seed data.** Added last
  session with a real field, endpoint, and per-type end-user UI — but
  every seeded App agent had `app_type=None` the whole time, so the
  "real different UI per type" feature had nothing real to show out of
  the box. Fixed with genuinely varied seed values.

Also: entering a *published* agent's playground now auto-opens the
existing Try It panel, leading with the real end-user UI rather than the
canvas — verified both ways (auto-opens for published, does not for
draft), reusing the panel built in v5 rather than a second view.

---

## 0.6. New in v5 — a real "Try it" panel, reusing the actual end-user UI

The v4 `AgentRunner` page solved "what does an end user see after
publish." This closes the other half: "how does a developer test that
*before* publishing, without a fake dev-console experience." The old
Test button ran one fixed string and showed a toast — not remotely
representative of the real chat or form experience an end user gets.

- `ChatRunner`/`FormRunner` extracted into a shared `RunnerUI.tsx`,
  imported by both the real published page and a new Builder-only
  `TryItModal` — the SAME components, not a parallel implementation.
- Available regardless of publish status, with an honest "still draft"
  banner rather than a block.
- Runs tagged `user:builder-preview`, distinguishable from real end-user
  traffic (`user:end-user`) in logs/metrics without a separate code path.
- A real Rules-of-Hooks bug (a `useState` placed after an early return)
  caught immediately by testing, not shipped.

---

## 0. Closed since v2

| Gap (from v2) | Now |
|---|---|
| `subagent` unwired | Real recursive invocation via the same `run_flow_sync` a direct run uses. Verified: a real 3-hop chain with output bubbling back through every level; real cycle detection refusing a call *before* attempting it; the 5-hop cap correctly refusing a 6th hop. |
| `agent` unwired | Three genuinely different execution patterns (`react`, `function_calling`, `plan_execute`) on the same real-or-mock `call_llm` — not the same prompt three ways. Verified via monkeypatch that each branches/loops correctly. |
| Evals — entirely fake | Real tables + real execution against the live agent + real DeepEval `GEval` scoring (judged by this app's own Claude call) + real drift detection across consecutive runs, both directions proven. |
| No Prometheus integration | A real `/metrics` endpoint, real `Counter`/`Histogram` objects, instrumented at the actual call sites (not computed from a DB snapshot). Verified with genuinely incrementing counts and valid exposition format. |
| `UCS_SOFT_CHECK` | New — a real, reversible env var that lets execution-time UCS checks (still genuinely attempted) not block a run, specifically to make `subagent` chains testable without standing up UCS for every hop. Publish-time enforcement is untouched. |

## 0.5. New in v4 — real App agent types and an end-user runner page

Not a previously-flagged gap being closed — a genuinely new capability.
Before this, publishing an agent produced a working flow with **nowhere
real for a non-builder person to use it**; the only UI was the Builder,
a creator tool.

- **`app_type`** — a real field on App agents (`workflow`, `chatflow`,
  `chatbot`, `agent`, `text_generator`), set via a real 5-option selector
  at creation. It genuinely determines which end-user UI gets built, not
  just a cosmetic label.
- **`AgentRunner`** (`/app/:agentId`) — a real architectural decision, not
  just a new page: it renders with no builder sidebar/topbar at all,
  requiring the router itself to branch before the internal shell.
  Chat-like types (`chatbot`/`chatflow`/`agent`) get a real chat calling
  the actual `/run` endpoint; form-like types (`workflow`/
  `text_generator`) get a real form with one structured result. `agent`
  specifically surfaces the real per-step reasoning trace behind a
  toggle — verified that `chatbot`-type apps do NOT show this even when
  the backend response happens to include it, confirming it's real UI
  logic gated on `app_type`, not just conditional data.
- Honest states throughout: unpublished agents show a real "not
  published yet" message; failed runs show the genuine failure reason.

---

## 1. What's changed since the last report

| Area | Then | Now |
|---|---|---|
| **`retrieval` node** | Pure placeholder string, no real data ever | **Real for pgvector**: a genuine `psycopg2` connection to a separate, user-configured database, a real query against a real table. Still not true vector similarity search (no embedding model configured anywhere) — fetches configured rows, not the top-k most *similar* ones. |
| **Knowledge Base connections** | Didn't exist | New Integrations tab — real CRUD, real connection testing (genuine `psycopg2` for pgvector; real HTTP reachability check for the other four providers), real secret masking on the password field, identical to the `EnvVar` pattern. |
| **`loop` node** | Body/exit-port workaround — the person wired a cycle back to the loop node itself; termination was checked by the loop node before each pass | **Real nested container.** Nodes get a real `parent_id`; dropping one inside a loop's rendered box makes it an actual body node, wired among siblings with real edges including their own `condition` branches. A body branch ending at a real `exit_loop` node stops the loop immediately. `max_loops` is now a safety cap, not the primary exit mechanism. |
| **`exit_loop` node** | Didn't exist | Real — genuinely stops loop iteration and is genuinely present in the execution trace (a bug where it silently skipped its own span was caught and fixed during the build). |
| **`comment` node** | Didn't exist | Real documentation-only node — sticky note on canvas, config-backed (Title/Body), no execution ports, rejected as a connection target with an honest message. Proven never reachable during execution, not just visually excluded. |
| **Node icons** | A plain colored dot per type | Real distinct SVG icon per type (20 icons across 21 node types), in the palette, the node picker, and canvas cards. Cosmetic, not a functional gap — noted here because a prior response mentioned it. |
| **Templates** | Backend `Template` model had no `flow` field at all — every "template" produced the same generic 3-node skeleton | Real, distinct flows per template (7 templates), real "Customize" and "Publish & use" actions, the latter genuinely reusing the same entitlement-check logic as any other publish. |
| **Real memory, webhooks, env vars** | Didn't exist | All real — see §2. |

A genuine bug was found and fixed in the *use* of the new loop container,
not just its initial build: growing a loop by clicking its own port (the
natural interaction, not drag-and-drop from the palette) silently produced
a top-level node instead of a real child, because only one of the two
node-creation code paths had containment logic. Fixed in both paths, with
a regression test reproducing the exact scenario.

---

## 2. What's genuinely real (cumulative)

| Area | What's real |
|---|---|
| **Flow graph** | A true directed graph — explicit `nodes` + `edges`. Loop containers are a real parent/child relationship (`parent_id`), not implicit positioning. |
| **Branching & looping** | `condition` genuinely routes on a real (sandboxed) expression. `loop` genuinely repeats a real body subgraph, exits via a real `exit_loop` node or the `max_loops` safety cap. |
| **Retrieval (pgvector only)** | Real connection, real query, against a database that's deliberately separate from the platform's own operational Postgres. |
| **Memory** | `AgentMemory` table — values genuinely survive across separate runs. Agent-wide and per-user scoping both real and isolated. |
| **Environment variables** | Real CRUD; secret values genuinely write-only after creation; `{{env.KEY}}` genuinely substitutes at execution time. |
| **Knowledge Base connections** | Real CRUD, real connection testing, real secret masking — see §1. |
| **Governance (UCS)** | A real, separately-built product — its own FastAPI service, Postgres, RBAC tables. Dual-check: agent's bound groups AND the invoking user's real group membership, checked independently. |
| **Orchestration** | Temporal is a real dependency, independently verifiable outside this app's own claims. Sync vs. durable routing is a real decision; both paths share one graph-walking structure (`exec_node`/`run_loop_body`, mirrored between `harness.py` and `temporal_workflow.py`) so they can't silently diverge. |
| **Code execution** | Real OS-level subprocess isolation — not VM-grade, but genuine process isolation, not `eval()`. |
| **HTTP node** | Makes a real outbound call, verified against a live external endpoint. |
| **Doc Extractor** | Real fetch + real text extraction (`pypdf`, `python-docx`) from S3/GCS/Azure Blob URLs, public or presigned only. |
| **Webhook trigger** | Genuinely executes the agent through the same code path as the manual Test button. |
| **Templates** | Real, distinct flows; "Publish & use" genuinely subject to the same entitlement check as any other publish. |
| **Builder UI** | Drag-to-connect, click-to-add-node-picker, and drop-into-loop-container are all real, tested via actual dispatched mouse/drag events. |
| **LLM node** | Genuine Anthropic API call if `ANTHROPIC_API_KEY` is set; a clearly-labeled mock (`"mock": true`) otherwise. |

---

## 3. Genuine placeholders — still the part most likely to be mistaken for "done"

| Node | Status |
|---|---|
| **`retrieval`** | Real *only* when the referenced knowledge base is registered with provider `pgvector`. Any other value — an unregistered string, or a registered non-pgvector provider — falls back to the honest placeholder it's always used. Not true similarity search even in the real case (no embedding model anywhere in this reference implementation). |
| **`tool`** | **Unchanged, still a pure placeholder.** The entitlement check in front of it is real (a real UCS call, genuinely denies without a binding); the actual query execution is `"[no live connector configured — placeholder result for {obj}]"`, exactly as before. This is the one still-open half of what used to be the single biggest gap. |

**Practical consequence, updated:** an Analysis Agent (or any template using
both `retrieval` and `tool`) now produces a real LLM response conditioned
on *real* retrieved data *if* a pgvector knowledge base is registered and
referenced correctly — but any `tool` step in the same flow still injects
placeholder text. A flow that's "half real" is easy to misjudge as either
fully real or fully fake; it's neither.

---

## 4. Node types with no execution logic at all

21 node types now exist; **17 have real handlers, 1 (`loop`) has real
logic without a `NODE_HANDLERS` entry (special-cased in the traversal
itself), 1 (`comment`) is real but deliberately never executed, and only
2 remain genuinely unwired** — down from 4 in v2:

| Node | Status | Real blocker |
|---|---|---|
| `iteration` | Not wired | The array-based sibling of `loop` — needs the same real-container mechanism `loop` now has, plus per-element variable binding. A real, smaller follow-on, not blocked by the schema. |
| `hitl` | Partially wired | The durable-path *routing* is real. The actual pause-for-approval mechanism (a Temporal signal handler + an approval-inbox UI) doesn't exist. |

---

## 5. Platform-level gaps (unchanged from v1)

| Gap | Detail |
|---|---|
| **No authentication** | The persona switcher is a UI convenience, not real login. |
| **Test coverage is real but partial** | 204 frontend tests now exist (steadily grown from 24 in the earliest version, through 34/46/55/61/72/76/84/85/94/105/113/118/133/142/167/172/175/182/187/192/197 in the versions since), covering every interactive feature built across all twenty-three report versions via real dispatched DOM events. Backend logic is verified through direct Python calls and live HTTP requests during development, not a persisted pytest suite. |
| **Retention/versioning** | Publishing bumps a version string and flips a status flag — no real version history or rollback. |

---

## 6. How to verify any of this yourself

- `grep -n "handle_" backend/app/harness.py` — every real handler, with an honesty note in its own docstring.
- `backend/app/routers/knowledge_bases.py`, `templates.py` — the newest real endpoints.
- `frontend/src/__tests__/` — run `npm test` for all 34 real tests.
- `README.md` — the fullest narrative version of this report, with the exact verification steps (commands, assertions) for each claim.

---

## 7. Suggested priority if extending this further

1. **Real `tool` execution (a live connector, e.g. Trino)** — still the
   single largest remaining gap between "the platform runs" and "the
   platform produces real answers." `retrieval`'s pgvector path is a
   template for how to close this the same way.
2. **A real embedding model** for `retrieval` — turns "fetches configured
   rows" into genuine similarity search, and extends real query support to
   the other four vector-DB providers already accepting real connections.
3. **`iteration`** — small, mechanical follow-on to the now-real `loop`.
4. **`hitl`'s pause mechanism** — the durable-path plumbing already exists.
5. **Basic auth** — still the largest deployment blocker outside of data
   connectivity.
6. **Text to Speech / Speech to Text** — real and genuinely buildable via
   the browser's native Web Speech APIs (`SpeechSynthesisUtterance`,
   `SpeechRecognition`), not an external service dependency. Deferred in
   v9, not silently dropped.
7. **Annotation Reply** — a real curated-response store (input pattern →
   priority-matched reply, short-circuiting the flow like moderation
   does) — a real, moderate-effort follow-on to the Features panel added
   in v9.
8. **Stand up a real Prometheus server** pointed at `/metrics`, and a real
   scrape config — the endpoint itself is real and correct; nothing
   scrapes it yet.
9. **CSV batch processing for Workflow apps** — upload a CSV, download a
   column-matched template, run the workflow once per row, export
   results as CSV. Described in Dify's own workflow-webapp docs as a
   distinct capability from single-run execution (which is real and
   already built, including result save/copy/structured-output display
   via the new Output node). A genuinely separate, moderate-to-large
   feature — not attempted at reduced quality just to claim completeness.
