# Templates: draft, publish, and distribution

A template is a reusable Agent definition with an explicit authoring boundary:

```text
Save / edit draft -> Publish -> immutable TemplateVersion -> Gallery / install
                         ^
                         | edit again -> unpublished draft changes -> republish
```

Published versions never change in place. A person can keep editing the draft without changing what the gallery or a newly created Agent receives.

## Origins shown in Studio

The database still uses `source=platform|private`, but Studio distinguishes the actual origin so locally authored templates are not confused with templates shipped by InaiEra.

| Studio label | Stored as | Managed by | Distribution |
|---|---|---|---|
| **Platform provided** | `source=platform`, `release_managed=true` | release packages | shipped to data planes with the product |
| **Organization-managed** | `source=platform`, `release_managed=false` | platform admin in Studio | this data plane only |
| **Private** | `source=private`, `release_managed=false` | owner (platform admin may administer) | this data plane only |

Release-managed Platform templates are read-only in Studio. Organization-managed and Private templates use the same Draft -> Publish lifecycle.

## Data model

`Template` is the mutable authoring record. Important fields:

- `id`: stable resource identity; API routes use this, never the display name.
- `flow`, metadata: current mutable draft.
- `status`: `draft`, `published`, or `archived`.
- `draft_revision`: optimistic-lock revision.
- `current_version_id`: immutable version currently exposed in the gallery.
- `published_revision`: draft revision represented by `current_version_id`.
- `release_managed`: true only for product-release packages.
- `app_type`: preserved so Workflow/Chatflow/Agent behavior survives template round-trips.

`TemplateVersion` is immutable and stores the exact published snapshot, including `flow`, `app_type`, metadata, requirements, semantic version, and publisher.

Agents created from templates record stable provenance:

- `template_name`: display snapshot,
- `template_id`: stable Template id,
- `template_version_id`: exact immutable version installed.

## Name rules

Active template names are unique case-insensitively. `_` and `%` are treated as literal characters, not SQL wildcards. Archived templates do **not** reserve their former names, so a name can be reused after archive without deleting history.

## Gallery and management APIs

`GET /api/templates` returns only installable published snapshots. Draft edits are invisible until republished.

`GET /api/templates/manage` returns mutable authoring records.

`GET /api/templates/facets` counts the same published-gallery population as `GET /api/templates` and reports both persisted `source` totals and user-facing `origin` totals.

`GET /api/templates/{id}/versions` returns immutable publication history.

## Save and publish

`POST /api/templates` captures an Agent as a draft. Body includes:

```json
{
  "agent_id": "a123",
  "name": "Lead Scoring",
  "source": "private",
  "desc": "...",
  "category": "Sales",
  "vertical": "Banking",
  "tags": ["lead"]
}
```

For an Organization-managed template a platform administrator sends `source=platform`. The UI labels this **Organization-managed**, not Platform provided, because it is local to one data plane.

Saving sanitizes literal credentials, recomputes requirements, and returns validator findings. An incomplete draft is allowed.

`PUT /api/templates/{id}` updates the draft. It requires `If-Match` with the current `draft_revision`. The header accepts `7`, `"7"`, or `W/"7"`. An identical save is a no-op and does not increment the revision.

`POST /api/templates/{id}/publish` also requires `If-Match`. Publish runs strict flow validation. If the draft is valid it creates a new immutable `TemplateVersion`; if the same revision is already published the call is idempotent and returns that version instead of manufacturing another patch.

`DELETE /api/templates/{id}` archives rather than destroys history.

## Save & publish UX

The Save as Template dialog exposes two actions:

- **Save draft** — keep work private to Manage templates.
- **Save & publish** — save first, then immediately publish the returned draft revision.

This keeps the explicit two-phase model while retaining the one-step path for finished templates.

## Install, duplicate, export, and import

`POST /api/templates/{id}/instantiate` always installs `current_version_id`, never mutable draft state.

`POST /api/templates/{id}/duplicate` can copy `state=published|draft` and always creates a new Private draft.

`GET /api/templates/{id}/export?state=published|draft` exports the requested state.

`POST /api/templates/import` sanitizes and recomputes requirements, then creates a **Private draft**. Import cannot make itself Platform provided or Organization-managed.

`POST /api/agents` accepts `template_id`. Unknown request fields are rejected, so obsolete `template_name` requests fail with 422 rather than silently creating a blank Agent.

## Credential sanitization and requirements

Literal values under credential-like config keys (`api_key`, `token`, `password`, `authorization`, `secret`, and similar) are blanked when captured/exported. `{{env.KEY}}` references are preserved.

Agent nodes get extra treatment. Their `skills` and `files` ids belong to the source agent, so the ids are removed, but the skills and text files themselves are embedded in the package (`skills_embedded`, `files_embedded`) and re-created for the new agent on install. Each file's *In templates* setting decides what happens: `require` (the default) lists it in `files_required` with a stable `requirement_id` for the installer to upload, `embed` copies its text into the package, `exclude` leaves it out. Saving fails with `TEMPLATE_ASSET_MISSING` if a node references an asset that no longer exists. Publishing the installed agent is blocked until each required file is supplied. ENV values never travel: only the key names do, so the installer sees what to fill in. Each removal is listed in the save response's redactions.

Requirements are always derived from the flow and include node types, knowledge bases, catalog objects, tools, models, environment variables, and a review list for Code/HTTP nodes.

## Release-managed Platform packages

Release-managed templates live in `app/template_packages/*.json`. `seed.sync_platform_templates` manages **only** `source=platform AND release_managed=true`; it never modifies Private or Organization-managed rows.

The seeder is availability-safe:

- a package older than the database version (for example during application rollback) is logged and skipped;
- changed content at the same semantic version is logged and skipped;
- neither condition crashes application startup;
- a removed package archives/hides its release-managed row rather than deleting history.

If an incoming release package collides with an active local template name, the local template is renamed. If it already had a published version, the system creates a patch snapshot carrying the renamed published metadata so the gallery remains name-unique without exposing unrelated draft edits.

## Package promotion and CI

Use:

```bash
python -m app.promote_template ~/Downloads/lead-scoring.template.json \
  --key lead-scoring --category Sales --vertical Banking --version 1.0.0
```

Promotion:

1. parses and sanitizes package semantics,
2. runs the same publish-level `validate_flow` checks used by Studio,
3. refuses the same or a lower version for an existing/previously shipped key,
4. validates the complete package directory,
5. updates `app/template_packages.lock.json` with version + canonical SHA-256.

CI must run:

```bash
python -m app.promote_template --check
```

The check validates every shipped flow and verifies the immutable package lock. Manual content edits without a version bump therefore fail CI instead of taking down data planes at startup.

## Current first-party package notes

`blank-canvas` intentionally has no fixed `app_type`, so the New Agent dialog's chosen app type is preserved and the intentionally minimal canvas is not rejected as an incomplete Workflow.

`task-automation` is a valid Workflow package: its HITL node has an explicit delivery method and its terminal node is a structured `output` node.

## Out of scope

Marketplace contributions, package signing, a Control Plane registry, release stages, and in-product upgrade of an already-instantiated Agent remain outside this template lifecycle.
