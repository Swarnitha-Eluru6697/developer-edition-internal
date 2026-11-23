# Developer Edition: Admin Portal UI and API change plan

Source: *Developer Edition – Plan of Execution* (sections "Registered users, downloads and login audit", "Admin portal", "Errors and security", "Detection and enforcement", checklist items 18–21).
Repos touched: `InaiEra_admin_portal_backend` (admin) and `InaiEra_admin_portal_frontend` (adminui).

## 1. Decisions to confirm first

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Where does the Registration service live? | A new module inside the admin backend: `src/devedition/`, mounted at `/v1`, tables in a separate Postgres schema `dev_edition`. Split into its own deployable later if traffic needs it. | The admin backend already has Express, `pg`, SendGrid (`utils/email.ts`), bcrypt, JWT and SAML. A separate service doubles the work in a 15-day window. Public `/v1/*` and admin `/v1/admin/*` can still sit on different hostnames (`register.<domain>`, `admin.<domain>`) via ingress. |
| D2 | Admin / viewer roles | Reuse the existing RBAC (`features` + `permissions` + `requirePermission`) instead of a new `admin_users` table. "Viewer" = `view` on the Developer Edition features; "admin" = `view` + `edit` / `delete`. | SSO (SAML) and role management already exist in the portal; a second admin list would drift. Keep `admin_actions` as the audit table, with `admin_id` = existing `users.id`. |
| D3 | Error shape for the new routes | New `/v1` routes return the doc's shape `{ error: { code, message, ... } }`; existing `/api/*` routes stay `{ message }`. | Doc requires one shape; changing old routes is out of scope. Add a small `devError()` helper. |
| D4 | Public endpoints (the 11 under `/v1`) | In scope for "API end", built first, because every admin screen reads the data they write. | Without them the admin portal has nothing to show. |

## 2. Gaps and contradictions in the requirement doc (raise in review)

1. `registered_users` has no `license_version` / `license_accepted_at` columns, but the Legal section and checklist item 24 require them. **Add both.**
2. Legal section says "accept the license before the password is emailed", which contradicts the one-time-code flow. Fix wording.
3. Open question 1 says "Keycloak stays in the stack", which contradicts the scope (Keycloak removed). Close it.
4. No **sessions** table, yet `DELETE /v1/sessions/current` and "block → next check-in pauses the app" need server-side session/token state. **Add `dev_sessions`** (token hash, user, install, expires, revoked_at).
5. No **unblock** action. Block is reversible in practice. **Add `POST /v1/admin/users/{id}/unblock`** and `unblock_user` to the `admin_actions.action` enum.
6. Enforcement is "warning email first", but there is no action for it. **Add `POST /v1/admin/activations/{id}/warn`** (sends SendGrid template) and `send_warning` action; plus `flag_reviewed` so flagged installs can be dismissed.
7. `GET /v1/admin/verifications` shows **SendGrid bounces**, but nothing receives them. **Add `POST /v1/webhooks/sendgrid`** (Event Webhook, signed) and an `email_events` table.
8. `GET /v1/admin/flags` needs rules computed somewhere. Add a nightly job (or SQL view) that evaluates the four patterns (24/7 uptime for N weeks, > N activations per account, cloud-IP check-ins, > N sign-ups from one company domain). Thresholds are not in the doc. **Need numbers.**
9. "Active installs" is undefined. Propose: activation `status = active` and `last_check_in` within 8 days (7-day check-in + 1 day grace).
10. `admin_actions.target_id` points to a user **or** an activation. Add `target_type` (`user` / `activation`).
11. `download_events.user_id` is null for GitHub downloads; the Downloads screen needs a `registered` filter (already listed), and the backfill rule (link a download to a user when that `install_id` later registers) is not written. **Add it** in `POST /v1/registrations`.
12. `user_activity` view needs `license_status`; derive from the latest activation (`active` / `deactivated` / `revoked` / `none`).
13. CORS today allows *any* localhost origin. Requirement: public routes allow only the marketing site + `http://localhost:8080`; `/v1/admin/*` allows only the portal domain. Needs per-router CORS.
14. Delete-user anonymisation must cover `download_events`, `login_audit`, `email_verifications`, `license_activations`; specify that `email` becomes `deleted+<uuid>@invalid` so the unique constraint holds.

## 3. Backend (API) work

### 3.1 Database: migration `019_dev_edition.sql` (schema `dev_edition`)

Tables per the doc, plus the additions above:

