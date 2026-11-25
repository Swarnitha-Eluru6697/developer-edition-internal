# Gap report: Schemas, Quality, Lineage and Transforms tabs

**Date:** 2026-10-16
**Environment:** local stack (frontend `localhost:5001`, backend `localhost:8001`, Kafka `localhost:9092`, Kafka Connect `localhost:8083`), org `org_1001`.
**Scope:** each tab was tested and reported only. No code was changed. Each feature was exercised through the UI (headless browser, with screenshots), through the API, and against the real Kafka topics, Kafka Connect and database.

Code references use these roots:
- **BE** = `Pralai_DataConnect_Backend`
- **FE** = `Pralai_DataConnect_Frontend`

Evidence is in `scratchpad/gaps/<tab>/` for this session: the scripts, their output and screenshots (`shots/`). The full path is `/private/tmp/claude-501/-Users-lithishbabutamma-InaiEra/d3598352-437d-4cdf-a321-4878169dc483/scratchpad/gaps/`.

---

## Summary

| Tab | Bugs | High | Medium | Low |
|---|---|---|---|---|
| Schemas | 16 | 5 | 8 | 3 |
| Quality | 11 | 3 | 6 | 2 |
| Lineage | 10 | 2 | 4 | 4 |
| Transforms | 11 | 3 | 6 | 2 |
| **Total** | **48** | **13** | **24** | **11** |

Bugs rated between two levels are counted at the lower one: Medium-High as Medium, Low-Medium as Low.

Most bugs follow five patterns that recur across the tabs:

1. **Changes made after deploy don't take effect.**
   - Quality: rule edits, policy swaps and OBSERVE↔ENFORCE switches apply only after a restart.
   - Lineage: it is never redrawn after an edit.
   - Transforms: after a policy edit, the pipeline's stored config goes stale.
   - UCS: it is updated only on deploy.
2. **"Valid" doesn't mean valid.** All of these say "valid" for settings that fail later: transform Validate, the deploy readiness check, schema compatibility (a stub that always says valid), and saving quality rules. Four quality rule types are accepted but never evaluated.
3. **Some failures are invisible.** Nothing shows any of these on a page:
   - records dropped under skip-and-log
   - quality evaluation errors
   - a sink starving after ENFORCE→OBSERVE
   - a stale schema being reused
4. **Deleting leaves leftovers, and blocked deletes give the wrong reason.**
   - A deleted pipeline leaves its lineage behind, and its auto-created transform policy too.
   - A blocked delete of a policy says "references data that doesn't exist", which is the opposite of what happened.
5. **Unchecked input causes 500s.** A non-UUID id, a too-long string, or an `id` sent by the client gives a 500 on all four tabs. A 500 is sometimes shown in the UI as "Network error — is the backend reachable?".

---

## 1. Schemas

### Bugs

**S1. A few pending drift banners shrink the schema table to 0 px (High)** — **FIXED 2026-10-16**
- The fix:
  - All pending changes now share one banner, with one compact row per change. Each row names its schema.
  - The rows scroll inside the banner after about 3.
  - The table keeps at least 240 px.
- Change: FE `src/pages/Schemas.tsx`, `src/styles.css`. Test: `tests/e2e/schema-drift.spec.ts` ("many pending drift changes share one banner…").
- Checked live with 6 pending changes and 214 schemas: the table body is 216 px tall at 1280×720 and 666 px at 2000×1170, and the last schema opens.
- What I did: opened the tab at 1440×1000 with 6 pending drift approvals.
- Expected: the table stays visible and scrollable under the banners.
- What happened:
  - The table's box is 3.4 px tall; the table body has `clientHeight 0` (scrollHeight 3074).
  - No row's View button can be clicked ("page-fill intercepts pointer events").
  - At 1440×900 with 211 schemas, no row is reachable at all. It only works in a 2400 px tall window.
- Evidence: `schemas/shots/01_list.png`, `schemas/shots/11_many_schemas_900h.png`
- Code: FE `src/styles.css:175` (`.page-fill>.alert{flex-shrink:0}`) together with `:166` (the page is a fixed-height flex column), and `src/pages/Schemas.tsx:282` (one banner per pending approval, with no limit and no collapse).

**S2. Drift is never detected automatically (High)**
- What I did: deployed `gap-sch-evolve-v1`, then changed the source table:
  - renamed `qty`→`quantity` and changed it to numeric(10,2)
  - dropped `note`
  - added `added_col int NOT NULL`
  - inserted 2 rows
- What happened:
  - The topic carried the new shape: `{"id":3,"name":"c","quantity":3.5,"added_col":8}`.
  - The pipeline stayed `running`.
  - The schema still lists `id, name, qty, note`.
  - No drift approval was created.
- Code: nothing calls `detect_drift` on its own. It runs only from `POST /schemas/{id}/detect-drift` (records pasted by hand, BE `app/api/routes/schemas.py:177`) and from a manual `POST /drift-approvals`.

**S3. "Approve new schema version" creates no new version (High)**
- What I did: approved an 11-field drift approval (`82395fb3`) in the UI.
- What happened:
  - The toast said "Schema version approved".
  - The schema count did not change and the schema is unchanged (`c_date` is still `timestamp`).
  - `resulting_schema_version_id` is `null`.
- Code: BE `app/services/schema_service.py:235-251` only changes the approval's status. FE `src/pages/Schemas.tsx:69` never sends `resulting_schema_version_id`.

**S4. Any schema with a primary key is always reported as "breaking" (High)**
- What I did: sent records identical to the source rows of `evolve` to detect-drift.
- What happened:
  - The result was `id primary_key_changed / block_deployment` and `approval_required: true`, and a pending approval was created.
  - The UI says "id — no longer primary key".
  - The drawer's default sample records cause the same result, so every drift run from the UI creates a new pending approval.
- Code: inference always sets `primary_key: False` (BE `app/services/schema_inference.py:121`), and BE `app/services/schema_drift.py:35` compares that against the approved PK.

