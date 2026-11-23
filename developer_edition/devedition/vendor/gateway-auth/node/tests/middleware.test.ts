import { describe, it, expect, beforeEach } from "vitest";
import express from "express";
import http from "http";
import { createGatewayMiddleware } from "../src/middleware";

// Two DIFFERENT services' role vocabularies, deliberately dissimilar, to
// prove the middleware is genuinely generic and not secretly ML-Studio-shaped.
const ML_STUDIO_ROLES = {
  allRoles: ["ml-viewer", "ml-engineer", "ml-platform-admin"] as const,
  tiers: { "ml-viewer": 0, "ml-engineer": 1, "ml-platform-admin": 2 },
  defaultRole: "ml-viewer" as const,
};
const WORKSPACE_ROLES = {
  allRoles: ["workspace-guest", "workspace-member", "workspace-owner"] as const,
  tiers: { "workspace-guest": 0, "workspace-member": 1, "workspace-owner": 2 },
  defaultRole: "workspace-guest" as const,
};

function startApp(opts: Parameters<typeof createGatewayMiddleware>[0]) {
  const app = express();
  app.use(createGatewayMiddleware(opts));
  app.get("/whoami", (req, res) => res.json({ role: req.role, identity: req.gatewayIdentity }));
  const server = app.listen(0);
  const port = (server.address() as any).port;
  return { server, base: `http://127.0.0.1:${port}` };
}

function get(base: string, headers: Record<string, string>): Promise<{ status: number; body: any }> {
  return new Promise((resolve) => {
    http.get(base + "/whoami", { headers }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode || 0, body: JSON.parse(data) }));
    });
  });
}

describe("createGatewayMiddleware", () => {
  let server: http.Server;
  let base: string;

  beforeEach(() => {
    ({ server, base } = startApp({ trustEnvoyHeaders: true, gatewaySecret: "real-secret", roles: ML_STUDIO_ROLES }));
  });

  it("rejects when TRUST_ENVOY_HEADERS is off, even with everything else correct", async () => {
    const local = startApp({ trustEnvoyHeaders: false, gatewaySecret: "real-secret", roles: ML_STUDIO_ROLES });
    const r = await get(local.base, { "x-user-name": "alice", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(401);
    local.server.close();
  });

  it("rejects a missing gateway secret", async () => {
    const r = await get(base, { "x-user-name": "alice", "x-user-roles": "ml-platform-admin" });
    expect(r.status).toBe(401);
    expect(r.body.error).toMatch(/did not come through the gateway/);
  });

  it("rejects a wrong gateway secret", async () => {
    const r = await get(base, { "x-user-name": "alice", "x-inaiera-gateway-secret": "wrong" });
    expect(r.status).toBe(401);
  });

  it("rejects a missing username even with a correct secret", async () => {
    const r = await get(base, { "x-user-roles": "ml-platform-admin", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(401);
    expect(r.body.error).toMatch(/missing identity headers/);
  });

  it("accepts a correct secret and resolves a single matching role", async () => {
    const r = await get(base, { "x-user-name": "alice", "x-user-roles": "ml-engineer", "x-tenant-id": "org_acme", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("ml-engineer");
    expect(r.body.identity.username).toBe("alice");
    expect(r.body.identity.tenantId).toBe("org_acme");
  });

  it("picks the recognized role out of a mixed list including unrelated products' roles", async () => {
    const r = await get(base, { "x-user-name": "bob", "x-user-roles": "some-other-product-role,ml-viewer,another-role", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("ml-viewer");
  });

  it("resolves to the HIGHEST tier when a user has more than one recognized role", async () => {
    const r = await get(base, { "x-user-name": "carol", "x-user-roles": "ml-viewer,ml-platform-admin", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("ml-platform-admin");
  });

  it("defaults to the configured least-privileged role when nothing matches, and does not reject", async () => {
    const r = await get(base, { "x-user-name": "dave", "x-user-roles": "totally-unrelated", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("ml-viewer");
  });

  it("splits and trims X-User-Groups correctly", async () => {
    const r = await get(base, { "x-user-name": "erin", "x-user-groups": "eng, data-stewards ,ml-team", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.identity.groups).toEqual(["eng", "data-stewards", "ml-team"]);
  });

  it("falls back to the configured default tenant when X-Tenant-Id is absent", async () => {
    const local = startApp({ trustEnvoyHeaders: true, gatewaySecret: "real-secret", roles: ML_STUDIO_ROLES, defaultTenantId: "ml_models" });
    const r = await get(local.base, { "x-user-name": "frank", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.body.identity.tenantId).toBe("ml_models");
    local.server.close();
  });

  it("is genuinely generic: a second service with an entirely different role vocabulary works identically", async () => {
    const local = startApp({ trustEnvoyHeaders: true, gatewaySecret: "real-secret", roles: WORKSPACE_ROLES });
    const r = await get(local.base, { "x-user-name": "grace", "x-user-roles": "workspace-owner,ml-viewer", "x-inaiera-gateway-secret": "real-secret" });
    // ml-viewer isn't in WORKSPACE_ROLES at all -- must be ignored, not error
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("workspace-owner");
    local.server.close();
  });

  it("environment-variable defaults work when options are omitted", async () => {
    process.env.TRUST_ENVOY_HEADERS = "true";
    process.env.ENVOY_INTERNAL_SECRET = "env-secret";
    const local = startApp({ roles: ML_STUDIO_ROLES });
    const r = await get(local.base, { "x-user-name": "henry", "x-inaiera-gateway-secret": "env-secret" });
    expect(r.status).toBe(200);
    local.server.close();
    delete process.env.TRUST_ENVOY_HEADERS;
    delete process.env.ENVOY_INTERNAL_SECRET;
  });

  it("never crashes on empty header values", async () => {
    const r = await get(base, { "x-user-name": "iris", "x-user-roles": "", "x-user-groups": "", "x-inaiera-gateway-secret": "real-secret" });
    expect(r.status).toBe(200);
    expect(r.body.role).toBe("ml-viewer");
    expect(r.body.identity.groups).toEqual([]);
  });
});
