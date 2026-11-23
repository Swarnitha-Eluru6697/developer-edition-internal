/**
 * The single sidebar, grouped like the other InAiEra apps (BUILD / JOBS / …).
 * Workspace is the editor screen; every other entry is a full-page view with
 * the file panel and chat hidden. Data Quality holds the whole DQ surface:
 * rules, rulesets, job bindings and their rendered checks.
 *
 * `permission` gates an item: the caller must hold it (or, for a list, at least
 * one of them) — the same IDs the backend checks (permissions.json). Omitted
 * for pages anyone who may open Workspace at all (workspace:access:view) sees.
 * A jobs page opens for whoever may edit OR run that kind of job; the actions
 * inside it are gated on their own permission. Data Quality opens for anyone who
 * may view quality status (workspace:dq:view); Settings for holders of
 * workspace:settings:edit (administration on it needs workspace:settings:manage).
 */
import { hasAnyPermission, type Permission } from "../rbac/permissions";
import type { NavSection } from "../store/uiSlice";

export interface NavItem { id: NavSection; icon: string; label: string; permission?: Permission | readonly Permission[] }
export interface NavGroup { label: string; items: NavItem[] }

export const NAV_SECTIONS: NavGroup[] = [
  { label: "Build", items: [{ id: "ws", icon: "ti-code", label: "Workspace" }] },
  {
    label: "Jobs",
    items: [
      { id: "dbt", icon: "ti-git-merge", label: "dbt", permission: ["workspace:dbt:edit", "workspace:dbt:run"] },
      // SQL and Python jobs run in the same Runner as dbt projects, each kind with its own edit / run permission.
      { id: "sqljobs", icon: "ti-file-database", label: "SQL", permission: ["workspace:sql-jobs:edit", "workspace:sql-jobs:run"] },
      { id: "pythonjobs", icon: "ti-brand-python", label: "Python", permission: ["workspace:python-jobs:edit", "workspace:python-jobs:run"] },
      { id: "flink", icon: "ti-wave-sine", label: "Flink", permission: ["workspace:flink:edit", "workspace:flink:deploy"] },
    ],
  },
  {
    label: "Govern",
    items: [
      { id: "dq", icon: "ti-checkup-list", label: "Data Quality", permission: "workspace:dq:view" },
      // Failures of the jobs above, posted to Slack / Teams channels. Opens for anyone who may view alerts; acknowledging
      // needs workspace:alerts:edit, rules and channels workspace:alerts:manage (gated inside the page).
      { id: "alerts", icon: "pi-bell", label: "Alerts", permission: "workspace:alerts:view" },
      // The ordinary Settings page; administration on it (the codegen agent and its key) needs workspace:settings:manage,
      // which the backend reports per card (`can_edit`).
      { id: "settings", icon: "pi-cog", label: "Settings", permission: "workspace:settings:edit" },
    ],
  },
];

const NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((s) => s.items);

const allowed = (item: NavItem, permissions: readonly string[]) => hasAnyPermission(permissions, item.permission);

/** The sidebar as a holder of `permissions` sees it: gated items removed, empty groups dropped. */
export function visibleNavSections(permissions: readonly string[]): NavGroup[] {
  return NAV_SECTIONS
    .map((g) => ({ ...g, items: g.items.filter((i) => allowed(i, permissions)) }))
    .filter((g) => g.items.length > 0);
}

/** False when `section` isn't in the sidebar at all, or `permissions` don't open it. */
export function canOpenSection(section: NavSection, permissions: readonly string[]): boolean {
  const item = NAV_ITEMS.find((n) => n.id === section);
  return !!item && allowed(item, permissions);
}