**S5. Postgres types are mapped wrongly, so unchanged tables raise drift alarms (High)**
- Wrong mappings found:
  - `interval` and `point` become `integer` (because they contain the substring "int").
  - `date` and `time` become `timestamp`.
  - `numeric(12,2)` becomes `number` without precision or scale.
  - `json`, `jsonb`, `text[]` and `int[]` become `string`.
- Effect: a drift check on the `alltypes` table's own unchanged rows produced 11 events, all false: c_bit, c_date, c_int_arr, c_interval, c_json, c_jsonb, c_money, c_point, c_text_arr, c_time and id.
- Evidence: `schemas/s2c.out`, `schemas/shots/06_review_11_fields.png`
- Code: BE `app/services/connection_discovery.py:308-318` (`_map_sql_type` matches substrings). Its type names also don't match inference's (date, decimal, object, array).

**S6. A new pipeline on an existing contract reuses an outdated schema without checking (Medium-High)**
- What I did: after the table change in S2, created pipeline `gap-sch-evolve-relink`.
- What happened: `schema_introspection_status: linked_existing`, linked to `f84c45e8` (the old columns `qty`, `note`), with no drift check.
- Code: the contract key (domain, source_system, entity, version) ignores the connection and the database (BE `app/services/pipeline_service.py:439-449`).
- An existing case: `pg-products-cdc` (Debezium) and `pg-products-source` (JDBC) share schema `71ccabf3`.
- Not verified: whether two different databases with the same entity name share one schema. The cross-database attempt was stopped by the topic-name collision check (409).

**S7. A pending `block_deployment` drift doesn't block deploy (Medium)**
- What happened: `deploy-readiness` for `gap-sch-evolve-v1` returned `ready: true`, with every check ok, while a `block_deployment` approval (`740f46ad`) was pending on its schema. The deploy went ahead.
- The same check also called a schema with `compatibility: "SOMETIMES"`, `status: "banana"` and `format: "xml"` valid.
- Code: `validate_compatibility` is a stub that always returns valid (BE `app/services/schema_service.py:149-150`). It backs both `POST /validate-compatibility` and the "Schema valid" readiness check (BE `app/services/pipeline_service.py:1100-1105`).

**S8. Some bad input gives a 500, which the UI shows as "Network error" (Medium)**
- These return HTTP 500 `StringDataRightTruncationError`, with no CORS headers:
  - an entity name over 100 characters
  - a version over 50 characters

  Because the CORS headers are missing, the UI shows "Network error — is the backend reachable at http://localhost:8001…" (`schemas/shots/08_register_long_entity.png`).
- These also return 500 (with `retryable: true`):
  - a non-UUID id on `GET /schemas/{id}`, on approve, or as `resulting_schema_version_id`
  - an `id` supplied by the client (`"my-id"`)
  - `schema_definition.fields` that isn't a list
  - detect-drift on a schema whose stored field has no `name`
- Code: the columns are String(100) and String(50) (BE `app/models/schema.py:32-35`), but the request model has no `max_length` (BE `app/schemas/schema.py:113-121`). The generic 500 handler runs outside the CORS middleware (BE `app/main.py:353-358`).

**S9. Schemas have no input validation (Medium)**
- All of these were accepted with 201:
  - empty domain, source_system, entity and version
  - whitespace or dots in names
  - unknown compatibility, format and status values
  - fields with no `name`
  - duplicate field names
  - unknown field types (`flurb`)
  - an empty definition
- None of this is checked anywhere in the backend.

**S10. Drift approvals can be approved and rejected any number of times (Medium)**
- What I did: approved, then rejected, then approved the same approval again.
- What happened:
  - Every call returned 200 and overwrote the previous reviewer and decision.
  - A made-up `resulting_schema_version_id` (all zeros) was accepted, and so was an empty `reviewed_by`.
  - The same drift raised twice gave two pending approvals.
- Code: BE `app/services/schema_service.py:205-233` checks neither the current status nor that the referenced version exists.

**S11. `GET /schemas/{id}/drift-events` always returns `[]` (Medium)**
- The response is empty even for schemas that have several approvals. As a result, the drawer's drift-history block (FE `src/pages/Schemas.tsx:240`) is always empty.
- Code: BE `app/services/schema_service.py:170-171` reads `schema["drift_events"]`, but `_schema_to_dict` never sets that key.

**S12. `POST /schemas/{id}/versions` ignores the `{id}` (Medium)**
- What I did: posted `totally.unrelated.thing v9` to the versions endpoint of `gap-sch.manual.orders`.
- What happened:
  - It created an unrelated schema (`213e95ea`).
  - There is no version chain and no "latest" marker: v1, v2 and v3 are all `active`.
  - A v3 that drops the primary key and changes `id` to string was accepted, even though the compatibility mode is BACKWARD.
- Code: BE `app/api/routes/schemas.py:142-156`.

**S13. The drift rules are wrong or inconsistent (Medium)**
- The schema's compatibility mode is ignored: a schema with `NONE` still returned `approval_required: true`.
- Nullability isn't compared: an approved `name NOT NULL` with a null observed value gave no event.
- Removing a column:
  - The backend says `compatible: true` and needs no approval, while the UI dialog marks it "Breaking" (FE `src/pages/Schemas.tsx:43-44`).
  - With empty records, every field counts as "removed", and the result is still `compatible: true`.
- A new nullable column with no nulls in the sample is reported as `new_required_field` (hold for approval).
- A rename shows up as a removal plus an addition.

**S14. The UI shows some field details wrongly (Low)**
- Nested `fields` and array `items` aren't shown.
- Precision and scale aren't shown.
- A field with no `nullable` value is shown as "no", although the backend treats it as nullable (FE `src/pages/Schemas.tsx:221`).
- Evidence: `schemas/shots/10_view_nested.png`

**S15. Approvals record the org as the reviewer, and creating one isn't audited (Low)**
- The UI sends `reviewed_by: cfg.orgId`, which is stored as "org_1001" instead of the user (FE `src/pages/Schemas.tsx:69`).
- Creating a drift approval writes no audit row. Approve, reject and create schema are audited.

