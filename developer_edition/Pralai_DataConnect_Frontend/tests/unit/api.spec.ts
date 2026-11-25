/*
 * API client unit tests — plain Node-context Playwright tests (no browser):
 * createApi() is exercised directly against a stubbed global.fetch, the way
 * the client would be used inside RTK Query's queryFn (see src/store/apiSlice.ts).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { createApi } from "../../src/api";
import type { ConnectionCreate } from "../../src/types/api";

interface FetchCall { url: string; init: RequestInit }

function stubFetch(status: number, body?: unknown): FetchCall[] {
  const calls: FetchCall[] = [];
  (globalThis as unknown as { fetch: typeof fetch }).fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      text: async () => (body === undefined ? "" : JSON.stringify(body)),
    } as Response;
  }) as typeof fetch;
  return calls;
}

function headerOf(call: FetchCall, name: string): string | undefined {
  return (call.init.headers as Record<string, string>)[name];
}

const CONN_PAYLOAD: ConnectionCreate = { name: "n", source_type: "postgres", connection_config: {} };

/** Sequenced statuses across successive fetch() calls — e.g. [503, 503, 200] for "fails twice, then succeeds". */
function stubFetchSequence(steps: { status: number; body?: unknown }[]): FetchCall[] {
  const calls: FetchCall[] = [];
  (globalThis as unknown as { fetch: typeof fetch }).fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const i = Math.min(calls.length, steps.length - 1);
    calls.push({ url: String(input), init });
    const step = steps[i]!;
    return {
      ok: step.status >= 200 && step.status < 300,
      status: step.status,
      statusText: "",
      text: async () => (step.body === undefined ? "" : JSON.stringify(step.body)),
    } as Response;
  }) as typeof fetch;
  return calls;
}

function stubFetchAlwaysFailing(status: number): FetchCall[] {
  return stubFetchSequence(Array.from({ length: 10 }, () => ({ status, body: { detail: "upstream unavailable" } })));
}

