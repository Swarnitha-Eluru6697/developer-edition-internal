# Agent node: Configure panel (v53.2)

v53.1 closed the v53 review (`InaiEra_Agent_Node_V53_Blockers_Bugs_Gaps_Review`). v53.2 closes the follow-up review of v53.1 (3 blockers, 8 highs). Both mappings are at the end. OAuth and UCS integration are out of scope, as in the reviews.

## Data
- `agentskill` / `agentfile` (migrations `b73e1d9c5a40`, `c84f2e0d6b51`). Bytes live in the object store under `agents/{agent_id}/skills|files/{id}`; rows keep metadata, `sha256`, and `retired_at`. Files also carry `status` (`ready` / `stored_only` / `failed`) and `status_detail`.
- `agentnodesecret` (`agent_id`, `node_id`, `key`, `value_enc`, `retired_at`): node ENV values, encrypted with `secrets_crypto`. Unique per (agent, node, key). `agentfile.template_policy` (`require` default / `embed` / `exclude`). Both added by migration `d95a3f1b7c62`.
- Node config keys: `model`, `prompt` (falls back to legacy `instructions`), `skills: [id]`, `files: [id]`, `tools: ["tool:Name:Action"]`, `env: [{key}]` (**names only, never values**), `strategy`, `query`, `max_iterations`, `memory_enabled`.

## Contracts

### ENV values are never in the flow, and the declared keys are the source of truth (B1, B2)
- **One transactional endpoint:** `PUT /api/agents/{id}/nodes/{node_id}/env` with optional `If-Match: <draft_revision>` and `{"entries": [{"key": "CRM_TOKEN", "value": "…"}]}`. The node's key list on the draft and the encrypted values commit in one transaction. A stale `If-Match` is a 409 and **nothing** is written. `entries` is the full list of keys the node declares: an entry with `value` writes or rotates it; one without keeps the stored value (and re-declares a retired key); a stored key left out is detached (see below). Response: `{entries: [{key, configured}], draft_revision, agent}`, never a value. The old per-key `PUT/DELETE .../secrets/{key}` routes are gone, so there is no second call that could fail halfway.
- `GET .../secrets` returns `[{key, configured}]`. Without `SECRETS_ENCRYPTION_KEY` a value write returns 503 and writes nothing.
- **Declared ∩ stored.** At run time `effective_env_map` resolves only keys the node declares in `config.env` (agent-level env first, node value wins on a clash). Publish validation applies the same rule to `{{env.KEY}}` and to `env_value_missing`. A value left behind for an undeclared key is inert: it can't resolve and can't make validation pass. The same map is built in `_execute_flow`, loop children and the Temporal node activity.
- **Secrets are retired, not deleted, while a published version declares them.** Removing a key, or deleting the node, from the draft: if any `AgentVersion` flow still declares that (node, key), the value is **retired** (`retired_at` set, value kept, still resolves for that version); otherwise it is deleted. This is staged in the same transaction as the draft change. Re-declaring a retired key un-retires it with its value. Restoring a version un-retires the secrets and assets its flow uses. Rotation stays live: a rotated value takes effect for the published version without a republish (by design).
- Scrubbing at both write paths (`_atomic_save_agent` raw SQL and ORM listeners on `Agent` / `AgentVersion`) still drops any `env[].value` that reaches a flow.
- Duplicating an agent does not copy secret values.

### Assets are immutable once a version references them (B3, B4, B15)
- **Remove in the panel only detaches** the asset from the node (a config change). The agent's library keeps it.
- Delete is a separate call: `DELETE .../skills/{id}` or `.../files/{id}`.
  - 409 while any draft node uses it.
  - If a published `AgentVersion` references it, it is **retired** (`retired_at` set, hidden from lists, bytes kept) so old versions still run with the exact bytes they were published with. Response: `{ok, outcome: "retired"}`.
  - Otherwise it is deleted and the blob removed after commit. Response: `{ok, outcome: "deleted"}`.
- `sha256` is recorded at upload. Retired names can be reused (partial unique index on live rows).
- Uploads write the blob, then commit; if the commit fails the blob is removed.
- **Restore:** restoring a version un-retires the assets and secrets its flow references. The skill and file lists also include a retired asset that the current draft points at (marked `retired: true`, shown as "Restored from version"), so the panel always shows what a node uses.

