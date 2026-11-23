# Resource/data policy for production actions (X-006)

Workspace RBAC (`app/core/rbac.py`, [RBAC contract](../contracts/rbac.contract.v1.json)) is the **coarse product gate**: may a data
engineer deploy Flink jobs at all. **UCS is the resource/data policy gate**: may *this* person deploy *this* job, run *this* project,
publish *this* rule set, push *this* repository, against *this* data. Before, only data access (a connection's `query`) went to UCS;
every execution and deployment action was a local role decision.

## How it works

Every route of the dbt, Flink, Data Quality and Git routers names the product action it performs
(`app/core/data_policy.py: ROUTE_ACTIONS`). The check runs **inside the RBAC capability dependency, after the role check**: a role that
is refused is refused by RBAC without UCS being asked; one that is allowed is then governed by UCS. Routes that have no role capability
of their own (most Git routes) use `PolicyUser` for the same effect. `tests/test_data_policy.py` fails if a route of those routers has
no entry, a stale entry exists, or a gated route has nothing that applies the policy, so a new route cannot quietly skip it.

| Product action | UCS action | Routes |
|---|---|---|
| `dbt.project.read` | read | list/get projects, lineage, runs, run events |
| `dbt.project.edit` | write | register, update, delete a project |
| `dbt.run` | write | start a run |
| `dbt.deploy` | write | *(no route yet: Workspace has no separate production-deploy route; the name is reserved)* |
| `flink.job.read` | read | list/get a job, status, Flink UI link |
| `flink.job.edit` | write | create (also from Git), update, delete, validate, sync from Git |
| `flink.deploy` | write | deploy |
| `flink.cancel` | write | cancel |
| `flink.savepoint` | write | stop with savepoint |
| `dq.rule.read` | read | list/get rules, rulesets, items, bindings, rendered artifacts |
| `dq.rule.edit` | write | create/update/delete rules, rulesets, items |
| `dq.publish` | write | approve a custom rule, create/delete/resolve a binding, apply an artifact to a runner, sync checks to the catalog |
| `dq.execute` | query | validate a rule, re-render an artifact |
| `git.repo.read` | read | connections, repos, status, binding, pull, import |
| `git.repo.write` | write | commit, push, branch, pull request, store a token, disconnect |
| `dataset.read` / `dataset.preview` / `dataset.write` | read / query / write | enforced where the data is touched (below) |

Exempt (documented in `EXEMPT_REASONS`): the Git webhook (authenticated by the provider's signature) and the OAuth connect/callback
(they only start/complete a login).

## What UCS is asked

`POST /api/v1/authz/check` with UCS's own action vocabulary and an object:

* **object** — the target data object when the request names one (a DQ binding's `ucs_fqn`); otherwise a stable pseudo object
  `inaiera_workspace.<kind>.<id>` (`kind` = `dbt_project`, `flink_job`, `dq_rule`, `dq_binding`, `git_repo`). Governance can grant these
  like any object, e.g. `inaiera_workspace.flink_job.*` for the streaming team. The catalog name is `dataPolicy.catalog`.
* **context** — `product_action`, `environment`, `org_id`, `resource_kind`, `resource_id`, `resource_owner`, `target_data_object`,
  `classification` (UCS's classification tags for the target object, best effort), `route`, `roles`. The current UCS ignores fields it does
  not read; the context is also written to Workspace's audit log with every denied or would-be-denied decision.
* **identity** — the person's gateway username (`X-User-Name`), which is how UCS knows them. Everywhere else the identity is the immutable
  subject (W-020).

## Modes (`DATA_POLICY_MODE`, chart `dataPolicy.mode`)

* `off` — no UCS call.
* `audit` (**default**) — ask UCS, log the decision; a denial is audited as `data_policy.would_deny` and the action proceeds. After a
  UCS failure audit mode stops asking for 30 s so an outage never slows or refuses requests.
* `enforce` — a denial is `403 DATA_POLICY_DENIED`; UCS unreachable or not configured is `503 DATA_POLICY_UNAVAILABLE` (**fail closed**,
  never fail open). `DATA_POLICY_ENFORCED_ACTIONS` / `dataPolicy.enforcedActions` narrows enforce to some actions; the rest stay in audit.
  The chart refuses `enforce` without `ucs.enabled`.

## Rolling it out

UCS answers only on grants it has. Today no governance team has granted the pseudo objects, so `enforce` would deny everything: **run
`audit`, read the `data_policy.would_deny` audit rows, grant what those users legitimately need, then enforce action by action**
(for example `dataPolicy.enforcedActions: [flink.deploy, flink.cancel, dbt.run]` first).

## Not covered / open

* UCS does not read the context yet: it decides on action + object only. Using classification, environment and owner *in the decision*
  needs a UCS change (not part of this work, which was Workspace-side only).
* `dataset.write` is not a separate decision: SQL runs are authorised with UCS `query` on the connection's catalog object
  (`ucs_client.authorize_connection_use`), whatever the statement.
* `resource_owner` is known for Workspace's own resources (Git); for runner-owned dbt projects and Flink jobs Workspace does not look
  the owner up on each call, so it is empty.
* The runners and DQ still enforce their own scopes and roles as defence in depth; they do not call UCS.
