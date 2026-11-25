/*
 * FE-GAP-01: FEDERATED mode REQUIRES the host's apiBase. If it is missing, DataConnect renders a configuration error and builds
 * nothing — it never falls back to the development backend or a value left in localStorage. Standalone development keeps its
 * default. The tenant is NOT configuration: the backend takes it from the signed ticket and GET /me reports it, so a host prop
 * (or a stored value) can never choose it.
 */
import { mkdirSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { test, expect } from "@playwright/test";
import { build } from "esbuild";
import { STANDALONE_DEV_BASE, configurationErrorMessage, resolveRuntimeConfig } from "../../src/runtimeConfig";

const HOST = { apiBase: "/tunnel/dataconnect" };

test.describe("resolveRuntimeConfig — FEDERATED", () => {
  test("a host apiBase is used exactly (trimmed) and marks the backend host-controlled", () => {
    expect(resolveRuntimeConfig({ mode: "FEDERATED", apiBase: "  /tunnel/dataconnect " })).toEqual({
      ok: true, baseUrl: "/tunnel/dataconnect", hostControlled: true,
    });
  });

  test("a missing apiBase is a configuration error naming exactly what is missing", () => {
    expect(resolveRuntimeConfig({ mode: "FEDERATED" })).toEqual({ ok: false, missing: ["apiBase"] });
  });

  test("a host orgId (the old tenant prop) is ignored: the tenant is not configuration", () => {
    const withOrg = { mode: "FEDERATED", ...HOST, orgId: "org_other" } as unknown as Parameters<typeof resolveRuntimeConfig>[0];
    expect(resolveRuntimeConfig(withOrg)).toEqual({ ok: true, baseUrl: "/tunnel/dataconnect", hostControlled: true });
  });

  for (const blank of [undefined, null, "", "   ", "\n", 0, 7, {}, [], true]) {
    test(`${JSON.stringify(blank) ?? "undefined"} is "not provided"`, () => {
      expect(resolveRuntimeConfig({ mode: "FEDERATED", apiBase: blank })).toEqual({ ok: false, missing: ["apiBase"] });
    });
  }

  test("a value left in localStorage by a dev session never fills the gap", () => {
    expect(resolveRuntimeConfig({ mode: "FEDERATED", storedBase: "http://evil.example/api" })).toEqual({ ok: false, missing: ["apiBase"] });
  });

  test("the development backend is never returned", () => {
    for (const input of [{}, { apiBase: "" }, { storedBase: STANDALONE_DEV_BASE }]) {
      expect(JSON.stringify(resolveRuntimeConfig({ mode: "FEDERATED", ...input }))).not.toContain(STANDALONE_DEV_BASE);
    }
  });
});

test.describe("resolveRuntimeConfig — STANDALONE_DEV keeps its default", () => {
  test("with nothing supplied it uses the local development backend", () => {
    expect(resolveRuntimeConfig({ mode: "STANDALONE_DEV" })).toEqual({ ok: true, baseUrl: STANDALONE_DEV_BASE, hostControlled: false });
  });

  test("host props win over the stored value, which wins over the default", () => {
    expect(resolveRuntimeConfig({ mode: "STANDALONE_DEV", storedBase: "http://s/api" })).toEqual({
      ok: true, baseUrl: "http://s/api", hostControlled: false,
    });
    expect(resolveRuntimeConfig({ mode: "STANDALONE_DEV", ...HOST, storedBase: "http://s/api" })).toEqual({
      ok: true, baseUrl: "/tunnel/dataconnect", hostControlled: true,
    });
  });
});

// ── ConfigGate: what actually renders ─────────────────────────────────────────────────────────────────────────────────

// Playwright's own transform rewrites JSX in imported .tsx files into its component-testing format, which React cannot render. So the real
// ConfigGate source is bundled with esbuild (JSX compiled for React) and imported from there — what is tested is the shipped component.
type GateModule = { default: (props: Record<string, unknown>) => unknown };
let gate: GateModule;

function render(props: Record<string, unknown>) {
  const built: unknown[] = [];
  const html = renderToStaticMarkup(
    createElement(gate.default as never, { ...props, children: (config: unknown) => { built.push(config); return createElement("main", null, "APP"); } } as never),
  );
  return { html, built };
}

test.describe("ConfigGate", () => {
  test.beforeAll(async () => {
    const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
    const outdir = join(root, "node_modules", ".cache", "dc-unit-tests");
    mkdirSync(outdir, { recursive: true });
    const outfile = join(outdir, `ConfigGate.${process.pid}.mjs`);   // per worker process: parallel workers must not write the same file
    await build({
      entryPoints: [join(root, "src", "components", "ConfigGate.tsx")], outfile, bundle: true, format: "esm", platform: "node",
      jsx: "automatic", external: ["react", "react/jsx-runtime", "react/jsx-dev-runtime"], logLevel: "silent",
    });
    gate = (await import(`${outfile}?v=${Date.now()}`)) as GateModule;
  });

  test("FEDERATED without apiBase shows a configuration error and never builds the app", () => {
    const { html, built } = render({ mode: "FEDERATED", orgId: "org_acme" });

    expect(built).toEqual([]);                                       // the configured app (store, providers, pages) was not even constructed
    expect(html).toContain('role="alert"');
    expect(html).toContain("DataConnect is not configured");
    expect(html).toContain("<code>apiBase</code>");
    expect(html).not.toContain("APP");
    expect(html).not.toContain("localhost:8001");
  });

  test("FEDERATED ignores a stored dev value even when it is handed in", () => {
    const { html, built } = render({ mode: "FEDERATED", storedBase: "http://localhost:8001/x" });
    expect(built).toEqual([]);
    expect(html).toContain("DataConnect is not configured");
  });

  test("FEDERATED with a host apiBase renders the app with exactly that configuration", () => {
    const { html, built } = render({ mode: "FEDERATED", ...HOST });
    expect(html).toContain("APP");
    expect(html).not.toContain("DataConnect is not configured");
    expect(built).toEqual([{ ok: true, baseUrl: "/tunnel/dataconnect", hostControlled: true }]);
  });

  test("STANDALONE_DEV with no host still renders the app, on the development default", () => {
    const { html, built } = render({ mode: "STANDALONE_DEV" });
    expect(html).toContain("APP");
    expect(built).toEqual([{ ok: true, baseUrl: STANDALONE_DEV_BASE, hostControlled: false }]);
  });

  test("the message says what is missing and not the development values", () => {
    expect(configurationErrorMessage(["apiBase"])).toBe("DataConnect is not configured: the host did not provide apiBase.");
  });
});

// ── a guard so the dev defaults cannot be used anywhere else ──────────────────────────────────────────────────────────

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "types" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

test.describe("the development defaults stay in runtimeConfig.ts", () => {
  const files = sourceFiles(SRC);
  const rel = (file: string) => file.split("/src/")[1];

  test("the dev backend URL is written only in runtimeConfig.ts", () => {
    expect(files.filter((f) => readFileSync(f, "utf8").includes("localhost:8001")).map(rel)).toEqual(["runtimeConfig.ts"]);
  });

  test("no source file names a tenant: it only ever comes from GET /me", () => {
    expect(files.filter((f) => /["']org_[a-z0-9]+["']/.test(readFileSync(f, "utf8"))).map(rel)).toEqual([]);
    expect(files.filter((f) => /STANDALONE_DEV_BASE/.test(readFileSync(f, "utf8"))).map(rel)).toEqual(["runtimeConfig.ts"]);
  });

  test("AppProvider reaches the app only through ConfigGate", () => {
    const source = readFileSync(join(SRC, "context", "AppContext.tsx"), "utf8");
    expect(source).toContain("<ConfigGate mode={RUNTIME_MODE}");     // the build's real mode, not a literal
    expect(source).not.toMatch(/mode=\{["']STANDALONE_DEV["']/);
    expect(source).not.toMatch(/apiBase\s*\|\|/);
    expect(source).not.toMatch(/orgId\s*\|\|/);
  });
});
