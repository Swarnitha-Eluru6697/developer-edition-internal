import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// What ML Studio sends to UCS (task 25: UCS authorises on permissions).
// fetch is stubbed; each test inspects the headers of the one UCS request.
process.env.UCS_GATEWAY_SECRET = "ucs-secret";
process.env.UCS_SERVICE_USERNAME = "ml-studio-svc";

let calls: { url: string; headers: Record<string, string> }[] = [];
beforeEach(() => {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, headers: Object.fromEntries(Object.entries(init.headers as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v])) });
    return new Response(JSON.stringify({ decision: "allow", reason: "" }), { status: 200 });
  }));
});
afterEach(() => vi.unstubAllGlobals());

const roleOrBearer = (h: Record<string, string>) => Object.keys(h).filter((k) => k === "authorization" || k.startsWith("x-user-role"));

describe("UCS call headers", () => {
  it("lineage producer: service name + ucs:lineage-events:create, no role marker", async () => {
    const { ingestLineageEvent } = await import("../src/ucs/client");
    await ingestLineageEvent({ eventType: "COMPLETE", run: { runId: "r1" }, job: { namespace: "ml-studio", name: "train-x" } });
    const h = calls[0].headers;
    expect(calls[0].url).toMatch(/\/api\/v1\/lineage\/events$/);
    expect(h["x-inaiera-gateway-secret"]).toBe("ucs-secret");
    expect(h["x-user-name"]).toBe("ml-studio-svc");
    expect(h["x-user-permissions"]).toBe("ucs:lineage-events:create");
    expect(roleOrBearer(h)).toEqual([]);
  });

  it.each([
    ["registerCatalog", (c: any) => c.registerCatalog({ name: "ml_models", engine: "model" })],
    ["registerModel", (c: any) => c.registerModel("m1", "c")],
    ["registerModelVersion", (c: any) => c.registerModelVersion("m1", "s3://x", ["champion"], "c")],
    ["createSchema", (c: any) => c.createSchema("ml_models", "default")],
    ["createRole", (c: any) => c.createRole({ role_name: "r", catalog: "ml_models" })],
    ["bindGroupToRole", (c: any) => c.bindGroupToRole("g", "r")],
    ["revokeGroupFromRole", (c: any) => c.revokeGroupFromRole("g", "r")],
  ])("admin %s: service name + ucs:catalogs:manage,ucs:rbac:manage, no bearer", async (_n, call) => {
    const client = await import("../src/ucs/client");
    await call(client);
    const h = calls[0].headers;
    expect(calls[0].url).toContain("/api/v1/admin/");
    expect(h["x-inaiera-gateway-secret"]).toBe("ucs-secret");
    expect(h["x-user-name"]).toBe("ml-studio-svc");
    expect(h["x-user-permissions"]).toBe("ucs:catalogs:manage,ucs:rbac:manage");
    expect(roleOrBearer(h)).toEqual([]);
  });

  it("on behalf of a user (authz/check): user's name/id/groups, not their permissions", async () => {
    const { checkAuthz } = await import("../src/ucs/client");
    const { ucsIdentityFromPrincipal } = await import("../src/ucs/config");
    const principal = { username: "alice", userId: "sub-1", groups: ["data-science"], permissions: ["mlstudio:train:run"], tenantId: "acme", roles: ["x"] };
    await checkAuthz(ucsIdentityFromPrincipal(principal), "write", "ml_models.default.m1");
    const h = calls[0].headers;
    expect(h["x-inaiera-gateway-secret"]).toBe("ucs-secret");
    expect(h["x-user-name"]).toBe("alice");
    expect(h["x-user-id"]).toBe("sub-1");
    expect(h["x-user-groups"]).toBe("data-science");
    expect(h["x-user-permissions"]).toBeUndefined();
    expect(roleOrBearer(h)).toEqual([]);
  });
});