- `registered_users` (+ `license_version`, `license_accepted_at`)
- `email_verifications`
- `dev_sessions` *(new)*
- `download_events`
- `login_audit`
- `license_activations`
- `admin_actions` (+ `target_type`; enum adds `unblock_user`, `send_warning`, `flag_reviewed`)
- `email_events` *(new, SendGrid webhook)*
- `install_flags` *(new: flag_type, activation_id/user_id, detected_at, reviewed_at, reviewed_by)*
- View `user_activity` (first download, last login, login count, latest version, license status)
- Indexes: `registered_users(lower(email))` unique, `login_audit(user_id, occurred_at desc)`, `download_events(downloaded_at)`, `license_activations(user_id, status)`

Migration `020_dev_edition_admin_features.sql`: add Admin Portal module **Developer Edition** (`dev_edition`) with features `dev_overview`, `dev_users`, `dev_downloads`, `dev_installations`, `dev_flags`, `dev_email_delivery`; permissions `view`, `edit`, `delete`; grant to roles **DevEdition Admin** (all) and **DevEdition Viewer** (`view` only).
> Watch out: `010_sync_admin_portal_features.sql` and `seed_full.ts` delete non-canonical Admin Portal modules. Update the canonical list in the seed scripts so a reseed does not wipe the new module.

### 3.2 Code layout

```
src/devedition/
  router.public.ts     /v1 (11 endpoints + webhook)
  router.admin.ts      /v1/admin (reads + actions)
  services/            registration, verification, password, session, license (sign/verify JWT RS256), activation, flags
  lib/errors.ts        devError(code, status, extra)
  lib/rateLimit.ts     per-IP / per-email limits (Postgres or in-memory + Redis later)
  lib/geo.ts           IP → country, cloud-IP ranges; never stores the IP
  jobs/flags.ts        nightly flag evaluation
```
`server.ts`: mount `/v1/admin` behind `authMiddleware` + per-route `requirePermission`; mount `/v1` with its own CORS. Remove the `/api/test-email` dev route before go-live.

### 3.3 Public endpoints (feed the admin data)

All 11 from the doc, unchanged contract: registrations, verify, resend-code, password, sessions (POST/DELETE), password/forgot, password/reset, downloads, activations/check-in, events/login-batch. Plus `POST /v1/webhooks/sendgrid`.
Key rules: bcrypt passwords (≥12 chars), SHA-256 codes, 10-min expiry, 5 tries, 3 resends/hour; 202 on forgot/resend; blocked user → `403 account_blocked` on sign-in **and** on check-in; check-in deactivates the account's other active activation.

### 3.4 Admin endpoints

| Endpoint | Permission | Notes |
|---|---|---|
| GET /v1/admin/stats | dev_overview:view | from, to |
| GET /v1/admin/stats/timeseries | dev_overview:view | metric = signups / downloads / active_installs; interval = day / week |
| GET /v1/admin/users | dev_users:view | q, verified, status, company, from, to, sort, page, page_size ≤ 200 |
| GET /v1/admin/users/{id} | dev_users:view | profile, consent, license acceptance, activations |
| GET /v1/admin/users/{id}/logins | dev_users:view | event filter, newest first |
| GET /v1/admin/downloads | dev_downloads:view | rows + counts by version / OS / country |
| GET /v1/admin/activations | dev_installations:view | status, is_cloud_ip, **user_id** (added) |
| GET /v1/admin/flags | dev_flags:view | type, **reviewed** (added) |
| GET /v1/admin/verifications | dev_email_delivery:view | sent / verified / expired / bounced |
| GET /v1/admin/export/users.csv | dev_users:view | streams CSV, same filters as /users |
| POST /v1/admin/users/{id}/block | dev_users:edit | reason required; revokes open sessions |
| POST /v1/admin/users/{id}/unblock *(new)* | dev_users:edit | reason |
| POST /v1/admin/users/{id}/resend-verification | dev_users:edit | only if unverified |
| POST /v1/admin/activations/{id}/warn *(new)* | dev_installations:edit | sends warning template |
| POST /v1/admin/activations/{id}/revoke | dev_installations:edit | reason required |
| POST /v1/admin/flags/{id}/review *(new)* | dev_flags:edit | marks reviewed / dismissed |
| DELETE /v1/admin/users/{id} | dev_users:delete | anonymise, reason required |
| GET /v1/admin/actions *(new)* | dev_users:view | audit log list for the Audit screen |

Every action writes `admin_actions` in the same DB transaction as the change. A viewer gets `403 forbidden` in the doc's error shape.

### 3.5 Config / secrets (`.env` of the admin backend only)

`DEV_LICENSE_PRIVATE_KEY`, `DEV_LICENSE_KEY_ID`, `DEV_SENDGRID_TEMPLATE_{VERIFY,RESET,WELCOME,WARNING}`, `SENDGRID_WEBHOOK_PUBLIC_KEY`, `DEV_PUBLIC_CORS_ORIGINS`, `DEV_ADMIN_CORS_ORIGINS`, `DEV_PRODUCT_NAME` (name not final, read from config). None of these go into the public repo or images.

## 4. Frontend (Admin Portal UI) work

