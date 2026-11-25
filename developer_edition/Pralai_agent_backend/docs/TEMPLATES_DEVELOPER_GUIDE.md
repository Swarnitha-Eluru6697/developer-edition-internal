# Templates: Developer Guide (v52.1)

Audience: engineers working on the Agent platform backend/frontend and InaiEra team members who author or ship templates. `docs/TEMPLATES.md` is the shorter product/contract reference.

## 1. Mental model

There are three user-visible origins built on two persisted `source` values:

| Origin | `source` | `release_managed` | Editable in Studio | Distribution |
|---|---|---:|---|---|
| Platform provided | `platform` | true | no | application release |
| Organization-managed | `platform` | false | platform admin | current data plane |
| Private | `private` | false | owner / platform admin | current data plane |

A mutable `Template` is never itself the published artifact. Publish creates an immutable `TemplateVersion` and points `Template.current_version_id` at it.

```text
Template draft r7
   | publish
   v
TemplateVersion 1.0.0  <-- gallery/install
   |
edit Template -> r8     <-- gallery still uses 1.0.0
   | publish
   v
TemplateVersion 1.0.1
```

### Invariants

1. Stable API identity is `Template.id`, not `name`.
2. Gallery/install paths use `TemplateVersion`, never mutable `Template.flow`.
3. Published versions are immutable.
4. Active display names are unique case-insensitively; archived names can be reused.
5. Studio cannot mutate `release_managed=true` rows.
6. Release synchronization never mutates Organization-managed or Private rows.
7. `app_type` and exact template-version provenance survive installation.

## 2. Code map

### Backend

- `app/models.py` — `Template`, `TemplateVersion`, `TemplateSource`, `TemplateStatus`, Agent provenance.
- `app/routers/templates.py` — authoring, publish, gallery, versions, archive, import/export, instantiate.
- `app/template_packages.py` — package parser/builder, sanitization, requirements.
- `app/promote_template.py` — release promotion, publish-level validation, immutable package lock, CI check.
- `app/template_packages/*.json` — shipped Platform templates.
- `app/template_packages.lock.json` — historical release version/content lock.
- `app/seed.py` — release-managed template convergence.
- `migrations/versions/a62f91c7e4d2_template_draft_publish_versions.py` — lifecycle/version/provenance schema and partial active-name index.
- `tests/test_platform_private_templates.py` — lifecycle, package, seeder, regression tests.
- `tests/test_alembic_migrations.py` — real fresh-Postgres migration coverage.

### Frontend

- `src/types.ts` — template DTOs including `origin`.
- `src/api.ts` — template APIs.
- `src/pages/Templates.tsx` — Published gallery + Manage templates.
- `src/modals/TemplateDetailsModal.tsx` — Save/Edit draft and Save & publish.
- `src/modals/NewAgentModal.tsx` — install a published template by stable id.
- `src/__tests__/PlatformPrivateTemplates.test.tsx` and `NewAgentModal.test.tsx` — lifecycle/UX regressions.

## 3. Data model

### Template

The mutable row stores authoring state: id/key/name/type/app_type/description/flow/source/status/category/vertical/tags/requirements/owner/release_managed/hidden plus `draft_revision`, `published_revision`, `current_version_id`, publisher timestamps and audit fields.

### TemplateVersion

Immutable publication snapshot: id/template_id/version/name/type/app_type/description/first_party/flow/category/vertical/tags/requirements/published_at/published_by.

### Agent provenance

An Agent instantiated from a template stores:

- `template_id`
- `template_version_id`
- `template_name` (display snapshot only)

Never use `template_name` to resolve current template state.

## 4. Optimistic locking

Every mutable template update and publish requires `If-Match` equal to the current `draft_revision`.

Accepted forms:

```http
If-Match: 7
If-Match: "7"
If-Match: W/"7"
```

The update uses DB-level compare-and-swap. Identical content is detected before the write and does not increment `draft_revision`.

## 5. Save / sanitize / validate

Save captures an Agent flow, deep-copies it, blanks literal secret-like config values, computes requirements, and returns non-blocking validation findings.

Publish is stricter: it rejects empty or invalid flows with 422. The saved draft remains intact.

When adding a new secret-bearing node config key, extend the sanitizer pattern/tests.

Agent-node config that is not matched by key name is handled explicitly in `sanitize_flow`: `skills`/`files` ids are dropped (the assets themselves are carried as `skills_embedded` / `files_embedded` by `embed_agent_assets`, and `files_required` lists what the installer must upload; `embed_agent_assets` raises `AssetError` with code `TEMPLATE_ASSET_MISSING` / `TEMPLATE_FILE_NOT_EMBEDDABLE` rather than dropping anything, and `bind_requirement` resolves an entry) and `env[].value` is blanked. Node ENV values live in `agentnodesecret`, not in the flow, so a template never contains them.

