/*
 * The UI authorises from permissions only (contract docs/auth/01-CONTRACT.md). These tests pin that:
 *  - the generated permission list matches the backend's permissions.json (when the backend repo is checked out next to this one, or
 *    DATACONNECT_PERMISSIONS_FILE points at it) and is well formed;
 *  - every permission the UI checks with can("…") is one the backend declares;
 *  - no role name, role prop or client identity header survives anywhere under src/;
 *  - the permission checker is least-privilege: anything missing, malformed or unknown grants nothing.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { PERMISSION_IDS } from "../../src/types/permissions.generated";
import { ACCESS_PERMISSION, CAN_NOTHING, effectivePermissions, permissionChecker, type Permission } from "../../src/permissions";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SRC = join(ROOT, "src");
const BACKEND_PERMISSIONS = resolve(process.env.DATACONNECT_PERMISSIONS_FILE || join(ROOT, "..", "InaiEra_DataConnect_Backend", "permissions.json"));

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx|css)$/.test(name) ? [path] : [];
  });
}
const rel = (file: string) => file.slice(SRC.length + 1);

test.describe("the generated permission list", () => {
  test("is well formed: dataconnect:<area>:<action>, unique, and includes dataconnect:access:view", () => {
    expect(PERMISSION_IDS.length).toBeGreaterThan(0);
    for (const id of PERMISSION_IDS) expect(id).toMatch(/^dataconnect:[a-z][a-z-]*:[a-z][a-z-]*$/);
    expect(new Set(PERMISSION_IDS).size).toBe(PERMISSION_IDS.length);
    expect(PERMISSION_IDS).toContain(ACCESS_PERMISSION);
  });

  test("equals the backend's permissions.json", () => {
    test.skip(!existsSync(BACKEND_PERMISSIONS), `backend permissions.json not found at ${BACKEND_PERMISSIONS}`);
    const backend = JSON.parse(readFileSync(BACKEND_PERMISSIONS, "utf8")) as { app: string; permissions: { id: string }[] };
    expect(backend.app).toBe("dataconnect");
    expect([...PERMISSION_IDS]).toEqual(backend.permissions.map((p) => p.id));
  });
});

test.describe("the UI only checks declared permissions", () => {
  const files = sourceFiles(SRC).filter((f) => !f.endsWith(".generated.ts"));

  test("every dataconnect:* literal under src/ is declared", () => {
    const used = new Set(files.flatMap((f) => [...readFileSync(f, "utf8").matchAll(/["'](dataconnect:[a-z-]+:[a-z-]+)["']/g)].map((m) => m[1]!)));
    expect(used.size).toBeGreaterThan(10);
    for (const id of used) expect(PERMISSION_IDS as readonly string[], id).toContain(id);
  });

  test("every page is gated by a permission", () => {
    const shell = readFileSync(join(SRC, "DataConnectApp.tsx"), "utf8");
    const block = shell.slice(shell.indexOf("const PAGE_PERMISSION"), shell.indexOf("};", shell.indexOf("const PAGE_PERMISSION")));
    for (const page of ["dash", "mon", "conn", "pip", "sch", "qual", "lin", "trn", "cat"]) {
      expect(block, page).toMatch(new RegExp(`\\b${page}: "dataconnect:[a-z-]+:view"`));
    }
  });
});

test.describe("no roles and no client identity in the UI", () => {
  const files = sourceFiles(SRC).filter((f) => !f.endsWith("dataconnect-api.generated.ts"));

  test("no role name appears anywhere under src/", () => {
    const roleName = /DataConnect (Admin|Integrator|Viewer)|DATACONNECT_(ADMIN|INTEGRATOR|VIEWER)|dataconnect-(admin|integrator|viewer)|\b(data-engineer|no-access)\b|["'](admin|reader|viewer|integrator)["']/;
    expect(files.filter((f) => roleName.test(readFileSync(f, "utf8"))).map(rel)).toEqual([]);
  });

  test("no role prop, role switcher or simulated user is left", () => {
    const leftovers = /\bsimulatedUser\b|\bRoleSwitch\b|\bUserSimulator\b|\bresolveHostRole\b|\bnormalizeRole\b|\bROLE_LABELS\b|\bCAPABILITY_MATRIX\b|\brole-pill\b|\buser-sim\b/;
    expect(files.filter((f) => leftovers.test(readFileSync(f, "utf8"))).map(rel)).toEqual([]);
  });

  test("no request sets an identity, tenant or role header", () => {
    const header = /["'](X-Org-Id|X-Actor|X-User-Role|X-User-Roles|X-User-Permissions|X-User-Name|X-Tenant-Id)["']/i;
    expect(files.filter((f) => header.test(readFileSync(f, "utf8"))).map(rel)).toEqual([]);
  });
});

test.describe("permissionChecker is least privilege", () => {
  test("holds exactly what /me returned", () => {
    const can = permissionChecker(["dataconnect:access:view", "dataconnect:pipelines:view"]);
    expect(can("dataconnect:pipelines:view")).toBe(true);
    expect(can("dataconnect:pipelines:edit")).toBe(false);
  });

  for (const bad of [undefined, null, [], "dataconnect:pipelines:view", { "dataconnect:pipelines:view": true }, [7, null, {}]]) {
    test(`${JSON.stringify(bad) ?? "undefined"} grants nothing`, () => {
      const can = permissionChecker(bad as never);
      for (const id of PERMISSION_IDS) expect(can(id as Permission), id).toBe(false);
    });
  }

  test("another app's permission, a role name or a wildcard grants nothing", () => {
    const can = permissionChecker(["ucs:pipelines:view", "admin", "DataConnect Admin", "dataconnect:*", "dataconnect:pipelines:*"]);
    for (const id of PERMISSION_IDS) expect(can(id as Permission), id).toBe(false);
  });

  test("CAN_NOTHING (before /me answers, or when it failed) holds nothing", () => {
    for (const id of PERMISSION_IDS) expect(CAN_NOTHING(id as Permission)).toBe(false);
  });
});

test.describe("view as role: effectivePermissions = /me ∩ viewPermissions", () => {
  const me = ["dataconnect:access:view", "dataconnect:pipelines:view", "dataconnect:settings:view"];

  test("narrows to the chosen role's permissions", () => {
    expect(effectivePermissions(me, ["dataconnect:access:view", "dataconnect:pipelines:view"])).toEqual(["dataconnect:access:view", "dataconnect:pipelines:view"]);
  });

  test("never widens: an ID only in viewPermissions stays absent", () => {
    const can = permissionChecker(effectivePermissions(me, [...PERMISSION_IDS]));
    expect(can("dataconnect:settings:view")).toBe(true);
    expect(can("dataconnect:settings:edit")).toBe(false);
    expect(effectivePermissions([], [...PERMISSION_IDS])).toEqual([]);
    expect(effectivePermissions(null, [...PERMISSION_IDS])).toEqual([]);
  });

  for (const none of [null, undefined]) {
    test(`${String(none)} means no narrowing`, () => {
      expect(effectivePermissions(me, none)).toEqual(me);
    });
  }

  test("an empty list narrows to nothing; junk entries are ignored", () => {
    expect(effectivePermissions(me, [])).toEqual([]);
    expect(effectivePermissions(me, [7, null, "dataconnect:pipelines:view"] as never)).toEqual(["dataconnect:pipelines:view"]);
  });
});