### 4.1 Navigation and routing
- `Layout.tsx` `navConfig`: new section **Developer Edition** with items Overview, Users, Downloads, Installations, Flagged installs, Email delivery, Audit log; each gated by `can('<feature>', 'view')`. Add `openSections['Developer Edition']`.
- `App.tsx`: routes under `/dev-edition/*` wrapped in `ProtectedRoute`.
- `api/axios.ts`: second instance (or base-path option) for `/v1/admin`, and an error interceptor that reads `error.code` / `error.message` for toasts.
- Sign-in: no change; SSO already exists. Only ensure the SSO user maps to the DevEdition Admin / Viewer role.

### 4.2 Screens

| Screen | Content | Actions (hidden unless `can(..., 'edit'/'delete')`) |
|---|---|---|
| **Overview** `/dev-edition` | Tiles: sign-ups, % verified, downloads, active installs, sign-ins (7d). Recharts line/bar: sign-ups, downloads, active installs per day/week. Date-range picker. | — |
| **Users** `/dev-edition/users` | Table: name, email, company, role, verified badge, first download, last login, logins, version, license status, status. Filters: search, verified, status, company, date range; sortable columns; server pagination. | Export CSV |
| **User detail** `/dev-edition/users/:id` | Profile, consent, license version + accepted at, registration source; tabs: Installations (activations), Sign-ins (login_audit, event filter), Admin history (admin_actions for this user). | Block / Unblock, Resend verification, Revoke an activation, Delete user. Each opens a confirm modal with a required **reason**. Delete needs typed confirmation (email). |
| **Downloads** `/dev-edition/downloads` | Table of download_events; breakdown cards/charts by version, OS/arch, country; filter registered vs anonymous. | — |
| **Installations** `/dev-edition/installations` | license_activations: user, install_id (short), OS, version, status, first/last check-in, country, cloud-IP badge. Filters: status, cloud IP. | Warn, Revoke |
| **Flagged installs** `/dev-edition/flags` | Grouped by type (24/7, many activations, cloud IP, same domain), with the evidence row and link to user. | Send warning, Mark reviewed, Revoke |
| **Email delivery** `/dev-edition/email` | Codes sent / verified / expired, bounce and drop list from SendGrid events, date range. | Resend (for unverified users) |
| **Audit log** `/dev-edition/audit` | admin_actions: when, admin, action, target, reason. | — |

`CrudPage` does not fit (no create/edit forms, server-side paging). Build one reusable `DataTable` (server paging, sort, filters, CSV) and a `ReasonModal`.

### 4.3 Watermark / support items
None in the admin portal; the banner, chat icon and Slack link belong to the Developer Control Plane.

## 5. Sequence (fits the 15-day window)

| Days | Backend | Frontend |
|---|---|---|
| 1–2 | Migrations 019/020, error helper, CORS split, RBAC features + roles | Nav section, routes, axios instance, DataTable + ReasonModal skeletons |
| 3–6 | Public endpoints: sign-up → verify → password → sessions → forgot/reset → downloads; SendGrid templates (test mailbox) | Overview (tiles + charts) against seeded data |
| 6–8 | Check-in + license token (RS256) + one-active-install rule; login-batch; SendGrid webhook | Users list + detail + CSV |
| 8–10 | Admin reads + actions with admin_actions audit; seed script `seed:devedition` | Downloads, Installations, Audit log |
| 10–12 | Flags job + flags/review/warn endpoints; rate limits; delete anonymisation | Flagged installs, Email delivery; viewer vs admin button gating |
| 12–14 | Vitest + supertest suites; security pass (no secrets, CORS, 202 responses) | Vitest/RTL tests; cross-browser check |
| 15 | Go-live on `register.<domain>` / `admin.<domain>` once Infra inputs arrive | — |

## 6. Tests to add (maps to checklist 15–21, 24, 26–27)

- Sign-up → code → password → sign-in writes `login_audit`; wrong code returns `attempts_left`; 5 tries / 10 min → `410`; 4th resend in an hour → `429`.
- Unverified sign-in → `403 email_not_verified`; blocked → `403 account_blocked` on sign-in and check-in.
- Second machine check-in deactivates the first.
- Viewer gets `403 forbidden` on every action; admin action writes exactly one `admin_actions` row.
- Delete anonymises every table and keeps counts.
- `/users` sort `-last_login`, `page_size` > 200 rejected; CSV matches list filters.
- UI: action buttons hidden for viewer; reason required; error toasts show server `message`.

## 7. Blocked on (from "What the build needs")

Infra: hosting + TLS for `register.` / `admin.`, managed Postgres, SendGrid domain auth + key, license key pair. Marketing: email templates, product name. Legal: license text + version. IT: SSO app registration and first admin list. Product: flag thresholds (gap 8).