## 6. HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/templates` | published gallery |
| GET | `/api/templates/manage` | mutable authoring rows |
| GET | `/api/templates/facets` | counts over the published gallery, including source/origin totals |
| GET | `/api/templates/{id}/versions` | immutable history |
| POST | `/api/templates` | capture Agent as draft |
| PUT | `/api/templates/{id}` | update draft (`If-Match`) |
| POST | `/api/templates/{id}/publish` | validate + immutable publish (`If-Match`) |
| DELETE | `/api/templates/{id}` | archive |
| POST | `/api/templates/{id}/duplicate` | new Private draft from published/draft state |
| GET | `/api/templates/{id}/export?state=...` | package export |
| POST | `/api/templates/import` | Private draft import |
| POST | `/api/templates/{id}/instantiate` | create Agent from current immutable version |

## 7. Name uniqueness and archive

The database has a partial unique index on `lower(name)` only where `status <> 'archived'`. This matches the API check and means:

- `A_C` is not interpreted as a wildcard;
- `Name` and `name` conflict while active;
- archiving frees the display name;
- version/history rows remain untouched.

Do not reintroduce a global `UNIQUE(name)` constraint.

## 8. Package format

```json
{
  "package_version": "1",
  "manifest": {
    "key": "lead-scoring",
    "name": "Lead Scoring",
    "version": "1.0.0",
    "type": "App",
    "app_type": "workflow",
    "category": "Sales",
    "vertical": "Banking",
    "tags": [],
    "desc": "...",
    "first_party": true
  },
  "flow": {"nodes": [], "edges": []},
  "requirements": {}
}
```

`requirements` is recomputed from `flow`; input cannot under-declare dependencies.

## 9. Release promotion

Author/test a template, publish it on the authoring instance, export the published state, then use `python -m app.promote_template ... --version X.Y.Z`.

`promote_template` validates the package **and flow**, writes the shipped file, and updates `app/template_packages.lock.json`.

`python -m app.promote_template --check` is mandatory in CI. It rejects:

- malformed packages,
- duplicate keys/names,
- flows that Studio Publish would reject,
- a manual content change at the same locked version,
- an unpromoted package/version that does not match the lock.

Retired package keys stay in the lock so the same/lower version cannot later be reintroduced accidentally.

## 10. Seeder behavior

`sync_platform_templates` owns only release-managed Platform rows.

Bad release package state must not make application startup unavailable:

- lower package version than DB -> log + skip;
- changed content at same version -> log + skip;
- valid higher version -> create immutable snapshot;
- package removed -> archive/hide release row.

The release pipeline, not runtime startup, is the enforcement boundary for immutable package mistakes.

### Local-name collision during release

If a shipped template claims the name of an active local template, the local row is renamed. If the local template is already published, the seeder creates a patch snapshot carrying only the renamed published metadata. If that template also has unrelated unpublished draft edits, they remain unpublished.

## 11. Frontend behavior

Published gallery shows only immutable published snapshots and labels origins as:

- Platform provided
- Organization-managed
- Private

Manage templates exposes drafts, unpublished changes, and release-managed read-only rows.

The Save dialog uses **Save & publish** as the direct path and keeps **Save draft** for unfinished work.

The New Agent modal displays the published version next to a template name and sends only the stable `template_id`.

## 12. Testing gates

Backend:

```bash
alembic upgrade head
pytest tests/test_platform_private_templates.py
pytest tests/test_alembic_migrations.py
python -m app.promote_template --check
```

Frontend:

```bash
npx vitest run src/__tests__/PlatformPrivateTemplates.test.tsx src/__tests__/NewAgentModal.test.tsx src/__tests__/Templates.test.tsx
npx tsc --noEmit
```

Regression coverage includes fresh migration, archive/reuse, literal `_`/`%` names, quoted If-Match, identical-save idempotency, seeder rollback, same-version release changes, local/release name clashes, package lock enforcement, and shipped-flow publish validation.

## 13. Troubleshooting

- **409 stale revision** — reload the template and retry with the current `draft_revision`.
- **422 publish blocked** — the draft remains saved; fix the returned validator issues.
- **Template absent from gallery** — it is draft/archived or has no `current_version_id`.
- **Draft changes not visible in gallery** — expected until publish.
- **Release package skipped during startup** — inspect logs; CI/promotion should have prevented a rollback or same-version content change.
- **`promote_template --check` says package/lock mismatch** — use the promotion command with a higher version; do not hand-edit shipped package state.
- **403 edit on Platform provided** — release-managed packages are read-only; duplicate to a Private draft.

## 14. Out of scope

Marketplace publication, package signing, Control Plane registry/release stages, and automatic upgrade of already-instantiated Agents are not part of this version.
