// v1.1 permission API — contract 01-CONTRACT.md sections 4-6. Mirrors python/tests/test_permissions.py.
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import express from "express";
import http from "http";
import { gatewayAuth, requirePermission, meBody, splitMulti, extractIdentity, hasPermission } from "../src/index";

const SECRET = "real-secret";
const b64 = (v: unknown) => Buffer.from(JSON.stringify(v)).toString("base64");
const viaEnvoy = (permissions?: string[], extra: Record<string, string> = {}) => ({
  "x-inaiera-gateway-secret": SECRET, "x-user-name": "alice", "x-user-id": "u-123", "x-tenant-id": "acme",
  ...(permissions ? { "x-user-permissions": b64(permissions) } : {}),
  ...extra,
});

function get(base: string, path: string, headers: Record<string, string> = {}): Promise<{ status: number; body: any }> {
  return new Promise((resolve) => {
    http.get(base + path, { headers }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode || 0, body: data ? JSON.parse(data) : null }));
    });
  });
}

describe("splitMulti (contract 4)", () => {
  it.each([
    b64(["mlstudio:clusters:view", "ucs:rbac:manage"]),
    '["mlstudio:clusters:view","ucs:rbac:manage"]',
    "mlstudio:clusters:view, ucs:rbac:manage",
  ])("parses %s", (raw) => {
    expect(new Set(splitMulti(raw))).toEqual(new Set(["mlstudio:clusters:view", "ucs:rbac:manage"]));
  });

  it.each([null, undefined, "", "   ", b64("not-a-list"), b64({ a: 1 }), b64(5)])("invalid/empty %s -> []", (raw) => {
    expect(splitMulti(raw as any)).toEqual([]);
  });
});

describe("identity", () => {
  it("carries userId, permissions and audit-only roles", () => {
    const id = extractIdentity({ username: "alice", groups: null, tenantId: "acme", userId: "u-1",
      permissions: b64(["a:b:view", "a:b:view"]), roles: b64(["ml-engineer"]) }, "default")!;
    expect(id).toMatchObject({ userId: "u-1", tenantId: "acme", permissions: ["a:b:view"], roles: ["ml-engineer"] });
    expect(hasPermission(id, "a:b:view")).toBe(true);
    expect(hasPermission(id, "a:b:edit")).toBe(false);
    expect(hasPermission(null, "a:b:view")).toBe(false);
  });
});

describe("gatewayAuth + requirePermission (contract 6 A-G)", () => {
  let server: http.Server;
  let base: string;

  beforeAll(() => {
    const app = express();
    app.use(gatewayAuth({ publicPaths: ["/api/health", "/static/*"] }));
    app.get("/api/health", (_req, res) => res.json({ ok: true }));
    app.get("/static/app.js", (_req, res) => res.json({ js: true }));
    app.get("/api/clusters", requirePermission("mlstudio:clusters:view"), (_req, res) => res.json({ clusters: [] }));
    app.get("/api/anything", (_req, res) => res.json({ ok: true }));
    app.get("/api/me", (req, res) => res.json(meBody(req.principal!)));
    server = app.listen(0);
    base = `http://127.0.0.1:${(server.address() as any).port}`;
  });
  afterAll(() => server.close());
  beforeEach(() => {
    process.env.TRUST_ENVOY_HEADERS = "true";
    process.env.ENVOY_INTERNAL_SECRET = SECRET;
  });

  it("A: no secret -> 401, on every non-public route", async () => {
    expect((await get(base, "/api/clusters")).status).toBe(401);
    expect((await get(base, "/api/anything")).status).toBe(401);
  });
  it("B: wrong secret -> 401", async () => {
    expect((await get(base, "/api/clusters", viaEnvoy(["mlstudio:clusters:view"], { "x-inaiera-gateway-secret": "wrong" }))).status).toBe(401);
  });
  it("trust flag off -> 401", async () => {
    process.env.TRUST_ENVOY_HEADERS = "false";
    expect((await get(base, "/api/clusters", viaEnvoy(["mlstudio:clusters:view"]))).status).toBe(401);
  });
  it("C: right secret, no permission header -> 403; no identity -> 401", async () => {
    expect((await get(base, "/api/clusters", viaEnvoy())).status).toBe(403);
    expect((await get(base, "/api/clusters", { "x-inaiera-gateway-secret": SECRET })).status).toBe(401);
  });
  it("D: needed permission -> 200", async () => {
    const r = await get(base, "/api/clusters", viaEnvoy(["mlstudio:clusters:view"]));
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ clusters: [] });
  });
  it("E: only another app's permission -> 403", async () => {
    expect((await get(base, "/api/clusters", viaEnvoy(["ucs:clusters:view", "mlstudio:clusters:edit"]))).status).toBe(403);
  });
  it("F: forged x-user-role / x-user-roles with admin values -> 403", async () => {
    const forged = viaEnvoy([], { "x-user-role": "ml-platform-admin", "x-user-roles": b64(["admin", "ml-platform-admin"]) });
    expect((await get(base, "/api/clusters", forged)).status).toBe(403);
  });
  it("G: public paths need nothing", async () => {
    delete process.env.TRUST_ENVOY_HEADERS;
    expect((await get(base, "/api/health")).status).toBe(200);
    expect((await get(base, "/static/app.js")).status).toBe(200);
  });
  it("/me returns the contract body", async () => {
    const r = await get(base, "/api/me", viaEnvoy(["b:x:view", "a:x:view"]));
    expect(r.body).toEqual({ user: "alice", userId: "u-123", tenant: "acme", permissions: ["a:x:view", "b:x:view"] });
  });
});

describe("requirePermission without gatewayAuth", () => {
  it("runs the gate itself", async () => {
    process.env.TRUST_ENVOY_HEADERS = "true";
    process.env.ENVOY_INTERNAL_SECRET = SECRET;
    const app = express();
    app.get("/x", requirePermission("a:x:view"), (_req, res) => res.json({}));
    const server = app.listen(0);
    const base = `http://127.0.0.1:${(server.address() as any).port}`;
    expect((await get(base, "/x")).status).toBe(401);
    expect((await get(base, "/x", viaEnvoy(["a:x:view"]))).status).toBe(200);
    expect((await get(base, "/x", viaEnvoy(["a:y:view"]))).status).toBe(403);
    server.close();
  });
});