**S16. A Viewer's empty state says "register one", but Viewers can't register (Low)**
- Evidence: `schemas/shots/14_empty_viewer.png`. Code: FE `src/pages/Schemas.tsx:312`.

### Missing features and UX gaps
- **Drift banners and review:**
  - The banner and review dialog don't say which schema the drift is on. `pipeline_id` is always null for drift run from the UI.
  - The review dialog doesn't show the action for each event (quarantine or block).
  - In the view drawer, drift results are shown as raw JSON.
  - The default sample record is always the CRM `account_id` record, whatever the schema.
- **Browsing:**
  - No search, filter, sort, paging or grouping by version.
  - All 211 schemas load in one call (345 KB), in no fixed order (`list_for_org` has no ORDER BY).
- **Managing schemas:**
  - No edit, deprecate or delete: PUT, PATCH and DELETE return 405.
  - There is no versions list in the UI or the API (`GET /{id}/versions` returns 405).
  - No UI for validate-compatibility.
  - No "pipelines using this schema".
- **Information not shown:**
  - No "auto-introspected" badge (`schema_definition.source` is stored but not shown).
  - The original database column type isn't shown.
  - No history of approved or rejected approvals; the "Approvals" card counts every status together (9).
- **Inference and detect-drift:**
  - The Infer drawer can't save its result as a schema.
  - Detect-drift has no record limit (5000 records were accepted); infer is capped at 500.
- An Integrator can approve their own drift. This matches the role matrix, so it's probably intended.

### What works
- Roles:
  - A Viewer gets 403 `read_only_role` on every write and 200 on reads.
  - An Integrator and an Admin can do everything.
  - Unknown roles get 403.
  - A Viewer sees no Infer, Register, Review or drift input.
- Another org's `X-Org-Id` gets 404, or an empty list.
- A duplicate contract returns 409 with a readable message.
- The UI catches field JSON with a missing type.
- Infer:
  - 0 records returns an empty result.
  - More than 500 records returns 413.
  - It infers int, number, bool, date, timestamp, object, array and null-only correctly.
- Introspection status (`created` / `linked_existing`) is recorded.
- Error states render, and the stat cards match the API.

---

## 2. Quality

### Bugs

**Q1. Changes to a running pipeline's quality setup are ignored until it restarts (High)**
- The worker keeps the policy and mode it started with. It only picks up a change after a pause and resume, or after the policy is detached and attached again. Four tests show it:
- **Rule edit:**
  - I changed `gap-q-main`'s `amount_range` max from 1000 to 50, added `name_len` (min 4), and inserted rows 12 (amount 200) and 13 (name "e13").
  - Both passed: stats 13/6, and nothing new in quarantine. The preview, which reads the saved policy, says they fail.
- **Policy swap:**
  - I attached `gap-q-ui-minmax`, a policy every record fails, and inserted row 16.
  - Row 16 passed under the old policy.
- **OBSERVE → ENFORCE:**
  - The PATCH response named `prepared_topic: org_1001-prepared-gapq-pg-items-batch-v1`, but that topic was never created.
  - Any sink set up as the response instructs gets nothing.
- **ENFORCE → OBSERVE:**
  - The API returned `prepared_topic: null`, yet the next good row still went to the prepared topic and reached the sink.
- Code: `QualityStreamManager.reconcile_once` skips pipelines that already have a worker: `if pipeline_id in self._workers: continue` (BE `app/services/quality_stream_worker.py:724-726`). The worker keeps its policy and `_enforcing` flag (`:234, :240-241`). `update_pipeline` only refreshes lineage (BE `app/services/pipeline_service.py:684-725`).

**Q2. Four of the 10 rule types are never evaluated (High)**
- The four types are `timestamp_freshness`, `null_percentage_threshold`, `duplicate_key_batch` and `referential_hint`.
- The API accepts them and Validate says "Policy valid ✓". The page says "Rule types 10", but the form offers only 6.
- Every record passes them. Live, these all passed:
  - a row 30 days old against a 3600 s limit
  - a duplicate key
  - a NULL that the null-% rule should catch

  In preview, dates of `1970-01-01` and 2020 passed the freshness rule.
- Evidence: `quality/shots/60-validate-freshness.png`, `quality/shots/61-execute-freshness-passes.png`
- Code: `evaluate_record` has no branch for these types (BE `app/services/quality.py:76-107`). The count comes from FE `src/pages/Quality.tsx:401` and `src/constants.ts:23`.

**Q3. A type check left on its default "string" never fails (High)**
- What I did: added a type check in the UI form and left the expected type on its default, "string".
- What was saved: `{"name":"qty_type_check","type":"type_check","field":"qty"}`, with no `expected_type`, so nothing ever fails (0 of 2 in preview).
- Evidence: `quality/shots/10-form-typecheck-default.png`
- Code: the select shows the first option, but `buildRulePayload` skips params that were never changed (FE `src/qualityRules.ts:26-34`, `src/pages/Quality.tsx:135`). `_matches_type(value, None)` then returns True.

**Q4. Broken rules are accepted when saved and break only during evaluation (Medium-High)**
- All of these were accepted with 201, and Validate says valid:
  - min > max
  - `min: "5"` given as a string
  - a rule with no name
  - a regex rule with no pattern
  - `allowed_values` given as a string
  - an unknown `expected_type` (`"intgr"`)
  - a rule with no field
  - duplicate rule names
  - an empty values list
  - a policy with zero rules
- What preview does with them:
  - No name, a string min, or no pattern: HTTP 500 `internal_error` (the log shows `KeyError: 'name'`).
  - `allowed_values: "active"` matches as a substring, so `"act"` passes.
  - No field: every record fails.
  - An unknown expected type never fails.
- Live:
  - I attached `gap-q-broken-noname` to a running pipeline. The record that hit the broken rule went only to `…-quality_worker-v1` as `evaluation_error`, with "An unexpected error occurred…".
  - The stats stayed at 26 while the raw topic held 27.
  - Nothing on the Quality tab shows this.
