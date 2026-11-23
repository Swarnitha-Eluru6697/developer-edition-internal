import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_CONTROL_PLANE_LOGIN_URL, controlPlaneLoginUrl, loadRuntimeConfig, runtimeConfig, sanitizeLoginUrl,
} from "../src/api/runtimeConfig";
import { redirectToControlPlaneLogin } from "../src/api/auth";

const serving = (body: unknown, ok = true) => (async () => ({ ok, json: async () => body }) as Response) as unknown as typeof fetch;

afterEach(() => { vi.unstubAllEnvs(); });

describe("W-006: the control-plane login URL is runtime configuration", () => {
  it("defaults to production when nothing is configured", async () => {
    await loadRuntimeConfig(serving({}, false));
    expect(runtimeConfig().controlPlaneLoginUrl).toBe(DEFAULT_CONTROL_PLANE_LOGIN_URL);
  });

  it("uses what /config.json says (a staging / private-cloud / white-label control plane)", async () => {
    await loadRuntimeConfig(serving({ controlPlaneLoginUrl: "https://login.staging.example.org/signin" }));
    expect(controlPlaneLoginUrl("https://ws.example/app")).toBe("https://login.staging.example.org/signin?next=https%3A%2F%2Fws.example%2Fapp");
  });

  it("the 401 redirect goes to the configured control plane, not the hard-coded one", async () => {
    await loadRuntimeConfig(serving({ controlPlaneLoginUrl: "https://login.eu.example.org/login" }));
    const loc = { href: "https://org.workspace.example/dbt" } as Location;
    vi.stubGlobal("window", { location: loc });
    redirectToControlPlaneLogin();
    expect(loc.href.startsWith("https://login.eu.example.org/login?next=")).toBe(true);
    expect(loc.href).not.toContain("www.app.inaiera.com");
    vi.unstubAllGlobals();
  });

  it("falls back to the build-time value when the file cannot be fetched", async () => {
    vi.stubEnv("VITE_CONTROL_PLANE_LOGIN_URL", "https://build.example.org/login");
    await loadRuntimeConfig((async () => { throw new Error("offline"); }) as unknown as typeof fetch);
    expect(runtimeConfig().controlPlaneLoginUrl).toBe("https://build.example.org/login");
  });

  it("the served file wins over the build-time value", async () => {
    vi.stubEnv("VITE_CONTROL_PLANE_LOGIN_URL", "https://build.example.org/login");
    await loadRuntimeConfig(serving({ controlPlaneLoginUrl: "https://served.example.org/login" }));
    expect(runtimeConfig().controlPlaneLoginUrl).toBe("https://served.example.org/login");
  });

  it.each([null, undefined, "", "   ", "/login", "login", "javascript:alert(1)", "data:text/html,x", "ftp://x/login", 42, {}])(
    "never accepts %p as a login URL", (bad) => { expect(sanitizeLoginUrl(bad)).toBeNull(); },
  );

  it("ignores a served file whose value is unsafe and keeps the default", async () => {
    await loadRuntimeConfig(serving({ controlPlaneLoginUrl: "javascript:alert(1)" }));
    expect(runtimeConfig().controlPlaneLoginUrl).toBe(DEFAULT_CONTROL_PLANE_LOGIN_URL);
  });

  it("a malformed body, or a hung server, never blocks start-up", async () => {
    await loadRuntimeConfig(serving("not an object"));
    expect(runtimeConfig().controlPlaneLoginUrl).toBe(DEFAULT_CONTROL_PLANE_LOGIN_URL);
    const hung = ((_u: string, init?: RequestInit) => new Promise((_res, rej) => init?.signal?.addEventListener("abort", () => rej(new Error("aborted"))))) as unknown as typeof fetch;
    const t0 = Date.now();
    await loadRuntimeConfig(hung, 50);
    expect(Date.now() - t0).toBeLessThan(1000);
    expect(runtimeConfig().controlPlaneLoginUrl).toBe(DEFAULT_CONTROL_PLANE_LOGIN_URL);
  });

  it("keeps an existing query string on the login URL and adds next", async () => {
    await loadRuntimeConfig(serving({ controlPlaneLoginUrl: "https://login.example.org/login?tenant=acme" }));
    const u = new URL(controlPlaneLoginUrl("https://ws/x"));
    expect(u.searchParams.get("tenant")).toBe("acme");
    expect(u.searchParams.get("next")).toBe("https://ws/x");
  });
});
