# Resource scoping: org-shared registries, workspace as a vouched-for label

(W-016, DBT-012, FL-003.)

## The model

| Resource | Who can see / use it | What `workspace_id` means |
|---|---|---|
| dbt projects and their runs (dbt runner) | everyone in the org with the dbt capability and scope | a **grouping label**: "registered from this Workspace". Not an ownership or isolation boundary. |
| Flink jobs and deployments (Flink runner) | everyone in the org with the Flink capability and scope | the same grouping label |
| Workspaces, workspace files, sessions (Workspace service) | the workspace's owner (`workspaces.owner_id`) | the real boundary lives here |

The two runner registries are **shared by the whole org**. Access control there is org + role capability (DBT-001, X-001). A
`workspace_id` never grants or limits access: the `?workspace_id=` list filter is a convenience for the UI. Operators and external CI
see every project/job of the org. Choosing the opposite model (workspace-scoped registries enforced in the runners) would need the
runners to know workspace membership, which lives only in the Workspace service; it was not chosen.

## What is enforced

A grouping label can still be abused (attaching your job to someone else's workspace pollutes their view). So a workspace id is accepted
only when **Workspace vouched for it**:

1. Workspace checks that the caller **owns** the workspace (`WorkspaceService.get`; a workspace that is not yours is a `404`, like every
   other workspace route) before forwarding. This covers `POST /dbt/projects/register`, `POST /flink/jobs`, `POST /flink/jobs/from-git`
   and `POST /flink/validate` (`app/core/workspace_scope.py`).
2. The verified id travels in the **signed delegated token** as the `workspace_id` claim (`mint_delegated_token(workspace_id=...)`).
3. The runners accept a `workspace_id` in a request body **only if it equals the claim** (`WORKSPACE_NOT_AUTHORIZED`, 403, otherwise). A
   token without the claim (external CI, an older Workspace) simply cannot assert a workspace; it can still register without one.
4. A claim alone assigns nothing: the body must ask for the workspace.

Reads stay org-wide. Rows written before this change keep whatever `workspace_id` they have (it was unverified); nothing rewrites them.

## Keeping it consistent (X-005)

The decision above is the answer to X-005 ("org-wide or workspace-scoped?"): **org-wide, with a vouched-for label**. What keeps the three
services consistent is checked by tests, so it cannot drift:

* Workspace: every proxy route that can carry a workspace id awaits `authorize_workspace`, and only the verified id is put in an
  outgoing body or token (`tests/test_workspace_scope_guard.py`: an AST check, plus a check that the guard itself flags a bad route).
* dbt runner and Flink runner: every route whose request model has a `workspace_id` calls `authorize_workspace`; the one exception is the
  Flink runner's internal `/validate`, which never stores or reads it (their `tests/test_workspace_scope.py`).
* Across repos: the claim name Workspace signs is the claim both runners read, and the two runners' `app/core/workspace_scope.py` are
  byte-identical (the Workspace guard fails when one is edited without the other).
* Paths audited and found clean: DQ never sends a workspace id to the Flink runner; codegen, executions and files use Workspace's own
  ownership checks; list filters (`?workspace_id=`) are filters; PATCH bodies cannot change a workspace (neither runner's update model has the field).

## Phase 1 collaboration model: workspaces are personal-only (W-021)

A Workspace workspace has exactly **one owner**. There is no membership, no ACL, no sharing, no review/approval between users in this
release; this is a **declared decision**, not an unfinished feature. It is exposed (`GET /auth/me` -> `workspace_model: "personal"`,
`app/core/workspace_scope.py: WORKSPACE_MODEL`), enforced (every workspace route is owner-only: someone else's workspace is a `404`)
and guarded (`tests/test_workspace_model.py` fails if a membership/ACL/sharing table, a client-settable workspace type or owner, an
owner-less workspace, or an unchecked workspace route appears).

What this means for the other services: the runners' `workspace_id` is a grouping label that Workspace vouched for (above), **not an
access-control list**. Two users of the same org can both see every dbt project and Flink job (the org-shared registries); that is
visibility of the registry, not membership of a workspace. A user cannot be "added to" someone's workspace, and nothing downstream
should be read as if they could.

Shared workspaces are a separate, larger product change. They would need, together: a membership table with the roles
`owner` / `editor` / `runner` / `viewer`; owner-or-member checks in every workspace route (files, sessions, executions, Git, codegen);
a stable workspace resource identity propagated to the runners and DQ (today it is only a label in a signed token claim); and the
frontend's role/permission handling. Changing `WORKSPACE_MODEL` is how that decision gets recorded; the tests above are meant to fail
until it is.