- Code: `_rule_errors` checks only the type name and that a regex compiles (BE `app/services/quality.py:46-68`).

**Q5. Switching back to OBSERVE starves a sink that reads the prepared topic, without warning (Medium)**
- After ENFORCE → OBSERVE plus a restart, a good row went to the raw topic only. `gap-q-sink` stayed RUNNING with 3 rows and no warning.
- The validator only blocks switching *to* ENFORCE (BE `app/services/pipeline_spec_validator.py:242-282`).
- Switching to ENFORCE doesn't backfill either: rows that had already passed never reach the prepared topic or the sink.

**Q6. The failed-records drawer explains old failures with the current policy (Medium)**
- The drawer reads the rules from the stats row's current policy, not from the policy each failure was checked against (FE `src/pages/Quality.tsx:433, :439`).
- After a policy swap, every old failure says "name_required — rule definition not found (policy may have changed since) / actual: —", even though the old policy still exists.
- After an edit, old failures are described with the new bounds, e.g. "between 1 and 50" for a record that was rejected under 1..1000.
- Evidence: `quality/shots/70-drawer-oldest.png`

**Q7. A policy that was ever used can't be deleted, and the error gives the wrong reason (Medium)**
- Deleting an attached policy returns 409 with this message: "This request references data that doesn't exist (foreign key 'dc_pipelines_quality_policy_id_fkey'…)".
  - That is backwards: the policy is still referenced.
  - The message doesn't name the pipeline.
- A policy that has been detached still can't be deleted, because `dc_quality_stream_stats` and `dc_quality_results` still reference it with NO ACTION foreign keys.
- Evidence: `quality/shots/80-delete-*.png`. Code: BE `app/main.py:412`.

**Q8. Form validation is weak and some input is lost silently (Medium)**
- An invalid regex shows only a toast: no input is marked as invalid, although the API returns `field_errors: rules[0].pattern`.
- min `"abc"` or `"1,000"` is saved as `null`, which means no bound. min > max is accepted.
- A rule row with an empty field is dropped silently: 5 rows were entered and the toast said "Policy created (4 rules)".
- Allowed values `"1, 2, 3"` are saved as strings, so they never match integer data and every record fails.
- Empty or whitespace-only policy names are accepted and show as blank rows.
- Evidence: `quality/shots/11-form-invalid-regex.png`, `quality/shots/12-form-minmax-etc.png`. Code: FE `src/qualityRules.ts:26-34`, `src/pages/Quality.tsx:55-64`.

**Q9. The range and required checks let bad values through (Medium)**
- The min/max rule passes `"NaN"`, `true` (counted as 1) and `"50"` (a string).
- The required rule passes `"   "` (only spaces).
- Nulls are handled inconsistently, and this isn't documented: regex, type and min/max checks skip nulls, while allowed_values fails them.
- Code: BE `app/services/quality.py:81-106`.

**Q10. The ingestion breakdown's total and its categories disagree (Low-Medium)**
- The total was 25 while the categories added up to 30, and "unclassified" was clamped to 0. 23 seconds later the total was 20 and the categories still added up to 30.
- Cause: the total uses exact offset deltas by timestamp, while the categories use minute buckets starting from now−3600 s (BE `app/services/monitoring_service.py:312-313, :350-357`; `app/services/failure_counters.py:74`).
- Part of the extra came from another test's sinks (counter rows dated before the pipeline existed). That root cause is not verified.

**Q11. Smaller bugs (Low)**
- A non-UUID policy id returns 500 on GET, validate and delete.
- The 403 message is a Python dict converted to text, and it names a role that doesn't exist ("Data Engineer").
- In the Execute drawer:
  - `"007"` is sent as 7.
  - Whitespace is trimmed to "missing", so `""`, `"   "` or an explicit null can't be tested (FE `src/pages/Quality.tsx:178-180`).
  - The pipeline list defaults to the first pipeline, not one that uses the policy.

### Missing features and UX gaps
- **No edit in the UI.** PUT exists in the API, but rules can only be changed by deleting and recreating the policy, which Q7 often prevents.
- **Headline numbers mix time spans:**
  - "Results logged" is actually the number of pipelines that have stats.
  - "Validation failures (total)" is a lifetime sum, shown next to "Failures — last 1h".
  - No timestamps are shown, so an 8-day-old paused pipeline looks current.
  - Stats are never reset, so totals mix several policies.
- **No per-pipeline filter on the breakdown**, though the API supports `pipeline_id`. Rules with the same name are merged across policies.
- **No replay or reprocessing** of quarantined or worker-DLQ records. The quarantine and worker-DLQ topic names, and evaluation errors, appear nowhere.
- **No list of the pipelines that use a policy, or their mode**, and no warning that changes need a restart (Q1).
- **The create-time autorun results are hidden**, along with their "skipped" reason. They are stored in `/quality-results`, but the tab never shows them, although the pipeline form says "results show in the Quality tab".
- **Batch jobs aren't in the UI:** execute-jobs and resume/resubmit are missing. "Execute" is only a preview and is never saved.
- **A Viewer sees full raw records in the drawer**; only secret-like keys are hidden. It's unclear whether that's intended.
- **Data arrives in a different shape than the database type suggests.** JDBC timestamps reach Kafka as epoch-millisecond integers, and NUMERIC as floats (10.0), so rules written for the database types behave unexpectedly.

### What works
- Policy create, list and view. An invalid regex is rejected with `field_errors`.
- The drop/block on-failure options and the object-storage sink are refused, and shown disabled in the form.
- The 6 implemented rule types evaluate correctly on clear pass/fail data, including a record that breaks 5 rules.
- **OBSERVE (fresh worker):** the raw topic has every record. Quarantine holds exactly the failures, with topic, partition, offset and policy id. Stats and the breakdown match the topics.
- **ENFORCE (fresh worker):** each record goes to either the prepared topic or quarantine, never both, and the sink received only the passing rows.
- A sink on an ENFORCE source's raw topic is refused with a clear message.
- Detaching an ENFORCE policy without switching back to OBSERVE is refused (422). Detaching stops the worker.
- Records produced while a policy was detached are evaluated after it's re-attached.
- The 1-hour window drops old buckets, and the API's per-pipeline filter works.
- The drawer lists the newest first and shows partition, offset, and expected vs actual (when the policy matches).
- A Viewer sees only View, and gets 403 on create and preview. Preview is capped at 200 records (413 above that).