### Cleanup and storage (High 6, 7)
- **Scheduled job.** `start_asset_reconciler()` runs at startup (`ASSET_RECONCILE_INTERVAL_SECONDS`, default 3600; 0 disables it for CronJob deployments, which run `python -m app.agent_assets reconcile`). A Postgres advisory lock lets one replica run at a time; a failure on one agent is logged and the rest continue. Each pass, per agent: (1) removes live skills/files that no draft node and no version references and that are older than `ASSET_ORPHAN_GRACE_HOURS` (default 72, by upload time, so a library item that was uploaded long ago and detached is collected too); (2) removes retired assets no version pins any more; (3) removes secrets that neither the draft declares nor any version references.
- **Grace floor.** `POST /api/agents/{id}/assets/reconcile?older_than_hours=N` returns 422 for N below 1, and the job never uses less, so a fresh upload that is about to be attached can't be collected.
- **Storage policy.** `GET .../files/limits` reports `usage: {live_bytes, historical_bytes, total_bytes}`. Live assets are capped as before (1 GB, 50 skills, 100 files). Retired-but-pinned history has its own cap, `AGENT_ASSET_MAX_HISTORICAL_BYTES` (default 5 GB): over it, an upload first reclaims retired assets nothing pins, and if the history is still over the cap it returns 413 asking an administrator to prune old versions. There is no user-facing version delete in the product yet, so pruning is an operator action.

### Agent delete and duplicate (B5, 17)
- Deleting an agent deletes its skills, files and secrets, then removes the blobs after commit.
- Duplicate copies assets inside the transaction. If any blob copy fails the duplicate is rolled back and the call returns 502; no half-copied agent is left behind.

### Upload safety
- `verify_content` checks magic bytes against the claimed type (executables rejected; DOCX must contain `word/document.xml`; text must be UTF-8).
- PDF and DOCX text is extracted in a separate subprocess (`app/extract_worker.py`) with `-I`, an empty environment, `cwd=/`, CPU/memory rlimits and a timeout. A parser crash or hang cannot take down the API.
- `SKILL.md` is capped at 256 KB (checked before it is read).
- Readable documents: text types, PDF, DOCX. `.doc .xls .xlsx .ppt .pptx .epub .rtf` return 415 with a "save as PDF/DOCX/TXT/CSV/MD" message. Images, audio and video are accepted but marked `stored_only`: kept, not sent to the model.
- Quotas per agent: 50 skills, 100 files, 1 GB total; 4 concurrent uploads (429 when busy). Limits: documents 15, images 10, audio 50, video 100 MB; skills 50 MB.

