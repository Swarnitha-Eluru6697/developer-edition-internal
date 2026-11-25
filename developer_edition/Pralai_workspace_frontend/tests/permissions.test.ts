/**
 * Role-free authorisation: the UI knows permission IDs only (openapi/permissions.json, a copy of the workspace backend's
 * permissions.json), loads the caller's permissions from GET /api/v1/me, and treats unknown / loading / failed as "nothing".
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { render, validate } from "../scripts/generate-rbac.mjs";
import { canOpenSection, NAV_SECTIONS, visibleNavSections } from "../src/app/navigation";
import { setRuntime } from "../src/api/apiBase";
import { permissionsApi } from "../src/rbac/api";
import { PERMISSION_LABELS, PERMISSIONS } from "../src/rbac/generated/permissions";
import { ACCESS_PERMISSION, hasAnyPermission, hasPermission, isPermission, toPermissions } from "../src/rbac/permissions";
import { JOB_PERMISSIONS } from "../src/features/dbt/jobKinds";
import { callEndpoint } from "./helpers/apiHarness";

const DOC = JSON.parse(readFileSync(resolve(__dirname, "../openapi/permissions.json"), "utf8"));
const ids = (sections: ReturnType<typeof visibleNavSections>) => sections.flatMap((g) => g.items.map((i) => i.id));

describe("the generated permission module is openapi/permissions.json", () => {
  it("src/rbac/generated/permissions.ts is exactly what the generator produces from the committed copy", () => {
    const onDisk = readFileSync(resolve(__dirname, "../src/rbac/generated/permissions.ts"), "utf8");
    expect(onDisk).toBe(render(DOC));
  });

  it("PERMISSIONS and their labels are the document's, in order", () => {
    expect([...PERMISSIONS]).toEqual(DOC.permissions.map((p: { id: string }) => p.id));
    for (const p of DOC.permissions) expect(PERMISSION_LABELS[p.id as keyof typeof PERMISSION_LABELS]).toBe(p.label);
  });

  it("every ID follows the contract format and workspace:access:view is published", () => {
    for (const p of PERMISSIONS) expect(p).toMatch(/^workspace:[a-z-]+:[a-z]+$/);
    expect(PERMISSIONS).toContain(ACCESS_PERMISSION);
  });

  it("the copy matches the backend's permissions.json when that repo is checked out next to this one", () => {
    let backend: string | null = null;
    try { backend = readFileSync(resolve(__dirname, "../../Pralai_workspace_backend/permissions.json"), "utf8"); } catch { /* not checked out */ }
    if (backend !== null) expect(JSON.parse(backend)).toEqual(DOC);
  });

  it("the generator rejects a malformed document", () => {
    expect(() => validate({ app: "other", version: 1, permissions: [] })).toThrow();
    expect(() => validate({ app: "workspace", version: 1, permissions: [{ id: "Workspace:X", label: "x" }] })).toThrow();
    expect(() => validate({ app: "workspace", version: 1, permissions: [{ id: "workspace:sql:run", label: "Run" }] })).toThrow(/access:view/);
  });

  it("every permission the UI gates on is a published one", () => {
    const used = [
      ...NAV_SECTIONS.flatMap((g) => g.items.flatMap((i) => (i.permission === undefined ? [] : typeof i.permission === "string" ? [i.permission] : [...i.permission]))),
      ...Object.values(JOB_PERMISSIONS).flatMap((p) => [p.edit, p.run]),
    ];
    for (const p of used) expect(isPermission(p)).toBe(true);
  });
});

describe("permission checks", () => {
  it("toPermissions keeps only known Workspace IDs, deduplicated, in document order; anything else is nothing", () => {
    expect(toPermissions(["workspace:sql:run", "mlstudio:clusters:view", "workspace:access:view", "workspace:sql:run", 7, "admin"]))
      .toEqual(["workspace:access:view", "workspace:sql:run"]);
    expect(toPermissions(undefined)).toEqual([]);
    expect(toPermissions("workspace:sql:run")).toEqual([]);
  });

  it("exact match only: no wildcards, no implied permissions", () => {
    expect(hasPermission(["workspace:flink:edit"], "workspace:flink:deploy")).toBe(false);
    expect(hasPermission(["workspace:*"], "workspace:flink:deploy")).toBe(false);
    expect(hasPermission(["workspace:flink:deploy"], "workspace:flink:deploy")).toBe(true);
    expect(hasAnyPermission([], ["workspace:dbt:edit", "workspace:dbt:run"])).toBe(false);
    expect(hasAnyPermission(["workspace:dbt:run"], ["workspace:dbt:edit", "workspace:dbt:run"])).toBe(true);
    expect(hasAnyPermission([], undefined)).toBe(true);
  });
});

describe("navigation from permissions", () => {
  it("no permissions: only the ungated pages; every gated page is hidden", () => {
    expect(ids(visibleNavSections([]))).toEqual(["ws"]);
    for (const s of ["dbt", "sqljobs", "pythonjobs", "flink", "dq", "settings"] as const) expect(canOpenSection(s, [])).toBe(false);
  });

  it("each page opens with its own permission, and a jobs page with either edit or run of that kind", () => {
    expect(ids(visibleNavSections(["workspace:dq:view"]))).toEqual(["ws", "dq"]);
    expect(ids(visibleNavSections(["workspace:dbt:run"]))).toEqual(["ws", "dbt"]);
    expect(ids(visibleNavSections(["workspace:sql-jobs:edit"]))).toEqual(["ws", "sqljobs"]);
    expect(ids(visibleNavSections(["workspace:python-jobs:run"]))).toEqual(["ws", "pythonjobs"]);
    expect(ids(visibleNavSections(["workspace:flink:deploy"]))).toEqual(["ws", "flink"]);
    // Settings: the ordinary page needs settings:edit; settings:manage alone (administration) does not add the page.
    expect(ids(visibleNavSections(["workspace:settings:edit"]))).toEqual(["ws", "settings"]);
    expect(canOpenSection("settings", ["workspace:settings:manage"])).toBe(false);
    // dbt permissions do not open the SQL / Python job pages any more.
    expect(canOpenSection("sqljobs", ["workspace:dbt:edit", "workspace:dbt:run"])).toBe(false);
  });

  it("every permission: every page", () => {
    expect(ids(visibleNavSections(PERMISSIONS))).toEqual(["ws", "dbt", "sqljobs", "pythonjobs", "flink", "dq", "alerts", "settings"]);
  });
});

describe("GET /api/v1/me", () => {
  afterEach(() => setRuntime({ apiBase: "", embedded: false }));

  it("is read from the API base, standalone", async () => {
    const body = { user: "ada", userId: "sub-1", tenant: "org-1", permissions: ["workspace:access:view"] };
    const { request, data } = await callEndpoint(permissionsApi.endpoints.getMyPermissions, undefined, body);
    expect(request).toMatchObject({ method: "GET", url: "/api/v1/me" });
    expect(data).toEqual(body);
  });

  it("goes through the portal's tunnel when embedded", async () => {
    setRuntime({ apiBase: "/control_plane/api/dataplane/workspace-api/", embedded: true });
    const { request } = await callEndpoint(permissionsApi.endpoints.getMyPermissions, undefined, { permissions: [] });
    expect(request.url).toBe("/control_plane/api/dataplane/workspace-api/api/v1/me");
  });
});