---

## 3. Lineage

What the tab has:
- a pipeline picker
- a graph for each pipeline, with "upstream"/"downstream" expand buttons and "+N more"
- an edge table and an asset table (4 rows each)
- four cards
- empty and error states

What it doesn't have:
- an org-wide view (`useGetLineageAssetsQuery` exists but is unused)
- search inside the graph
- an asset detail view (clicking a node does nothing)
- a status for the UCS catalog sync

### Bugs

**L1. A deleted pipeline leaves its lineage behind (High)**
- What I did: deployed `gap-lin-del-src` (with a quality policy) and `gap-lin-del-sink`, which reads its topic. Then I deleted the source: 204, the row was gone, and Connect returns 404 for its connector.
- What happened:
  - All 4 of its edges stay, with `pipeline_id = NULL`.
  - Every one of its assets stays, including the processor `gap-lin-policy (quality policy)`, whose qualified name contains the deleted pipeline's id.
  - In the UI, expanding the sink's topic shows the dead connector upstream and the dead processor downstream.
- Before this test, the database already had 27 edges with a NULL `pipeline_id`. They belonged to deleted demo and release-matrix pipelines whose connectors and topics no longer exist.
- Pruning can never remove these, because it counts NULL edges as still in use (BE `app/services/lineage_service.py:375-381`).
- `GET /lineage/pipeline/<deleted id>` returns 200 with an empty graph instead of 404.
- Evidence: `lineage/shots/12-delsink-ghost.png`. Code: `hard_delete` does no lineage cleanup (BE `app/services/pipeline_service.py:356-366`), and the foreign key uses `ondelete="SET NULL"` (BE `app/models/lineage.py:81-83`).

**L2. A sink's lineage never updates after an edit, and can't be redrawn (High)**
- What I did: PATCHed `gap-lin-sink-a2` to read a different topic, then restarted it.
- What happened:
  - Connect and the consumer group now read the new topic.
  - Lineage still shows the old topic feeding the sink, and the new topic shows nothing downstream.
  - The backend itself knows a2 no longer reads the old topic: the ENFORCE check names only the other two sinks.
- Why it can't be redrawn:
  - Lineage is drawn only on deploy (BE `app/services/pipeline_service.py:1268`) and on a quality PATCH (`:717-724`).
  - Deploy is refused once a pipeline has been deployed.
  - Restart and Resume don't redraw.
- Even a redraw wouldn't help, because `_materialize_sink_lineage` never prunes the old edge (BE `app/services/lineage_service.py:196-233`).
- The same applies to source edits (tables, transforms, connection). `docs/LINEAGE.md` says "until they are redeployed", which isn't possible.
- Evidence: `lineage/shots/30-srca-downstream-stale-a2.png`

**L3. Sinks with a quality policy show no quality flow (Medium)**
- `gap-lin-sink-q` (with a policy, OBSERVE, running) is drawn as topic → connector → table only: no processor and no quarantine. The quality worker does check sink inputs.
- Code: the sink path ignores `quality_policy_id` (BE `app/services/lineage_service.py:196-237`).

**L4. A sink deploy wipes the shared topic's metadata (Medium)**
- After the sinks were deployed, the topic assets' `metadata` became `{}`. This happened even to a prepared topic that had `{"zone":"prepared"}`, so the last pipeline deployed decides the value.
- Pruning decides what is quality-only by that `zone` (`:371-372`), so a prepared topic loses its classification.
- Code: BE `app/services/lineage_service.py:219-222` writes `metadata={}`, and get-or-create always overwrites name, type and metadata (`:438-440`).

**L5. `POST /lineage/events` can overwrite any asset and has no validation (Medium)**
- An Integrator event rewrote a topic asset into `source | gap-lin-HIJACKED | {"zone":"evil"}`. It was restored afterwards.
- Bad input:
  - A missing `qualified_name` returns 500.
  - A non-UUID `pipeline_id` returns 500.
  - An unknown pipeline returns 409 (a foreign-key error).
- A Viewer is correctly refused (403).
- `LineageEventConsumer` is not wired to anything, so no runtime lineage ever arrives from Kafka.
- Code: BE `app/services/lineage_service.py:384-423`, `app/schemas/lineage.py:37-47`.

**L6. The external catalog (UCS) gets stale or incomplete lineage (Medium)**
- What UCS showed:
  - It still has the deleted pipeline's edge.
  - It still shows a2's old input.
  - For an ENFORCE source, no raw → prepared edge is sent, so the prepared topic has nothing upstream.
  - For a multi-table source, only the first table is sent.
- Cause: events go out only on deploy (BE `app/services/ucs_lineage_sync.py:161`). Nothing is sent on a quality PATCH, an edit or a delete.
- `topics` is sent as the raw string (`:217, :230`), so a comma-separated list would become one dataset named `a,b`. This is from reading the code, not tested.
- Sync is enabled (`DATACONNECT_UCS_LINEAGE_SYNC_ENABLED=true`), but the tab shows no sync status.

**L7. A failed "expand" is swallowed (Low-Medium)**
- With a simulated 500 on `/lineage/{id}`, nothing appears on screen and the page throws an uncaught `pageerror: Object`.
- Evidence: `lineage/shots/22-expand-500.png`. Code: try/finally with no catch (FE `src/pages/Lineage.tsx:95-110`).

**L8. The graph's error state is misleading (Low)**
- With a simulated 500 on the graph:
  - The page shows plain "Internal Server Error" instead of the backend's message.
  - The cards show 0.
  - The asset table says "No assets for this pipeline yet — deploy it to create lineage".