test.describe("api client (src/api.ts)", () => {
  test("sends X-Request-Id and NO identity, tenant or role header (Envoy sets those from the ticket)", async () => {
    const calls = stubFetch(200, []);
    const api = createApi({ baseUrl: "http://api.test" });
    await api.connections.list();
    await api.health();

    expect(calls).toHaveLength(2);
    for (const call of calls) {
      expect(headerOf(call, "X-Request-Id")).toBeTruthy();
      const names = Object.keys(call.init.headers as Record<string, string>).map((h) => h.toLowerCase());
      for (const forbidden of ["x-org-id", "x-actor", "x-user-role", "x-user-roles", "x-user-permissions", "x-user-name", "x-tenant-id"]) {
        expect(names, forbidden).not.toContain(forbidden);
      }
    }
  });

  test("me() reads GET /me", async () => {
    const calls = stubFetch(200, { user: "alice", userId: "u1", tenant: "org_1", permissions: ["dataconnect:access:view"] });
    const me = await createApi({ baseUrl: "http://api.test" }).me();

    expect(calls[0].url).toBe("http://api.test/me");
    expect(calls[0].init.method).toBe("GET");
    expect(me.permissions).toEqual(["dataconnect:access:view"]);
  });

  test("an untyped caller passing the old identity options sends none of them", async () => {
    const calls = stubFetch(200, []);
    const legacy = { baseUrl: "http://api.test", orgId: "org_1", role: "DataConnect Admin", actor: "mallory" } as unknown as Parameters<typeof createApi>[0];
    await createApi(legacy).connections.list();

    expect(Object.keys(calls[0].init.headers as Record<string, string>)).toEqual(["X-Request-Id"]);
  });

  test("serializes the request body as JSON and uses the right HTTP method", async () => {
    const calls = stubFetch(201, { id: "c1", org_id: "org_1", ...CONN_PAYLOAD, status: "draft" });
    const api = createApi({ baseUrl: "http://api.test" });
    await api.connections.create(CONN_PAYLOAD);

    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].url).toBe("http://api.test/connections");
    expect(JSON.parse(String(calls[0].init.body))).toEqual(CONN_PAYLOAD);
  });

  test("rejects with an ApiError carrying the backend's detail message on a non-2xx response", async () => {
    // The pre-standard-envelope shape ({ detail: "..." }) — kept working as
    // a fallback (see isErrorEnvelope() in api.ts) for anything that isn't
    // the full ErrorEnvelope, not just historical compatibility.
    stubFetch(403, { detail: "missing permission dataconnect:connections:edit — not permitted" });
    const api = createApi({ baseUrl: "http://api.test" });

    await expect(api.connections.create(CONN_PAYLOAD)).rejects.toMatchObject({
      status: 403,
      message: expect.stringContaining("not permitted"),
      code: undefined,
      retryable: false,
    });
  });

  test.describe("standard error envelope (app/schemas/errors.py's ErrorEnvelope)", () => {
    test("parses code, message, retryable and requestId", async () => {
      stubFetch(502, {
        code: "kafka_connect_error",
        message: "Kafka Connect error (503): unavailable",
        field_errors: null,
        request_id: "req-123",
        retryable: true,
        details: { connect_status_code: 503 },
      });
      const api = createApi({ baseUrl: "http://api.test" });

      await expect(api.pipelines.deploy("pipe-0001")).rejects.toMatchObject({
        status: 502,
        code: "kafka_connect_error",
        message: "Kafka Connect error (503): unavailable",
        requestId: "req-123",
        retryable: true,
        details: { connect_status_code: 503 },
      });
    });

    test("parses field_errors for a validation-shaped failure", async () => {
      stubFetch(422, {
        code: "connector_config_error",
        message: "'username' is required for source type 'postgres'",
        field_errors: [
          { field: "connection_config.username", message: "'username' is required for source type 'postgres'" },
        ],
        request_id: null,
        retryable: false,
        details: { source_type: "postgres" },
      });
      const api = createApi({ baseUrl: "http://api.test" });

      let caught: unknown;
      try {
        await api.connections.create(CONN_PAYLOAD);
      } catch (e) {
        caught = e;
      }
      expect((caught as { fieldErrors?: { field: string }[] }).fieldErrors).toEqual([
        { field: "connection_config.username", message: "'username' is required for source type 'postgres'" },
      ]);
    });

    test("null field_errors/request_id/details become undefined, not null, on the ApiError", async () => {
      stubFetch(500, {
        code: "internal_error", message: "Internal server error",
        field_errors: null, request_id: null, retryable: true, details: null,
      });
      const api = createApi({ baseUrl: "http://api.test" });

      let caught: unknown;
      try {
        await api.connections.list();
      } catch (e) {
        caught = e;
      }
      const err = caught as { fieldErrors?: unknown; requestId?: unknown; details?: unknown };
      expect(err.fieldErrors).toBeUndefined();
      expect(err.requestId).toBeUndefined();
      expect(err.details).toBeUndefined();
    });
  });

  test("rejects with ApiError(0, ...) when the network request itself fails", async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => {
      throw new Error("boom");
    }) as typeof fetch;
    const api = createApi({ baseUrl: "http://api.test" });

    await expect(api.connections.list()).rejects.toMatchObject({ status: 0 });
  });

  test("reports write telemetry via onOperation on success", async () => {
    // describe()'s id heuristic (see src/api.ts) only treats a path segment as
    // a resource id when it's 4+ chars, to avoid false-positiving on short
    // literal segments — use a realistic (UUID-shaped) id here, not "c1".
    stubFetch(204, undefined);
    const ops: { status: string; resource?: string; action?: string; id?: string }[] = [];
    const api = createApi({ baseUrl: "http://api.test", onOperation: (op) => ops.push(op) });
    await api.connections.remove("conn-0001");

    expect(ops).toHaveLength(1);
    expect(ops[0]).toMatchObject({ status: "success", resource: "connections", action: "deleted", id: "conn-0001" });
  });

  test("reports write telemetry via onOperation on failure", async () => {
    stubFetch(500, { detail: "boom" });
    const ops: { status: string; httpStatus?: number }[] = [];
    const api = createApi({ baseUrl: "http://api.test", onOperation: (op) => ops.push(op) });
    await expect(api.connections.remove("conn-0001")).rejects.toBeTruthy();

    expect(ops).toHaveLength(1);
    expect(ops[0]).toMatchObject({ status: "error", httpStatus: 500 });
  });

  test("forwards the caller's signal straight to fetch()", async () => {
    const calls = stubFetch(200, []);
    const api = createApi({ baseUrl: "http://api.test" });
    const controller = new AbortController();

    await api.connections.list(controller.signal);

    expect(calls[0].init.signal).toBe(controller.signal);
  });

  test("aborting the signal cancels the in-flight fetch and rejects with the raw AbortError, not a wrapped ApiError", async () => {
    let capturedSignal: AbortSignal | undefined;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = ((_input: RequestInfo | URL, init: RequestInit = {}) => {
      capturedSignal = init.signal ?? undefined;
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
      });
    }) as typeof fetch;
    const api = createApi({ baseUrl: "http://api.test" });
    const controller = new AbortController();

    const pending = api.connections.list(controller.signal);
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(capturedSignal).toBe(controller.signal);
  });

  test("a cancelled write is never reported as a failed operation", async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = ((_input: RequestInfo | URL, init: RequestInit = {}) => {
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
      });
    }) as typeof fetch;
    const ops: unknown[] = [];
    const api = createApi({ baseUrl: "http://api.test", onOperation: (op) => ops.push(op) });
    const controller = new AbortController();

    const pending = api.connections.remove("conn-0001", controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });

    // A deliberate cancellation (unmount, superseded request) is not a
    // network failure — it must never surface as an "error" toast/telemetry
    // event the way a genuine failed write would.
    expect(ops).toHaveLength(0);
  });

  // Math.random is stubbed to 0 in most of these — backoffDelay() becomes
  // `0 * cap` — so they exercise the retry *logic* without waiting out real
  // backoff delays.
  test.describe("retry / backoff", () => {
    test("retries an idempotent GET on 503 and returns the eventual success", async () => {
      const originalRandom = Math.random;
      Math.random = () => 0;
      try {
        const calls = stubFetchSequence([{ status: 503 }, { status: 200, body: [] }]);
        const api = createApi({ baseUrl: "http://api.test" });

        const result = await api.connections.list();

        expect(result).toEqual([]);
        expect(calls).toHaveLength(2);
      } finally {
        Math.random = originalRandom;
      }
    });

    test("gives up after exhausting retries and throws the final error", async () => {
      const originalRandom = Math.random;
      Math.random = () => 0;
      try {
        const calls = stubFetchAlwaysFailing(503);
        const api = createApi({ baseUrl: "http://api.test" });

        await expect(api.connections.list()).rejects.toMatchObject({ status: 503 });
        // 1 initial attempt + 2 retries = 3 total.
        expect(calls).toHaveLength(3);
      } finally {
        Math.random = originalRandom;
      }
    });

    test("retries idempotent writes (PUT, DELETE) on a transient upstream failure", async () => {
      const originalRandom = Math.random;
      Math.random = () => 0;
      try {
        const calls = stubFetchSequence([{ status: 502 }, { status: 204 }]);
        const api = createApi({ baseUrl: "http://api.test" });

        await api.connections.remove("conn-0001");

        expect(calls).toHaveLength(2);
        expect(calls[0]!.init.method).toBe("DELETE");
      } finally {
        Math.random = originalRandom;
      }
    });

    test("never retries a POST (not idempotent) on a transient upstream failure", async () => {
      const calls = stubFetchAlwaysFailing(503);
      const api = createApi({ baseUrl: "http://api.test" });

      await expect(api.connections.create(CONN_PAYLOAD)).rejects.toMatchObject({ status: 503 });

      expect(calls).toHaveLength(1);
    });

    test("never retries a PATCH (not idempotent) on a transient upstream failure", async () => {
      const calls = stubFetchAlwaysFailing(503);
      const api = createApi({ baseUrl: "http://api.test" });

      await expect(api.pipelines.update("pipe-0001", {})).rejects.toMatchObject({ status: 503 });

      expect(calls).toHaveLength(1);
    });

    test("never retries a plain 500 or a 4xx, even for an idempotent GET", async () => {
      const calls500 = stubFetchAlwaysFailing(500);
      const api = createApi({ baseUrl: "http://api.test" });
      await expect(api.connections.list()).rejects.toMatchObject({ status: 500 });
      expect(calls500).toHaveLength(1);

      const calls404 = stubFetchAlwaysFailing(404);
      await expect(api.connections.get("conn-missing")).rejects.toMatchObject({ status: 404 });
      expect(calls404).toHaveLength(1);
    });

    test("retries a GET after a network-level failure, not just a bad status", async () => {
      const originalRandom = Math.random;
      Math.random = () => 0;
      try {
        let attempt = 0;
        (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => {
          attempt += 1;
          if (attempt === 1) throw new TypeError("Failed to fetch");
          return { ok: true, status: 200, statusText: "", text: async () => "[]" } as Response;
        }) as typeof fetch;
        const api = createApi({ baseUrl: "http://api.test" });

        const result = await api.connections.list();

        expect(result).toEqual([]);
        expect(attempt).toBe(2);
      } finally {
        Math.random = originalRandom;
      }
    });

    test("aborting during a retry backoff stops retrying immediately", async () => {
      const calls = stubFetchAlwaysFailing(503);
      const api = createApi({ baseUrl: "http://api.test" });
      const controller = new AbortController();

      const pending = api.connections.list(controller.signal);
      // Let the first (failing) attempt land, then abort while the retry is
      // sleeping in backoff rather than in-flight — this exercises the
      // sleep()-side abort path, not the fetch()-side one already covered above.
      await Promise.resolve();
      controller.abort();

      await expect(pending).rejects.toMatchObject({ name: "AbortError" });
      expect(calls.length).toBeLessThan(3);
    });
  });
});

test.describe("tenant context (found by the live E2E, FE-04)", () => {
  test("schema inference is tenant-scoped on the backend, from the ticket — the client sends no tenant", async () => {
    const calls = stubFetch(200, { fields: [], confidence: 1, sample_size: 0 });
    await createApi({ baseUrl: "http://api" }).schemas.infer([{ id: 1 }]);
    expect(calls[0]!.url).toBe("http://api/schemas/infer");
    expect(headerOf(calls[0]!, "X-Org-Id")).toBeUndefined();
  });

  test("no route in the client opts in or out of a tenant: there is no per-request tenant at all", () => {
    const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "api.ts"), "utf8");
    expect(source).not.toMatch(/org:\s*(true|false)/);
    expect(source).not.toContain("X-Org-Id");
  });
});
