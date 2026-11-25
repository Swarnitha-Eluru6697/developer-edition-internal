import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { AddressInfo } from "net";
import type { Server } from "http";

// Contract section 6 acceptance tests (docs/auth/01-CONTRACT.md) against the
// real Express app: real gatewayAuth + requirePermission from
// @aadhyaanalyticsinc/gateway-auth, real routers. Only Postgres is stubbed —
// every query returns no rows, which is all a 200 on a list route needs.
vi.mock("../src/db/connection", () => ({
  pool: { query: vi.fn(async () => ({ rows: [], rowCount: 0 })), connect: vi.fn() },
  initSchema: vi.fn(),
}));

const SECRET = "test-gateway-secret";
process.env.TRUST_ENVOY_HEADERS = "true";
process.env.ENVOY_INTERNAL_SECRET = SECRET;

const b64 = (list: string[]) => Buffer.from(JSON.stringify(list)).toString("base64");

/** What Envoy sets for a verified user. */
function viaEnvoy(permissions: string[] | null, extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = {
    "x-inaiera-gateway-secret": SECRET,
    "x-user-name": "alice",
    "x-user-id": "3f9c-sub",
    "x-tenant-id": "acme",
    "x-user-groups": b64([]),
    ...extra,
  };
  if (permissions) h["x-user-permissions"] = b64(permissions);
  return h;
}

let server: Server;
let base = "";

beforeAll(async () => {
  const { app } = await import("../src/app");
  server = app.listen(0);
  await new Promise<void>((r) => server.once("listening", () => r()));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(() => new Promise<void>((r) => server.close(() => r())));

const get = (path: string, headers: Record<string, string> = {}) => fetch(base + path, { headers });

describe("contract section 6 — /api/models", () => {
  it("A. no gateway secret -> 401", async () => {
    expect((await get("/api/models")).status).toBe(401);
  });

  it("A. identity headers alone (bypassing Envoy) -> 401", async () => {
    const h = viaEnvoy(["mlstudio:model-catalog:view"]);
    delete h["x-inaiera-gateway-secret"];
    expect((await get("/api/models", h)).status).toBe(401);
  });

  it("B. wrong gateway secret -> 401", async () => {
    const res = await get("/api/models", viaEnvoy(["mlstudio:model-catalog:view"], { "x-inaiera-gateway-secret": "wrong" }));
    expect(res.status).toBe(401);
  });

  it("C. right secret, no identity -> 401", async () => {
    expect((await get("/api/models", { "x-inaiera-gateway-secret": SECRET })).status).toBe(401);
  });

  it("C. right secret + identity, no permission header -> 403", async () => {
    expect((await get("/api/models", viaEnvoy(null))).status).toBe(403);
  });

  it("D. right secret + identity + mlstudio:model-catalog:view -> 200", async () => {
    const res = await get("/api/models", viaEnvoy(["mlstudio:model-catalog:view"]));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([]);
  });

  it("E. only another app's permissions (ucs:*) -> 403", async () => {
    const res = await get("/api/models", viaEnvoy(["ucs:access:view", "ucs:rbac:manage", "ucs:catalogs:manage"]));
    expect(res.status).toBe(403);
  });

  it("F. forged x-user-role / x-user-roles with admin values, no permissions -> 403", async () => {
    const res = await get("/api/models", viaEnvoy(null, {
      "x-user-role": "ml-platform-admin",
      "x-user-roles": b64(["ml-platform-admin", "admin", "platform-admin"]),
    }));
    expect(res.status).toBe(403);
  });

  it("F. forged roles also give nothing when TRUST_ENVOY_HEADERS is off -> 401", async () => {
    process.env.TRUST_ENVOY_HEADERS = "false";
    try {
      expect((await get("/api/models", viaEnvoy(["mlstudio:model-catalog:view"], { "x-user-role": "ml-platform-admin" }))).status).toBe(401);
    } finally {
      process.env.TRUST_ENVOY_HEADERS = "true";
    }
  });

  it("G. /api/health without anything -> 200", async () => {
    expect((await get("/api/health")).status).toBe(200);
  });
});

describe("clusters API", () => {
  for (const path of ["/api/cluster/telemetry", "/api/compute-pools"]) {
    it(`${path}: with mlstudio:clusters:view -> 200`, async () => {
      expect((await get(path, viaEnvoy(["mlstudio:clusters:view"]))).status).toBe(200);
    });
    it(`${path}: without it (other ML Studio permissions) -> 403`, async () => {
      expect((await get(path, viaEnvoy(["mlstudio:access:view", "mlstudio:dashboard:view", "mlstudio:model-catalog:view"]))).status).toBe(403);
    });
    it(`${path}: no secret -> 401`, async () => {
      expect((await get(path)).status).toBe(401);
    });
  }
});

describe("/api/me and /api/whoami", () => {
  for (const path of ["/api/me", "/api/whoami"]) {
    it(`${path} returns the principal's identity and permissions`, async () => {
      const res = await get(path, viaEnvoy(["mlstudio:train:run", "mlstudio:access:view"], { "x-user-roles": "ml-platform-admin" }));
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({
        user: "alice", userId: "3f9c-sub", tenant: "acme",
        permissions: ["mlstudio:access:view", "mlstudio:train:run"],
      });
    });
    it(`${path} without the gateway -> 401`, async () => {
      expect((await get(path)).status).toBe(401);
    });
  }
});

describe("write actions are permission-gated before any work", () => {
  const post = (path: string, headers: Record<string, string>, body: unknown = {}) =>
    fetch(base + path, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) });

  it("POST /api/training-runs without mlstudio:train:run -> 403", async () => {
    expect((await post("/api/training-runs", viaEnvoy(["mlstudio:model-catalog:view"]))).status).toBe(403);
  });
  it("POST /api/deployments/:id/promote without mlstudio:deploy:promote -> 403", async () => {
    expect((await post("/api/deployments/m1/promote", viaEnvoy(["mlstudio:train:run"]), { version: 1 })).status).toBe(403);
  });
  it("POST /api/alert-policies with only mlstudio:alerts:view -> 403", async () => {
    expect((await post("/api/alert-policies", viaEnvoy(["mlstudio:alerts:view"]))).status).toBe(403);
  });
  it("POST /api/base-models/sync-from-ucs without mlstudio:base-models:sync -> 403", async () => {
    expect((await post("/api/base-models/sync-from-ucs", viaEnvoy(["mlstudio:train:run"]))).status).toBe(403);
  });
  it("POST /oauth/token without the gateway -> 401", async () => {
    expect((await post("/oauth/token", {}, { grant_type: "client_credentials" })).status).toBe(401);
  });
});

describe("permissions.json", () => {
  it("declares every permission ID the code checks", async () => {
    const { PERM } = await import("../src/middleware/permissions");
    const declared = new Set((require("../permissions.json").permissions as { id: string }[]).map((p) => p.id));
    for (const id of Object.values(PERM)) expect(declared.has(id), id).toBe(true);
  });
});