- Evidence: `lineage/shots/21-graph-500.png`. Code: FE `src/pages/Lineage.tsx:114, :224`.

**L9. Labels go stale (Low)**
- A renamed policy keeps its old name on the processor until the next quality PATCH. A deleted pipeline's processor keeps it forever.
- The source asset is named after the connection of whichever pipeline deployed last.
- Pipelines with the same domain, system and entity share one source node (the three tf-cdc pipelines share `org_1001:source:tf:pg:people_cdc`).
- Code: BE `app/services/lineage_service.py:239-252`.

**L11. The pipeline picker's list was cut off by the "Pipeline flow" card (Medium)** — **FIXED 2026-10-16** (found by the user after the audit)
- What happened: the card has `overflow:hidden`, so with a short empty-state card only about two names showed, half hidden.
- The fix:
  - The list is now positioned against the window, so nothing around it can clip it.
  - It opens upward when there is no room below, shrinks to the available space, and follows the button on scroll or resize.
- Change: FE `src/components/SearchableSelect.tsx`, `src/styles.css`. Tests: `tests/e2e/searchable-select.spec.ts` (2 new).

**L10. Non-UUID ids return 500 (Low)**
- `GET /lineage/not-a-uuid` and `GET /lineage/pipeline/not-a-uuid` both return 500.

### Missing features and UX gaps
- **The graph draws columns, not edges (Medium).** One arrow between columns suggests that every node feeds every node in the next column. It looks as if the processor feeds the sink, and you can't tell whether a sink connector reads the quarantine topic or the prepared one (`lineage/shots/13-srcb-full-chain.png`).
- **Names are cut to 26 characters with no tooltip (Medium).** Four connectors all read `org_1001--gaplin--pg--item`, quarantine and prepared topics look alike, and every tf topic reads `org_1001-raw-tf-pg-people_`. Code: FE `src/pages/Lineage.tsx:138`; no element has a `title`.
- **Source nodes show the connection name, not the table.** All four test sources read "gap-lin-src-conn".
- **No runtime status, and no check against what really exists (Medium).**
  - Lineage is written while a pipeline is still "deploying" and never checked afterwards.
  - A failed pipeline whose topic doesn't exist in Kafka still shows connector → topic (`lineage/shots/32-failed-pipeline.png`).
  - Quarantine topics are drawn before they exist; they're only created on the first failure.
  - The pipeline picker has no status badges.
- **Multi-table sources aren't represented (Medium).**
  - With entity `multi`, the deploy was refused ("0 columns from public.multi").
  - With entity `t_one` and tables t_one and t_two, Connect reads both tables into one topic, but lineage, in DataConnect and in UCS, shows only t_one.
- **Transforms aren't shown (Low-Medium).** `transform_policy_id` exists only in edge metadata.
- **A sink's dead-letter topic isn't drawn (Low).** It exists in Kafka.
- **Seeing source → sink needs manual expanding (Low).** A source's graph never shows its sinks. Expansions are lost when you switch pipelines, and expanding a node with nothing more to show gives no feedback.
- **The cards don't add up (Low).** Connectors are counted in no card (Total 3 = Sources 0 + Topics 1 + Sinks/Proc 1), and "Sinks/Proc" mixes two kinds of node.
- **Many assets make the page unusable (Low-Medium).**
  - With 100 consumers, one column is 8116 px tall.
  - The pipeline's own nodes and the "+250 more" button sit about 4000 px down, and the tables below 8000 px (`lineage/shots/23-many-assets.png`).
  - A 7-column chain overflows sideways at 1440 px with no visible scroll cue.
- **The page opens on the first pipeline**, which may be a draft and so shows the empty state. Asset order is whatever the database returns.

### What works
- Without a policy, the graph is exactly source → connector → raw topic.
- Policy states:
  - OBSERVE adds the processor and the quarantine topic.
  - ENFORCE adds `EMITS_TO` → prepared topic.
  - Going from ENFORCE back to OBSERVE removes them.
  - A log-only policy removes the quarantine topic.
  - Detaching the policy removes the processor.
  - API, database and UI agree on all of these.
- A sink on the prepared topic links to the same topic node. Two sinks on one topic share the node, and a sink on an unknown topic has no upstream.
- The backend refuses a sink on an ENFORCE source's raw topic, and refuses ENFORCE while raw-topic sinks exist.
- A draft shows the "deploy it" empty state. Renaming a pipeline doesn't affect lineage.
- The 4-row tables:
  - They are 200 px tall with an inner scroll, a hidden scrollbar and a sticky header.
  - The title and cards stay put.
  - There's no horizontal page scroll at 390 px.
- A Viewer can read and gets 403 on events. Another org's asset id returns an empty graph, with no data leak.

---

## 4. Transforms

What the tab has:
- a list of policies, with View, Validate, Preview and Delete on each row
- "New policy", which is a raw JSON text box

What it doesn't have:
- edit
- search
- a step builder (add, reorder or remove steps)
- a "used by" column
- version history

Editing is possible only through `PUT /transform-policies/{id}` in the API.

### Bugs

**T1. Validate and the deploy check pass settings that Kafka Connect rejects (High)**
- I deployed each of these cases as a JDBC pipeline. Validate showed "Valid ✓" and `deploy-readiness` returned `ready: true`. What happened:

| Case | Result |
|---|---|
| Two steps named `mask` | Connect: "Duplicate alias provided"; the pipeline stuck at `unknown` |
| RegexRouter regex `([` | Connect rejected the `regex` setting; `unknown` |
| TimestampConverter format `yyyy-MM-dd'T` | The task FAILED ("Unterminated quote"); `degraded` |
| InsertField with no settings | The task FAILED ("No field insertion configured"); `failed` |
| Misspelled settings (`exlude`, `delimeter`, `bogus`) | Accepted silently; they do nothing |

