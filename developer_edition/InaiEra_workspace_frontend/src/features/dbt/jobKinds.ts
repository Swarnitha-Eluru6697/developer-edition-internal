/**
 * The kinds of job the Runner runs, and how each one is shown. "dbt": a dbt project folder. "sql": one .sql file of a repository,
 * its statements run in order against the job's engine. Both share the same screens (list, runs, schedule, Dependents), Dagster
 * jobs and run machinery; only these words, the register form and the Lineage tab differ.
 */
import type { DbtProject } from "../../types";
import type { Permission } from "../../rbac/permissions";
import type { RunUnit } from "./runCounts";

export type JobKind = "dbt" | "sql" | "python";

export interface JobKindUi {
  title: string;
  icon: string;
  registerLabel: string;
  registerTitle: string;
  emptyTitle: string;
  emptyHint: string;
  unit: RunUnit;
  hasLineage: boolean;
}

/** What each kind of job needs: `edit` to register, change or delete one, `run` to start a run. The backend and the Runner check the
 *  same permissions (app/core/rbac.py JOB_KIND_EDIT / JOB_KIND_RUN). */
export const JOB_PERMISSIONS: Record<JobKind, { edit: Permission; run: Permission }> = {
  dbt: { edit: "workspace:dbt:edit", run: "workspace:dbt:run" },
  sql: { edit: "workspace:sql-jobs:edit", run: "workspace:sql-jobs:run" },
  python: { edit: "workspace:python-jobs:edit", run: "workspace:python-jobs:run" },
};

export const JOB_KINDS: Record<JobKind, JobKindUi> = {
  dbt: {
    title: "dbt Projects", icon: "ti-git-merge", registerLabel: "Register project", registerTitle: "Register dbt project",
    emptyTitle: "No projects yet", emptyHint: "Register a dbt project to start running and scheduling builds.",
    unit: "model", hasLineage: true,
  },
  sql: {
    title: "SQL Jobs", icon: "ti-file-database", registerLabel: "Register SQL job", registerTitle: "Register SQL job",
    emptyTitle: "No SQL jobs yet",
    emptyHint: "Push a .sql file to Git, then register it here to run it on a schedule.",
    unit: "statement", hasLineage: false,
  },
  python: {
    title: "Python Jobs", icon: "ti-brand-python", registerLabel: "Register Python job", registerTitle: "Register Python job",
    emptyTitle: "No Python jobs yet",
    emptyHint: "Push a .py file to Git, then register it here to run it on a schedule.",
    unit: "step", hasLineage: false,
  },
};

/** A project as the list shows it: an older runner that does not send `kind` only has dbt projects. */
export const kindOf = (p: Pick<DbtProject, "kind">): JobKind => (
  p.kind === "sql" ? "sql" : p.kind === "python" ? "python" : "dbt"
);