## Runtime (`harness.handle_agent`)
1. `prompt` (or `instructions`) is interpolated: `{{env.KEY}}` then `{{var}}`.
2. **Skills** go in the system prompt. **All attached skills are included on every run** (no lazy loading; the UI says so). Budgets: 8,000 characters per skill, 24,000 in total.
3. **Reference files** go in the **user turn** inside a `<reference_files>` block, each fenced and labelled as untrusted data. The system prompt carries only a short note on how to treat that block. File content therefore cannot sit at system-instruction level (#20).
4. A missing or foreign asset id fails the run with a clear error instead of being silently skipped. At most 20 assets per node.
5. The existing strategies run unchanged. There is **no command sandbox**; only `SKILL.md` text reaches the model.

## Publish validation (`flow_validator._check_agent_node`)
Errors block publish; warnings do not.
- `unknown_agent_asset`: a skill/file id that does not exist for this agent.
- `unknown_tool`, `tool_not_callable`: a selected tool that is not in the catalog or not callable.
- `tools_inactive` (warning): tools selected but strategy is not `function_calling`.
- `unknown_model`, `model_not_chat`, `model_no_tool_calling`: model not in the catalog, not a chat model, or lacking tool calling while tools are in use.
- `env_value_missing` (warning): a declared key with no stored value.
- `file_unreadable` (error): an attached file whose text extraction failed. `file_not_sent` (warning): an attached image/audio/video that is stored but not sent to the model.
- `template_file_required` (error): a template `files_required` entry (required) that hasn't been supplied.
- `MODEL_PROVIDER_NOT_CONFIGURED` (error): the model's provider is wired up but this deployment has no usable credential for it (environment key or a key stored in Integrations; mock mode counts only when `ALLOW_MOCK_MODELS` is on, as at run time).

## Catalogs
- `GET /api/integrations/agent-tool-catalog` → `{tools: [{value, label, group, source_type, callable, reason}]}`. MCP and workflow entries are listed with `callable: false` and a reason; the picker shows them disabled.
- `GET /api/integrations/llm-models` now includes `supports_chat`, `supports_tool_calling`, `supports_vision` (`app/model_capabilities.py`; Replicate models have no tool calling).

## Templates (#21, v53.2)
- **Capture fails closed.** Saving a template embeds the agent's skills (always) and its files by each file's **template setting**. If any node references a skill or file the agent doesn't have, the save is refused with `422 TEMPLATE_ASSET_MISSING` naming the node and the asset id; nothing is quietly dropped. A file set to `embed` that can't be embedded (unreadable, over 256 KB of text, or over the 2 MB template total) is refused with `422 TEMPLATE_FILE_NOT_EMBEDDABLE`.
- **File template setting** (`PATCH /api/agents/{id}/files/{file_id}` `{template_policy}`): `require` (default: nothing from the file travels; the installer uploads their own), `embed` (the text is copied into the template), `exclude`. Skills are embedded because they are part of the agent's behaviour. ENV keys travel; values never do.
- **Requirements.** A `require` file becomes `files_required: [{requirement_id, name, filename, kind, accepted_types, required}]` on the node. After install, `POST /api/agents/{id}/nodes/{node_id}/requirements/{requirement_id}/bind` `{file_id}` (optional `If-Match`) attaches a file from the agent's library and clears the requirement in one draft update; the file must be the right kind and an accepted type, and readable. Publish is blocked (`template_file_required`) while a required entry is unresolved. Requirements from an imported package are normalised (stable ids, bounded).
- **Instantiate is all-or-nothing on the object store.** `materialize_embedded_assets` removes every blob it already wrote if it fails part-way, and the create-agent path removes them if the commit after it fails. An embedded skill with an invalid name fails the instantiate (`TEMPLATE_SKILL_INVALID`) rather than being skipped.
- Duplicate-agent has the same guarantee: a commit failure after the copies were written removes them.
- The requirements panel shows what the template embeds (`requirements.assets`, only present when non-empty, so older packages keep their content hash).

## Migration `d95a3f1b7c62`
Adds `agentnodesecret.retired_at` and `agentfile.template_policy` (default `require`, so no existing file starts embedding its contents). No data step.

## Migration `c84f2e0d6b51`
Adds the asset columns and the `agentnodesecret` table, then moves any plaintext env values found in `agent.flow` / `agentversion.flow` into encrypted secrets and strips them from the flow. **If legacy values exist, `SECRETS_ENCRYPTION_KEY` must be set when you run it**; the migration refuses otherwise rather than lose or leak them. Rotate any credential that was ever stored in plaintext.

## Known limits
- MCP servers, workflows and Swagger-defined tools appear in the picker but are not callable from the Agent node yet.
- Images, audio and video are stored, not sent to the model.
- ENV values can still show up in run traces if the model echoes them.
- Template `files_required` must be supplied after install (the panel binds them; publish waits for them).
- Unattached library assets older than the orphan grace (72 h by default) are collected by the scheduled job, measured from upload time.
- Old versions can't be deleted from the product, so the historical-storage cap is relieved by an operator pruning versions.
- Reference-file extraction is separate from the Doc Extractor node.

## First review of v53 → fix (v53.1)
| # | Fix |
|---|---|
| B1 | `agentnodesecret`, encrypted; flow keeps key names only; scrub on both write paths; migration moves legacy values |
| B2 | Validator accepts a node's own secret keys |
| B3 | Delete retires assets referenced by a version; `sha256` recorded |
| B4 | Remove = detach; Delete is separate and 409s while a draft node uses it |
| B5 | Agent delete removes skills, files, secrets, then blobs |
| 6 | `verify_content` magic-byte checks |
| 7 | PDF/DOCX parsed in a limited subprocess |
| 8 | Narrowed formats, per-file `status` |
| 9 | "All attached skills are included" in UI and docs |
| 10 | `SKILL.md` 256 KB cap |
| 11, 12 | Backend tool catalog with `callable` / `reason` |
| 13, 14 | Model capabilities and DB-aware publish validation |
| 15 | Assets are library objects; reconcile endpoint; blob removed on failed commit |
| 16 | Quotas and upload concurrency limit |
| 17 | Duplicate aborts with 502 on blob failure |
| 18 | Inactive-tools warning and UI banner |
| 19 | "Agent-created files 50 MB" claim removed |
| 20 | Files in the user turn, fenced and untrusted |
| 21 | Templates embed skills and text files, list required files |

## Follow-up review of v53.1 → fix (v53.2)
| Finding | Fix |
|---|---|
| Blocker 1: draft edit breaks the published agent (secrets deleted) | `retired_at` on secrets; delete-node and key removal retire while a version declares the key; un-retire on re-declare and on restore; purge only when unreferenced |
| Blocker 2: secret storage and flow diverge | One transactional `PUT .../env` with `If-Match`; runtime and validator use declared ∩ stored |
| Blocker 3: template capture drops assets on one stale id | `422 TEMPLATE_ASSET_MISSING` naming node and asset; nothing dropped |
| High 1: required files not resolved | Stable `requirement_id`, bind endpoint, publish blocked until resolved, UI upload per requirement |
| High 2: instantiate leaks blobs | materialize cleans up on failure; create-agent and duplicate clean up on a failed commit |
| High 3: files embedded without consent | Per-file template setting, default `require`; embed is opt-in |
| High 4: restored version's assets vanish from the panel | Restore un-retires; lists include retired assets the draft points at, labelled |
| High 5: model availability not validated | `MODEL_PROVIDER_NOT_CONFIGURED` from the provider's real credential state |
| High 6: historical assets bypass quota | Separate historical cap, usage reported, reclaim-then-refuse |
| High 7: reconciler not scheduled | Hourly in-process job with advisory lock (or CronJob command); 1 h grace floor |
| High 8: failed files can be published | `file_unreadable` error, `file_not_sent` warning |