- The New pipeline form treats InsertField's `static.field` and `static.value` as required, but the backend's `REQUIRED_CONFIG` leaves InsertField out.
- With duplicate names, the second step silently overwrites the first one's settings: preview showed `transforms: "mask,mask"` with only `fields=email`.
- Kafka Connect's own `PUT /connector-plugins/<class>/config/validate` catches the duplicate-name and regex cases, but nothing calls it.
- Evidence: `transforms/shots/04_validate_*.png`, `transforms/t8_out.txt`. Code: BE `app/services/transform_policy.py:44-53` (REQUIRED_CONFIG), `:78-89` (no duplicate-name check), `:101-121` (generate_smt_config).

**T2. The platform's own step names aren't reserved (High)**
- A user step named `reroute` is merged with the platform's own `reroute` step. The result is `transforms: "reroute,reroute"`, with the type overwritten to RegexRouter, so the user's MaskField never runs.
- Connect rejected the config ("Duplicate alias") and the pipeline is stuck at `unknown`, yet Validate and readiness both passed.
- The same collision applies to `dckey`, `dckeyx` and `dckeyc` when a pipeline sets `key_field`. This is from the code, not tested live.
- Code: BE `app/services/connect_config.py:137-161` (`_reroute`, `_combine`), `app/services/connect_config_t1.py:114-140`.

**T3. Preview doesn't transform the record (High)**
- `POST /preview` returns the input record unchanged: `ssn` and `email` were not masked.
- The UI ignores the typed record and shows only the policy's config fragment, without the platform's `reroute` step.
- Evidence: `transforms/t2_out.txt`, `transforms/shots/03_preview_live.png`. Code: BE `app/api/routes/transforms.py:126-130`, FE `src/pages/Transforms.tsx:110-136`.

**T4. Editing a policy changes running connectors without telling anyone, and the stored config goes stale (Medium)**
- The change does reach Connect: a PUT reached the running connector's config in about 15 s, and new rows came out in the new shape.
- What's missing:
  - The PUT response doesn't list the affected pipelines.
  - The pipeline's `generated_config` and `GET /pipelines/{id}/generated-config` still showed `static.value: v1` and `fields: ssn` while Connect ran `v2` and `ssn,email`.
  - The topic now mixes record shapes with nothing marking the change: ids 1–2 have the email in clear, id 3 has it masked.
- Evidence: `transforms/t5_out.txt`. Code: BE `app/services/transform_service.py:63-91` bumps the reconcile generation but doesn't refresh the stored config.

**T5. Deleting a policy that's in use gives the wrong reason (Medium)**
- The delete is correctly blocked (409, by a foreign key), but the message and the UI toast say "This request references data that doesn't exist…", which is the opposite of what happened.
- The message doesn't name the pipeline, and the confirm dialog only asks `Delete "gap-tf-live"?`.
- Evidence: `transforms/t6_delete_inuse.txt`, `transforms/shots/05_delete_inuse.png`. Code: BE `app/api/routes/transforms.py:76-92`, `app/services/transform_service.py:93-100`.

**T6. `PUT` can change a policy's id (Medium)**
- `PUT` with `{"id": <new uuid>}` changed the primary key: the old id now returns 404 and the new one 200. On a policy in use, the same PUT gets the misleading foreign-key 409.
- `POST` with a non-UUID `id` returns 500. `POST` with an existing id returns a raw constraint 409.
- Evidence: `transforms/t3_out.txt`. Code: `TransformPolicyCreate` accepts `id` (BE `app/schemas/transform.py:20-21`), and `update_policy` passes it straight through.

**T7. Auto-created policies are never cleaned up (Medium)**
- Deleting a pipeline created from the New pipeline form leaves its `<name>-transforms` policy behind.
- The form creates the policy before the pipeline, so a failed create (409, duplicate topic) still leaves an orphan policy with the same name.
- Code: FE `src/pages/Pipelines.tsx:403-421`. BE `pipeline_service.delete_pipeline` and `hard_delete` (around `:727`, `:355`) never touch the policy.

**T8. A config that Connect rejects shows the pipeline as "unknown" instead of "failed" (Medium)**
- Pipelines v2 and v7 sit at `unknown` (desired: deployed).
- Their `runtime_error` has a short reason ("Settings it objected to: transforms") but doesn't say "duplicate alias".
- Probably the `observed_generation == 0` branch in BE `app/services/runtime_reconciler.py` (around `:858`). Not traced further.

**T9. The view drawer shows steps in saved order, not the order they run (Medium)**
- A policy saved with orders 3, 1, 2 is shown as third, first, second, while the generated config runs first, second, third.
- Two steps with the same `order` are accepted without a warning.
- Evidence: `transforms/shots/02_view_order.png`. Code: FE `src/pages/Transforms.tsx:99`.

**T10. `header` scope is accepted but can't work (Low)**
- `scope: "header"` validates fine, but it produces `MaskField$Header`, a class that doesn't exist. Only the readiness check catches it ("not installed on the worker").
- `scope: "key"` on types without a key/value variant (ValueToKey, RegexRouter) is stored but has no effect.
- Code: BE `app/services/transform_policy.py:87-89, :111-112`.

**T11. Policy names aren't validated (Low)**
- `""` and `"   "` are accepted, and so are duplicate names.
- A 300-character name returns 500 (the column is varchar(255)).
- `GET /transform-policies/not-a-uuid` returns 500 instead of 404.

### Issues from the earlier transformation test (still open)
| # | Issue | Status |
|---|---|---|
| 1 | HeaderFrom can't deploy: the backend generates the abstract `…HeaderFrom` class instead of `HeaderFrom$Value` (it's missing from `SCOPED_TRANSFORMS`) | Still reproduces |
| 2 | The New pipeline form sends Debezium's ExtractNewRecordState last, so field transforms run on the CDC envelope and every record is dropped | Still reproduces (code unchanged; not re-run live) |
| 3 | A user RegexRouter has no effect, because the platform's reroute is added after it | Still reproduces (no `_routed` topic exists) |

### Fixes made earlier today: rechecked
| Fix | Status |
|---|---|
| TimestampConverter Format pre-filled and required for a string target; required settings enforced on POST/PUT (422) | Holds |
| Deploy preflight check `smt_settings_valid` | Unverified live: an old-style policy couldn't be created to trigger it |
| `time.precision.mode=connect` on CDC with TimestampConverter | Holds |
| A failed task's reason saved as the pipeline's runtime error | Holds |

### Missing features and UX gaps
- **A saved policy can't be used from the UI.**
  - The New pipeline form always creates a new `<name>-transforms` policy.
  - The pipeline edit form can't change the policy (FE `src/pages/Pipelines.tsx:602-607`).
  - Policies made on this tab can only be attached through the API.
- **No edit in the UI.** PUT is API-only, so there's no place to warn about editing a policy that's in use.
- **Missing list features:**
  - No search or filter.
  - No "used by pipelines" column.
  - No version history or rollback.
  - No conflict check on PUT, so two editors can overwrite each other.
  - One stat card is a placeholder ("—").
- **The editor is a raw JSON text box:**
  - It has no help on each type's settings.
  - The default text uses `customer_id`.
  - The default description "key + mask PII" stays when only the name changes.
- **Transform types offered:**
  - The tab lists 10 types; the backend also accepts ExtractNewRecordState.
  - Many installed transforms aren't offered (Cast, Filter, InsertHeader, DropHeaders, TimestampRouter, SetSchemaMetadata): "Unsupported SMT type: Cast".
- **Validate shows only a toast**, without pointing at the step that failed.

### What works
- List, create and delete for an Admin. A Viewer gets View only, and View works.
- Steps run in the order of their `order` field, both in the generated config and on Connect.
- Key scope works end to end: the key came out as `{"id":1,"email":""}`.
- JDBC output matched the policy: `fname` → `first_name`, `tenant_id` dropped, `ssn` masked, `src` added.
- A policy edit reaches running connectors, and fixing a broken policy healed the failed pipelines back to running.
- Unknown types, bad step names and bad scopes return a clear 422. The readiness check catches the header-scope class.

---

## 5. Suggested order for fixing

1. **Changes that never take effect.**
   - Q1: the quality worker must reload its policy and mode on change.
   - L2: redraw lineage on edits, including the old sink edge.
   - T4: refresh the stored config when a policy is edited.
2. **"Valid" that isn't valid.**
   - Q2: implement or hide the 4 rule types.
   - Q3 and Q4: save type checks with their expected type, and validate rule parameters when they are saved.
   - T1 and T2: run Kafka Connect's own config validation in Validate and in readiness, and reserve the platform's step names.
   - S7: make schema compatibility a real check.
3. **Leftovers after deleting.**
   - L1: remove a deleted pipeline's lineage and prune what it orphans.
   - T7: remove a pipeline's auto-created transform policy.
   - Q7 and T5: give the correct reason when a delete is blocked, and name the pipeline.
4. **Schema drift.**
   - S2: detect drift automatically.
   - S3: approval must create the new version.
   - S4 and S5: no false "breaking" results, and correct type mapping.
   - S1: the banners must not hide the table.
5. **Input validation and the 500s on all four tabs.** S8, S9, Q11, L10, T6, T11.

---

## 6. Test data created by this audit (all kept as proof)

The tabs show this data now. In particular, the 7 pending drift banners currently hide the Schemas table (see S1).

| Tab | Created |
|---|---|
| Schemas | Role `gap_sch_user`; databases `gap_sch_src` (tables alltypes, nopk, evolve — evolve was altered) and `gap_sch_src2`; connections `gap-sch-conn`, `gap-sch-conn2`; pipeline `gap-sch-evolve-v1` (**running**) and drafts `gap-sch-alltypes-v1`, `gap-sch-nopk-v1`, `gap-sch-evolve-relink`, `gap-sch-evolve-v2`; about 165 schemas (`gap-sch.*`, including 150 `gap-sch.page.bulk_*`, plus 3 unprefixed edge cases: `2d449e42`, `4bbd2c82`, `213e95ea`); drift approvals: 7 pending, 2 approved, 1 rejected |
| Quality | Role `gap_q_user`; databases `gap_q_src`, `gap_q_tgt`; connections `gap-q-src-conn`, `gap-q-tgt-conn`; pipelines `gap-q-src` (**running**, policy gap-q-main, OBSERVE) and `gap-q-sink` (**running**, starved, proof for Q5); policies `gap-q-main`, `gap-q-broken-noname`, `gap-q-ui-typecheck-default`, `gap-q-ui-minmax` and 19 `gap-q-val-*`; topics `org_1001-raw/quarantine/prepared-gapq-pg-items-*` |
| Lineage | Role `gap_lin_user`; database `gap_lin`; connections `gap-lin-src-conn`, `gap-lin-tgt-conn`; policies `gap-lin-policy` (renamed `gap-lin-policy-renamed`), `gap-lin-policy-log`; sources `gap-lin-src-a-renamed`, `gap-lin-src-b` (ENFORCE), `gap-lin-multi2` (**running**), drafts `gap-lin-draft`, `gap-lin-multi`; sinks `gap-lin-sink-a`, `-sink-a2`, `-sink-prep`, `-del-sink`, `-sink-orphan`, `-sink-q`; lineage-event assets `gap-lin-evt-topic`, `gap-lin-evt-sink`. **Deleted:** `gap-lin-del-src` (the test for L1) |
| Transforms | Role `gap_tf_user`; database `gap_tf_src`; connection `gap-tf-conn`; pipelines `gap-tf-jdbc-live` (**running**), `gap-tf-jdbc-v2` … `-v8` (v2 and v7 unknown, v3 draft, v6 failed, the rest running); about 20 `gap-tf-*` policies (including 2 orphaned `gap-tf-uipipe-transforms`). **Deleted:** `gap-tf-uipipe` (the test for T7) |

Earlier test data from today is also still there:
- `tf-*-134770` (transformation test, database `tf_src_134770`)
- `fail-*-134251` (failure demo, databases `fail_src_134251` and `fail_tgt_134251`)
