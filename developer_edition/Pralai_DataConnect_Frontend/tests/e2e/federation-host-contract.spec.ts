/*
 * Module Federation HOST contract tests.
 *
 * federation-mount.spec.ts already covers this repo's remote-side half of
 * the contract (the exposed component renders standalone; a build emits a
 * loadable remoteEntry.js) and explains why a *real* cross-app mount can't
 * be driven from here — the Control Portal host doesn't live in this repo.
 *
 * What CAN be driven from here, and wasn't before this file: a synthetic
 * host page, built the same way any Module Federation host is (dynamic
 * `import()` of remoteEntry.js, `init(shareScope)`, `get('./DataConnectApp')`),
 * exercising the actual production build's federation runtime rather than
 * grepping for strings in it. It runs against a plain static file server
 * (not the Vite dev server other e2e specs use — dev mode never emits
 * remoteEntry.js), so every scenario below is the real bundle a host would
 * fetch.
 */
import { createServer, type Server } from "node:http";
import { readFile, rm } from "node:fs/promises";
import { readdirSync } from "node:fs";
import { execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect, type Page } from "@playwright/test";
import { meFor, mockBackend, PERSONAS } from "./fixtures";
import { PERMISSION_IDS } from "../../src/types/permissions.generated";

const dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(dirname, "../..");
// A dedicated, per-worker outDir — not the shared "dist" federation-mount.spec.ts
// also builds into, and not one fixed path either: Playwright's own test
// runner shards this file's tests across several worker processes, each
// running its own beforeAll, so a single shared path here raced two
// concurrent `vite build` runs against the same directory just as surely as
// sharing "dist" would (ENOENT mid-build, "failed to fetch" for a chunk
// deleted out from under an in-flight import()).
const distDir = path.join(root, `dist-federation-host-contract-test-${process.env.TEST_WORKER_INDEX ?? "0"}`);

const CONTENT_TYPES: Record<string, string> = {
  ".js": "application/javascript",
  ".css": "text/css",
  ".html": "text/html",
};

/*
 * The synthetic host page. Every scenario shares this shell and branches on
 * ?scenario=; it plays the role of "the Control Portal's federation wiring"
 * — the part of the contract that lives outside this repo.
 */
const HOST_HTML = `<!doctype html>
<html><body>
  <div id="mount-a"></div>
  <div id="mount-b"></div>
  <div id="mount-c"></div>
  <script type="module" src="/host.js"></script>
</body></html>`;

// The federation plugin's shared-react stub chunks are content-hashed, and
// that hash isn't a stable literal across builds — it shifts with anything
// that changes the stub's own bytes (minify on/off, a plugin version bump,
// ...), independent of this app's own source. Discovered from the actual
// build output (see resolveSharedChunkNames() in beforeAll) and interpolated
// below, rather than hardcoded, so this suite verifies the real contract
// (the shared scope gets used) instead of one build's incidental filenames.
function buildHostJs(reactChunk: string, reactDomChunk: string): string {
  return `
const result = { log: [] };
window.__result__ = result;
const scenario = new URLSearchParams(location.search).get("scenario");

async function loadRemote() {
  return import("/assets/remoteEntry.js");
}

// A stand-in for "the host's own copy of react/react-dom", registered into
// the shared scope the way a host built with the same federation plugin
// would. wrapping each in a call-counting get() lets the assertions prove
// the remote actually pulled this module through the shared scope, not its
// own bundled fallback.
async function hostSharedScope() {
  const reactMod = await import("/assets/${reactChunk}");
  const domMod = await import("/assets/${reactDomChunk}");
  const calls = { react: 0, "react-dom": 0 };
  result.sharedCalls = calls;
  // versionValue.get() must itself return a function that, when awaited and
  // invoked, resolves the module (get() -> factory -> module) — the same
  // curried shape moduleMap uses internally (see remoteEntry.js's own
  // get/init). A get() that resolves the module directly is a shape
  // mismatch the runtime will fail on ("factory is not a function").
  return {
    default: {
      react: { "18.3.1": { get: () => () => { calls.react++; return Promise.resolve(reactMod); }, loaded: 0 } },
      "react-dom": { "18.3.1": { get: () => () => { calls["react-dom"]++; return Promise.resolve(domMod); }, loaded: 0 } },
    },
  };
}

// get('./DataConnectApp') resolves to the component directly only when the
// built chunk's export set is an exact subset of {Module, __esModule,
// default, _export_sfc} (see remoteEntry.js's moduleMap). In this build the
// expose chunk also re-exports an incidental "j" (jsxRuntimeExports, pulled
// in by Rollup's chunking) alongside "default", so that check fails and
// get() hands back the raw module namespace instead — a real host has to
// unwrap ".default" itself rather than assume the factory always returns
// the component. Documented here because it's the actual contract, not the
// idealized one.
function resolveComponent(factoryResult) {
  return typeof factoryResult === "function" ? factoryResult : factoryResult.default;
}

// FE-GAP-01: apiBase is REQUIRED from a host in a federated build — without it DataConnect renders a configuration
// error instead of falling back to a development backend. A real host always supplies it, so by default this one does; the
// "config-missing" scenario below passes it as undefined on purpose to prove the failure.
const DEFAULT_API_BASE = location.origin + "/api/v1/dataconnect";

async function mountInto(elId, hostProps) {
  const props = { apiBase: DEFAULT_API_BASE, ...hostProps };
  const remote = await loadRemote();
  await remote.init((await hostSharedScope()).default);
  const factory = await remote.get("./DataConnectApp");
  const Comp = resolveComponent(factory());
  const React = (await import("/assets/${reactChunk}")).default;
  const ReactDOM = (await import("/assets/${reactDomChunk}")).default;
  // Node props (roleBadges, accountMenu) need the host's React, so the scenarios name them and they are built here.
  if (props.roleBadgeNames) {
    props.roleBadges = React.createElement("div", { className: "host-role-badges" },
      ...props.roleBadgeNames.map((name) => React.createElement("span", { key: name, className: "host-role-badge" }, name)));
    delete props.roleBadgeNames;
  }
  if (props.accountMenuLabel) {
    props.accountMenu = React.createElement("button", { type: "button", className: "host-account-menu" }, props.accountMenuLabel);
    delete props.accountMenuLabel;
  }
  const root = ReactDOM.createRoot(document.getElementById(elId));
  root.render(React.createElement(React.StrictMode, null, React.createElement(Comp, props)));
  return { Comp, root };
}

try {
  if (scenario === "basic") {
    const remote = await loadRemote();
    result.hasGet = typeof remote.get === "function";
    result.hasInit = typeof remote.init === "function";
    const factory = await remote.get("./DataConnectApp");
    result.exposedType = typeof resolveComponent(factory());
  }

  if (scenario === "shared") {
    await mountInto("mount-a", {});
  }

  if (scenario === "css") {
    await mountInto("mount-a", {});
  }

  if (scenario === "props") {
    window.__ops__ = [];
    await mountInto("mount-a", {
      // What the user may do comes from GET /me (the mock answers with every permission), never from a host prop.
      onOperation: (op) => window.__ops__.push(op),
    });
  }

  if (scenario === "embedded") {
    await mountInto("mount-a", { embedded: true });
  }

  // Portal chrome contract: a current portal passes roleBadges (display only) and keeps the bell + profile in its own rail.
  // mount-a is hosted that way, mount-b is a plain (standalone-style) mount, mount-c is an older portal still passing accountMenu.
  if (scenario === "portal-chrome") {
    await mountInto("mount-a", { roleBadgeNames: ["DataConnect Admin", "Data Engineer", "Data Steward", "DataConnect Viewer"] });
    await mountInto("mount-b", {});
    await mountInto("mount-c", { accountMenuLabel: "Old portal menu" });
  }

  // FE-BUG-22: apiBase is host-supplied, and
  // nothing else in this file proves it's actually used for outbound
  // requests rather than silently falling back to the dev-only DEV_BASE
  // (AppContext.tsx). The extra "/tenant-x" path segment (still ending in
  // .../api/v1/dataconnect so mockBackend's existing route glob keeps
  // handling it) is what the Playwright test below greps the captured
  // request URL for.
  if (scenario === "apibase") {
    await mountInto("mount-a", { apiBase: \`\${location.origin}/tenant-x/api/v1/dataconnect\` });
  }

  // Identity props a host might still pass (the old role / user / orgId) are ignored: who the user is, their tenant and their
  // permissions come from GET /me. mount-a passes privileged-looking values, mount-b passes none.
  if (scenario === "host-identity-ignored") {
    await mountInto("mount-a", { orgId: "org_from_host", role: "DataConnect Admin", user: "Host Says Admin" });
    await mountInto("mount-b", {});
  }

  // FE-GAP-01: a host that forgets apiBase. Each bad mount must render a configuration error, build nothing, and make NO
  // request — least of all to the development backend. The values are passed as explicit undefined/blank so the default above
  // does not fill them in.
  if (scenario === "config-missing") {
    window.__errors__ = [];
    const onError = (e) => window.__errors__.push(String(e && e.message || e));
    await mountInto("mount-a", { apiBase: undefined, onError });                                // no apiBase
    await mountInto("mount-b", { apiBase: "", onError });                                       // empty
    await mountInto("mount-c", { apiBase: "   ", onError });                                    // blank, not just absent
  }

  // "View as role": the portal passes the permission IDs of the role picked in its role strip (?view= is that list as JSON; absent =
  // undefined). window.__view__(list | null | undefined) re-renders the SAME root with a new viewPermissions, the way the portal does
  // when the user switches — no unmount, no new root. mount-b passes null explicitly, the "All roles" choice.
  if (scenario === "view-as") {
    const raw = new URLSearchParams(location.search).get("view");
    const initial = raw === null ? undefined : JSON.parse(raw);
    const { Comp, root } = await mountInto("mount-a", { roleBadgeNames: ["Role A", "Role B"], viewPermissions: initial });
    const React = (await import("/assets/${reactChunk}")).default;
    const badges = React.createElement("div", { className: "host-role-badges" }, "Role A", "Role B");
    window.__view__ = (viewPermissions) => root.render(React.createElement(React.StrictMode, null,
      React.createElement(Comp, { apiBase: DEFAULT_API_BASE, roleBadges: badges, viewPermissions })));
    await mountInto("mount-b", { viewPermissions: null });
  }

  if (scenario === "route") {
    await mountInto("mount-a", {});
    await mountInto("mount-b", {});
  }

  // FE-BUG-22: "route" above proves the two mounts' page-nav useState isn't
  // shared, but that's true of any two React trees and says nothing about
  // Redux specifically — config/toasts (see AppContext.tsx's file header)
  // are the only state that actually lives in this app's store. Two mounts
  // each getting its own createDataConnectStore() call
  // (AppContext.tsx), is what the test below drives a toast through to
  // prove one mount's dispatch never reaches the other's subscribers.
  if (scenario === "redux-isolation") {
    await mountInto("mount-a", {});
    await mountInto("mount-b", {});
  }

  // FE-BUG-22: CSS isolation, both directions. The host owns some elements and
  // (with ?hostile=1) a stylesheet of typical sloppy global rules. We record
  // computed styles of (a) the HOST's own probe elements before and after the
  // remote mounts — the remote must not restyle them — and (b) a few of the
  // REMOTE's elements, which the test compares between a clean host and a
  // hostile one — the host must not restyle the remote.
  if (scenario === "css-isolation") {
    const params = new URLSearchParams(location.search);
    if (params.get("hostile") === "1") {
      const hostile = document.createElement("style");
      hostile.textContent = \`
        body { font-family: "Comic Sans MS", cursive; line-height: 3; color: purple; }
        button { background: hotpink; border: 3px dashed purple; border-radius: 40px; padding: 20px; font-size: 30px; color: yellow; }
        input, select { height: 80px; border: 4px solid red; font-size: 28px; }
        h1, h2, h3 { color: green; font-size: 60px; margin: 40px; }
        a { color: orange; text-decoration: underline; }
        table { border-collapse: separate; border-spacing: 20px; }
        div, span { letter-spacing: 4px; }
      \`;
      document.head.appendChild(hostile);
    }
    const probes = document.createElement("div");
    probes.id = "host-probes";
    probes.innerHTML = '<button id="probe-button" class="btn-p">Host button</button>'
      + '<div id="probe-card" class="card">Host card</div><h1 id="probe-h1">Host title</h1>'
      + '<input id="probe-input" class="fld" /><span id="probe-badge" class="badge bg">host</span>';
    document.body.prepend(probes);
    const PROPS = ["backgroundColor", "borderTopWidth", "borderTopLeftRadius", "paddingTop", "fontSize", "color", "height", "fontFamily", "lineHeight", "letterSpacing", "boxSizing", "display"];
    const snap = (el) => { const cs = getComputedStyle(el); return Object.fromEntries(PROPS.map((p) => [p, cs[p]])); };
    const hostSnap = () => Object.fromEntries(["probe-button", "probe-card", "probe-h1", "probe-input", "probe-badge"].map((id) => [id, snap(document.getElementById(id))]));
    result.hostBefore = hostSnap();
    await mountInto("mount-a", {});
    await new Promise((resolve) => { const t = setInterval(() => { if (document.querySelector("#mount-a .sn")) { clearInterval(t); resolve(); } }, 20); });
    result.hostAfter = hostSnap();
    // Fixed-size elements only: a container's height depends on how much content has loaded, which would make this flaky.
    const remoteProbes = { nav: "#mount-a .sn", logo: "#mount-a .sb-logo", group: "#mount-a .sb-grp", topbar: "#mount-a .topbar" };
    result.remote = Object.fromEntries(Object.entries(remoteProbes).map(([name, sel]) => [name, document.querySelector(sel) ? snap(document.querySelector(sel)) : null]));
  }

  // FE-BUG-22: the remote must not read or write the host's own global state
  // (a host Redux store on window, redux-persist's localStorage key) nor add
  // store-shaped globals of its own.
  if (scenario === "host-state") {
    window.__HOST_STORE__ = Object.freeze({ state: Object.freeze({ count: 1 }), dispatch: () => { window.__HOST_DISPATCHED__ = true; } });
    localStorage.setItem("persist:root", "host-sentinel");
    result.globalsBefore = Object.getOwnPropertyNames(window);
    await mountInto("mount-a", {});
    await new Promise((resolve) => { const t = setInterval(() => { if (document.querySelector("#mount-a .sn")) { clearInterval(t); resolve(); } }, 20); });
    result.globalsAfter = Object.getOwnPropertyNames(window);
  }

  // FE-BUG-22: shared-scope negotiation across host React versions. ?v= is the
  // version the host offers; sharedCalls says whether the remote took it.
  if (scenario === "version") {
    const offered = new URLSearchParams(location.search).get("v");
    const reactMod = await import("/assets/${reactChunk}");
    const domMod = await import("/assets/${reactDomChunk}");
    const calls = { react: 0, "react-dom": 0 };
    result.sharedCalls = calls;
    const remote = await loadRemote();
    await remote.init({
      react: { [offered]: { get: () => () => { calls.react++; return Promise.resolve(reactMod); }, loaded: 0 } },
      "react-dom": { [offered]: { get: () => () => { calls["react-dom"]++; return Promise.resolve(domMod); }, loaded: 0 } },
    });
    const Comp = resolveComponent((await remote.get("./DataConnectApp"))());
    const React = (await import("/assets/${reactChunk}")).default;
    const ReactDOM = (await import("/assets/${reactDomChunk}")).default;
    ReactDOM.createRoot(document.getElementById("mount-a")).render(React.createElement(Comp, { apiBase: DEFAULT_API_BASE }));
    await new Promise((resolve) => { const t = setInterval(() => { if (document.querySelector("#mount-a .dc-root")) { clearInterval(t); resolve(); } }, 20); });
  }

  // FE-BUG-22: a screen that throws while rendering must not blank the remote
  // inside the host, and the host hears about it through onError.
  if (scenario === "crash") {
    window.__errors__ = [];
    const alive = document.createElement("button");
    alive.id = "host-alive";
    alive.textContent = "host still alive";
    document.body.prepend(alive);
    await mountInto("mount-a", { onError: (e) => window.__errors__.push(String(e && e.message || e)) });
  }

  if (scenario === "unavailable") {
    try {
      await import("/assets/remoteEntry-does-not-exist.js");
      result.caught = false;
    } catch (err) {
      result.caught = true;
      result.errorMessage = String(err);
    }
  }

  if (scenario === "version-mismatch") {
    const remote = await loadRemote();
    const originalLog = console.log;
    const logs = [];
    console.log = (...args) => { logs.push(args.join(" ")); originalLog(...args); };
    // A version well outside "^18.3.1", paired with a deliberately broken
    // module shape — if the remote used it, mounting would blow up.
    await remote.init({
      react: { "15.0.0": { get: () => () => Promise.resolve({ default: { brokenNotReact: true } }) } },
      "react-dom": { "15.0.0": { get: () => () => Promise.resolve({ default: { brokenNotReact: true } }) } },
    });
    const factory = await remote.get("./DataConnectApp");
    const Comp = resolveComponent(factory());
    const React = (await import("/assets/${reactChunk}")).default;
    const ReactDOM = (await import("/assets/${reactDomChunk}")).default;
    const root = ReactDOM.createRoot(document.getElementById("mount-a"));
    root.render(React.createElement(Comp, { apiBase: DEFAULT_API_BASE }));
    console.log = originalLog;
    result.negotiationLogs = logs;
  }

  result.ok = true;
} catch (err) {
  result.ok = false;
  result.error = String(err && err.stack || err);
}
`;
}

/*
 * Finds the federation plugin's shared react/react-dom stub chunks in a
 * build's assets/ directory by name pattern, not a fixed hash — see
 * buildHostJs() above for why the hash itself can't be relied on.
 */
function resolveSharedChunkNames(builtDistDir: string): { reactChunk: string; reactDomChunk: string } {
  const assetFiles = readdirSync(path.join(builtDistDir, "assets"));
  const reactDomChunk = assetFiles.find((f) => /^__federation_shared_react-dom-.+\.js$/.test(f));
  const reactChunk = assetFiles.find((f) => /^__federation_shared_react-(?!dom-).+\.js$/.test(f));
  if (!reactChunk || !reactDomChunk) {
    throw new Error(`Could not find shared react/react-dom chunks in ${builtDistDir}/assets — federation plugin output shape may have changed. Found: ${assetFiles.join(", ")}`);
  }
  return { reactChunk, reactDomChunk };
}

function startStaticServer(hostJs: string): Promise<{ server: Server; port: number }> {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      const url = new URL(req.url || "/", "http://localhost");
      try {
        if (url.pathname === "/" || url.pathname === "/host.html") {
          res.writeHead(200, { "Content-Type": "text/html" });
          res.end(HOST_HTML);
          return;
        }
        if (url.pathname === "/host.js") {
          res.writeHead(200, { "Content-Type": "application/javascript" });
          res.end(hostJs);
          return;
        }
        const filePath = path.join(distDir, url.pathname);
        if (!filePath.startsWith(distDir)) throw new Error("path escape");
        const body = await readFile(filePath);
        res.writeHead(200, { "Content-Type": CONTENT_TYPES[path.extname(filePath)] || "application/octet-stream" });
        res.end(body);
      } catch {
        res.writeHead(404);
        res.end("not found");
      }
    });
    server.listen(0, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      resolve({ server, port });
    });
  });
}

let server: Server;
let baseUrl: string;
const pageErrors: string[] = [];

test.beforeAll(async () => {
  execSync(`npx vite build --outDir ${JSON.stringify(distDir)}`, { cwd: root, stdio: "pipe" });
  const { reactChunk, reactDomChunk } = resolveSharedChunkNames(distDir);
  const started = await startStaticServer(buildHostJs(reactChunk, reactDomChunk));
  server = started.server;
  baseUrl = `http://localhost:${started.port}`;
});

test.afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
  await rm(distDir, { recursive: true, force: true });
});

async function gotoScenario(page: Page, scenario: string, afterMock?: () => Promise<void>, hash = "") {
  page.on("pageerror", (err) => pageErrors.push(`[${scenario}] ${String(err)}`));
  await mockBackend(page);
  // Route handlers run most-recently-registered-first, so anything that
  // needs to see a request before mockBackend's catch-all swallows it (e.g.
  // to inspect a header) must be registered after this call, not before.
  if (afterMock) await afterMock();
  await page.goto(`${baseUrl}/host.html?scenario=${scenario}${hash}`);
  await page.waitForFunction(() => (window as unknown as { __result__?: { ok?: boolean } }).__result__?.ok !== undefined);
  return page.evaluate(() => (window as unknown as { __result__: Record<string, unknown> }).__result__);
}

test.afterEach(() => {
  expect(pageErrors, "host page must not throw uncaught errors").toEqual([]);
  pageErrors.length = 0;
});

test("host loads remoteEntry.js and resolves the exposed module by name", async ({ page }) => {
  const result = await gotoScenario(page, "basic");
  expect(result.ok, String(result.error)).toBe(true);
  expect(result.hasGet).toBe(true);
  expect(result.hasInit).toBe(true);
  expect(result.exposedType).toBe("function");
});

test("remote consumes React through the host's shared scope (singleton), not its own bundled copy", async ({ page }) => {
  const result = await gotoScenario(page, "shared");
  expect(result.ok, String(result.error)).toBe(true);
  // Only react is asserted here: the exposed component graph (DataConnectApp.tsx
  // and everything under it) never imports "react-dom" — only the standalone
  // dev entry (src/main.tsx, outside the federated module graph) does, since
  // createRoot() is the host's job, not the remote's. react-dom is still
  // declared shared for symmetry/future-proofing, but has nothing to
  // exercise it from inside the exposed module today.
  const calls = result.sharedCalls as { react: number; "react-dom": number };
  expect(calls.react, "remote never pulled react from the host's shared scope").toBeGreaterThan(0);
  await expect(page.locator("#mount-a .dc-root")).toBeVisible();
});

test("mounted component's CSS is injected into the host document", async ({ page }) => {
  const result = await gotoScenario(page, "css");
  expect(result.ok, String(result.error)).toBe(true);
  const styleLink = page.locator('head link[rel="stylesheet"][href*="style-"]');
  await expect(styleLink).toHaveCount(1);
  // DataConnect has no user menu of its own in a host mount
  // — .topbar is always present regardless
  // of permissions, so it is the stable canary for "CSS applied".
  const topbarDisplay = await page.locator("#mount-a .topbar").evaluate((el) => getComputedStyle(el).display);
  expect(topbarDisplay).toBe("flex");
});

test("host-supplied props (onOperation) reach the mounted component, and no tenant header is ever sent", async ({ page }) => {
  let capturedOrgId: string | null | undefined;
  const result = await gotoScenario(page, "props", async () => {
    await page.route("**/api/v1/dataconnect/connections", async (route) => {
      capturedOrgId = await route.request().headerValue("x-org-id");
      return route.fallback();
    });
  });
  expect(result.ok, String(result.error)).toBe(true);

  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await page.locator("#mount-a .ctile").first().waitFor();
  expect(capturedOrgId).toBeNull();                                  // the tenant comes from the signed ticket, not the client
  await page.locator("#mount-a .ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator("#mount-a .iconadd").click();
  await page.locator('#mount-a input[placeholder^="my-postgres"]').fill("host-contract-db");
  await page.locator('#mount-a input[placeholder="db.internal"]').fill("db.internal.test");
  await page.locator("#mount-a .fld", { hasText: "Database" }).locator("input").fill("salesdb");
  await page.locator("#mount-a .fld", { hasText: "Username" }).locator("input").fill("dataconnect");
  await page.locator("#mount-a .fld", { hasText: "Password" }).locator("input").fill("hunter2");
  await page.getByRole("button", { name: "Create connection" }).click();
  await expect(page.locator("#mount-a .tbody")).toContainText("host-contract-db");

  const ops = await page.evaluate(() => (window as unknown as { __ops__: { status: string; resource?: string }[] }).__ops__);
  expect(ops.some((op) => op.status === "success" && op.resource === "connections")).toBe(true);
});

test("embedded prop hides the remote's own chrome for host-supplied shells", async ({ page }) => {
  const result = await gotoScenario(page, "embedded");
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a aside.sb")).toHaveCount(0);
  await expect(page.locator("#mount-a .dc-root")).toBeVisible();
});

test("hosted by the portal (roleBadges given): the role strip renders at the right of the top bar, with no avatar of DataConnect's own", async ({ page }) => {
  const result = await gotoScenario(page, "portal-chrome");
  expect(result.ok, String(result.error)).toBe(true);
  const hosted = page.locator("#mount-a");
  const badges = hosted.locator(".topbar .tb-r [data-testid=dc-role-badges] .host-role-badge");
  await expect(badges).toHaveText(["DataConnect Admin", "Data Engineer", "Data Steward", "DataConnect Viewer"]);
  await expect(hosted.locator(".tb-av")).toHaveCount(0);                              // the portal rail carries the profile
  await expect(hosted.locator(".topbar")).not.toContainText("Lithish");
  await expect(hosted.locator(".role-switch, .user-sim-btn")).toHaveCount(0);
  // The app's own logo is still the first thing at the top-left of its chrome (top of its sidebar), beside the portal's rail.
  await expect(hosted.locator("aside.sb > :first-child")).toHaveClass(/sb-logo/);
  await expect(hosted.locator("aside.sb .sb-sub")).toHaveText("DataConnect");
  // One line: the strip never wraps the bar, and sits right of the search.
  const bar = (await hosted.locator(".topbar").boundingBox())!;
  const strip = (await hosted.locator("[data-testid=dc-role-badges]").boundingBox())!;
  const search = (await hosted.locator(".hs-box").boundingBox())!;
  expect(bar.height).toBe(60);
  expect(strip.x).toBeGreaterThan(search.x + search.width);
  expect(strip.x + strip.width).toBeLessThanOrEqual(bar.x + bar.width);

  // Standalone (no roleBadges): today's top bar — the user's avatar, no strip.
  const standalone = page.locator("#mount-b");
  await expect(standalone.locator(".tb-av")).toHaveText("L");
  await expect(standalone.locator("[data-testid=dc-role-badges]")).toHaveCount(0);

  // An older portal still passing accountMenu keeps it where it was.
  await expect(page.locator("#mount-c .topbar .tb-r .host-account-menu")).toHaveText("Old portal menu");
  await expect(page.locator("#mount-c .tb-av")).toHaveCount(0);
});

test("the role strip stays on one line on a narrow screen (scrolls sideways, never wraps the bar)", async ({ page }) => {
  await page.setViewportSize({ width: 700, height: 800 });
  const result = await gotoScenario(page, "portal-chrome");
  expect(result.ok, String(result.error)).toBe(true);
  const hosted = page.locator("#mount-a");
  await expect(hosted.locator(".host-role-badge")).toHaveCount(4);
  const bar = (await hosted.locator(".topbar").boundingBox())!;
  const strip = (await hosted.locator("[data-testid=dc-role-badges]").boundingBox())!;
  expect(bar.height).toBe(60);
  expect(strip.x + strip.width).toBeLessThanOrEqual(bar.x + bar.width + 1);
});

test("embedded (no sidebar): the app's logo opens the top bar instead", async ({ page }) => {
  const result = await gotoScenario(page, "embedded");
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .topbar > :first-child")).toHaveClass(/tb-logo/);
  await expect(page.locator("#mount-a .topbar .tb-logo .sb-sub")).toHaveText("DataConnect");
});

// FE-BUG-22: the "props" test above only proves onOperation arrives —
// mockBackend's route glob (**/api/v1/dataconnect/**) matches any origin,
// so it would keep intercepting requests even if apiBase were silently
// ignored and the remote fell back to AppContext.tsx's dev-only DEV_BASE.
// This test captures the actual outbound request URL to prove apiBase
// itself — not just its path suffix — is what the remote requests against.
test("host-supplied apiBase is honored for outbound requests, not the dev-only fallback (FE-BUG-22)", async ({ page }) => {
  let capturedUrl: string | null = null;
  const result = await gotoScenario(page, "apibase", async () => {
    await page.route("**/tenant-x/api/v1/dataconnect/**", async (route) => {
      if (capturedUrl === null) capturedUrl = route.request().url();
      return route.fallback();
    });
  });
  expect(result.ok, String(result.error)).toBe(true);
  // Requires a successful round trip through the custom base, not just a
  // fire-and-forget request: the topbar's connection status only renders
  // once /health has actually resolved.
  expect(capturedUrl, "no request observed against the host-supplied apiBase").not.toBeNull();
  expect(capturedUrl).toContain("/tenant-x/api/v1/dataconnect/");
  expect(capturedUrl).not.toContain(":8001"); // DEV_BASE's port — never hit once a host supplies apiBase
});

// FE-GAP-01: the production bundle itself — not just the pure function — fails closed when the host's required configuration is missing.
test("a federated mount without apiBase shows a configuration error, makes no request, and never uses the dev backend (FE-GAP-01)", async ({ page }) => {
  const requests: string[] = [];
  // Exactly the state a dev session leaves behind on this origin: the dev backend.
  await page.addInitScript(() => {
    localStorage.setItem("dc_base_url", "http://localhost:8001/api/v1/dataconnect");
  });
  page.on("request", (req) => {
    if (/\/api\/v1\/dataconnect|:8001/.test(req.url())) requests.push(req.url());
  });

  const result = await gotoScenario(page, "config-missing");
  expect(result.ok, String(result.error)).toBe(true);

  for (const mount of ["#mount-a", "#mount-b", "#mount-c"]) {
    await expect(page.locator(`${mount} [data-testid=config-error]`)).toContainText("apiBase");
    await expect(page.locator(`${mount} [role=alert]`)).toBeVisible();
    await expect(page.locator(`${mount} .sn`)).toHaveCount(0);        // none of the app (sidebar, pages) was built
  }

  // Give any stray startup request time to appear before asserting that there were none.
  await page.waitForTimeout(500);
  expect(requests, "a misconfigured mount must not call any backend").toEqual([]);
  // The host is told, once per bad mount, so it can alert on it.
  const errors = (await page.evaluate(() => (window as unknown as { __errors__: string[] }).__errors__));
  expect(errors).toEqual(Array(3).fill("DataConnect is not configured: the host did not provide apiBase."));
});

test("identity props from a host (role / user / orgId) and localStorage are ignored: GET /me decides", async ({ page }) => {
  // Privileged-looking state a dev session (or an old build) could leave on this origin.
  await page.addInitScript(() => {
    localStorage.setItem("dc_role", "DataConnect Admin");
    localStorage.setItem("dc_org_id", "org_from_localStorage");
    localStorage.setItem("dc_sim_user", "ananya");
  });
  const sent: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/v1/dataconnect/")) sent.push(...Object.keys(req.headers()).map((h) => h.toLowerCase()));
  });
  const result = await gotoScenario(page, "host-identity-ignored", async () => {
    // GET /me says: a Viewer persona, user Priya Nair, tenant org_me (registered after mockBackend, so it wins for /me).
    await page.route("**/api/v1/dataconnect/me", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ...meFor("Viewer", "Priya Nair"), tenant: "org_me" }),
    }));
  });
  expect(result.ok, String(result.error)).toBe(true);

  for (const mount of ["#mount-a", "#mount-b"]) {
    await expect(page.locator(`${mount} .app`)).toHaveAttribute("data-dc-user", "Priya Nair");
    await expect(page.locator(`${mount} .app`)).toHaveAttribute("data-dc-org", "org_me");
    await expect(page.locator(`${mount} .role-switch, ${mount} .user-sim-btn`)).toHaveCount(0);
    await page.locator(`${mount} .sn`, { hasText: "Connections" }).click();
    await page.locator(`${mount} .ctile`).first().click();
    await expect(page.locator(`${mount} .iconadd`)).toHaveCount(0);                // a viewer: no create, whatever the host prop said
  }
  for (const h of ["x-org-id", "x-actor", "x-user-role", "x-user-roles", "x-user-permissions"]) expect(sent, h).not.toContain(h);
});

// ── "View as role" (viewPermissions): the effective set is /me ∩ viewPermissions — narrows only, never widens ────────────────────
const ALL_PAGES = ["Dashboard", "Monitoring", "Alerts", "Connections", "Pipelines", "Schemas", "Quality", "Lineage", "Transforms", "Templates", "Settings", "Audit / History"];
const NARROW = ["dataconnect:access:view", "dataconnect:dashboard:view", "dataconnect:connections:view"];
const viewAs = (list?: string[] | null) => `view-as${list === undefined ? "" : `&view=${encodeURIComponent(JSON.stringify(list))}`}`;

test("view as: narrowing to one role hides a page the full set shows", async ({ page }) => {
  const result = await gotoScenario(page, viewAs(NARROW));
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .sn")).toHaveText(["Dashboard", "Connections"]);
  await expect(page.locator("#mount-a .sn", { hasText: "Settings" })).toHaveCount(0);
  // mount-b (null, "All roles") on the same page and the same /me still shows Settings.
  await expect(page.locator("#mount-b .sn", { hasText: "Settings" })).toHaveCount(1);
  // Buttons follow too: the narrowed role has no connections:edit, so no create, though /me (Admin) holds it.
  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await page.locator("#mount-a .ctile").first().click();
  await expect(page.locator("#mount-a .btn-s", { hasText: "All types" })).toBeVisible();   // the type's page is open
  await expect(page.locator("#mount-a .iconadd")).toHaveCount(0);
});

test("view as: viewPermissions can never add a permission the user lacks", async ({ page }) => {
  const result = await gotoScenario(page, viewAs([...PERMISSION_IDS]), async () => {
    // /me: a viewer without Audit. The host lists everything (Audit and every edit permission included).
    await page.route("**/api/v1/dataconnect/me", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ...meFor("Viewer"), permissions: PERSONAS.Viewer.filter((p) => p !== "dataconnect:audit:view") }),
    }));
  });
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .sn")).toHaveText(ALL_PAGES.filter((t) => t !== "Audit / History"));
  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await page.locator("#mount-a .ctile").first().click();
  await expect(page.locator("#mount-a .btn-s", { hasText: "All types" })).toBeVisible();
  await expect(page.locator("#mount-a .iconadd")).toHaveCount(0);                  // still a viewer: no create
  // A role made only of permissions the user does not hold shows nothing — not a page of theirs, and not a blank screen.
  await page.evaluate(() => (window as unknown as { __view__: (v: string[]) => void }).__view__(["dataconnect:audit:view", "dataconnect:settings:edit"]));
  await expect(page.locator("#mount-a .sn")).toHaveCount(0);
  await expect(page.locator("#mount-a [data-testid=dc-access-denied]")).toContainText("Nothing to show for this role");
});

test("view as: null or undefined viewPermissions change nothing", async ({ page }) => {
  const result = await gotoScenario(page, viewAs(undefined));
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .sn")).toHaveText(ALL_PAGES);                 // undefined (an older portal)
  await expect(page.locator("#mount-b .sn")).toHaveText(ALL_PAGES);                 // null ("All roles")
});

test("view as: switching the role updates the pages in place, without a remount, and leaves a page no longer allowed", async ({ page }) => {
  const result = await gotoScenario(page, viewAs(undefined));
  expect(result.ok, String(result.error)).toBe(true);
  const app = page.locator("#mount-a");
  await app.locator(".sn", { hasText: "Settings" }).click();
  await expect(app.locator(".sn.active")).toHaveText("Settings");
  // A marker on the mounted DOM: a remount (new root / new tree) would drop it.
  await app.locator(".dc-root").evaluate((el) => el.setAttribute("data-test-mounted", "1"));

  await page.evaluate((v) => (window as unknown as { __view__: (v: string[]) => void }).__view__(v), NARROW);
  await expect(app.locator(".sn")).toHaveText(["Dashboard", "Connections"]);
  await expect(app.locator(".sn.active")).toHaveText("Dashboard");                 // Settings is gone: the first allowed page, not a blank
  await expect(app.locator(".cont")).toBeVisible();

  await page.evaluate(() => (window as unknown as { __view__: (v: null) => void }).__view__(null));
  await expect(app.locator(".sn")).toHaveText(ALL_PAGES);
  await expect(app.locator(".dc-root")).toHaveAttribute("data-test-mounted", "1");
});

test("two independent mounts on one host page don't share routing/navigation state", async ({ page }) => {
  const result = await gotoScenario(page, "route");
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .sn.active")).toContainText("Dashboard");
  await expect(page.locator("#mount-b .sn.active")).toContainText("Dashboard");

  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await expect(page.locator("#mount-a .sn.active")).toContainText("Connections");
  await expect(page.locator("#mount-b .sn.active")).toContainText("Dashboard");
});

// FE-BUG-22: the nav-state test above exercises plain useState (DataConnectApp.tsx),
// not Redux — config and toasts are the only state this app actually keeps
// in its store (see AppContext.tsx's file header). Each mount gets its own
// createDataConnectStore() call, so a toast fired by a client-side
// validation error in one mount must never reach the other's <Toasts/> —
// that's the actual "two mounts, one host page, one Redux Toolkit copy"
// conflict this app would be exposed to if the store were ever made a
// module-level singleton instead of per-instance.
test("two mounts' Redux stores don't leak state into each other (FE-BUG-22)", async ({ page }) => {
  const result = await gotoScenario(page, "redux-isolation");
  expect(result.ok, String(result.error)).toBe(true);

  await expect(page.locator("#mount-a .toasts .toast")).toHaveCount(0);
  await expect(page.locator("#mount-b .toasts .toast")).toHaveCount(0);

  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await page.locator("#mount-a .ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator("#mount-a .iconadd").click();
  // Submit with no name — a purely client-side validation error (Connections.tsx's
  // ConnectionForm.submit()), so this proves store isolation on its own with
  // no network mocking involved.
  await page.getByRole("button", { name: "Create connection" }).click();

  await expect(page.locator("#mount-a .toasts .toast")).toHaveCount(1);
  await expect(page.locator("#mount-a .toasts .toast")).toContainText("Connection name is required");
  // The other mount's own store never saw mount-a's dispatch.
  await expect(page.locator("#mount-b .toasts .toast")).toHaveCount(0);
});

test("an unavailable remote fails as a catchable rejection, not a page crash", async ({ page }) => {
  const result = await gotoScenario(page, "unavailable");
  expect(result.ok, String(result.error)).toBe(true);
  expect(result.caught).toBe(true);
  expect(typeof result.errorMessage).toBe("string");
});

test("an incompatible host-offered React version is rejected in favor of the remote's own bundled copy", async ({ page }) => {
  const result = await gotoScenario(page, "version-mismatch");
  expect(result.ok, String(result.error)).toBe(true);
  const logs = (result.negotiationLogs as string[]) || [];
  expect(logs.some((line) => line.includes("not satisfied"))).toBe(true);
  await expect(page.locator("#mount-a .dc-root")).toBeVisible();
});


// ── FE-BUG-22 additions ─────────────────────────────────────────────────────

type StyleSnapshot = Record<string, string>;

test("the remote's CSS does not restyle the host's own elements", async ({ page }) => {
  const result = await gotoScenario(page, "css-isolation");
  expect(result.ok, String(result.error)).toBe(true);

  // Same classes the remote defines (.btn-p, .card, .badge, .fld) on the HOST's elements:
  // every remote rule is scoped under .dc-root, so none of them may apply here.
  expect(result.hostAfter).toEqual(result.hostBefore);
});

test("a host's sloppy global CSS does not restyle the remote", async ({ page }) => {
  const clean = await gotoScenario(page, "css-isolation");
  expect(clean.ok, String(clean.error)).toBe(true);
  const hostile = await gotoScenario(page, "css-isolation&hostile=1");
  expect(hostile.ok, String(hostile.error)).toBe(true);

  // The hostile stylesheet really is active on the host side (otherwise this proves nothing)…
  const hostBefore = hostile.hostBefore as Record<string, StyleSnapshot>;
  expect(hostBefore["probe-button"]?.borderTopLeftRadius).toBe("40px");

  // …yet every property we compare on the remote's own elements is unchanged.
  const cleanRemote = clean.remote as Record<string, StyleSnapshot | null>;
  const hostileRemote = hostile.remote as Record<string, StyleSnapshot | null>;
  for (const name of Object.keys(cleanRemote)) {
    expect(cleanRemote[name], `remote element "${name}" should exist`).not.toBeNull();
    expect(hostileRemote[name], `remote element "${name}" must look the same under the host's global CSS`).toEqual(cleanRemote[name]);
  }
});

test("the remote does not touch the host's global state or add store-like globals", async ({ page }) => {
  const result = await gotoScenario(page, "host-state");
  expect(result.ok, String(result.error)).toBe(true);

  const before = new Set(result.globalsBefore as string[]);
  const added = (result.globalsAfter as string[]).filter((name) => !before.has(name));
  const storeLike = added.filter((name) => /redux|store|dataconnect|^dc_|__dc/i.test(name));
  expect(storeLike, `store-like globals added by the remote: ${added.join(", ")}`).toEqual([]);

  const hostState = await page.evaluate(() => ({
    persisted: localStorage.getItem("persist:root"),
    count: (window as unknown as { __HOST_STORE__: { state: { count: number } } }).__HOST_STORE__.state.count,
    dispatched: (window as unknown as { __HOST_DISPATCHED__?: boolean }).__HOST_DISPATCHED__ ?? false,
  }));
  expect(hostState).toEqual({ persisted: "host-sentinel", count: 1, dispatched: false });
});

test("navigating inside the remote leaves the host's URL and history alone", async ({ page }) => {
  const result = await gotoScenario(page, "shared", undefined, "#/portal/dataconnect");
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .dc-root")).toBeVisible();

  const startUrl = page.url();
  const startHistory = await page.evaluate(() => history.length);

  for (const label of ["Connections", "Pipelines", "Quality", "Lineage", "Dashboard"]) {
    await page.locator("#mount-a .sn", { hasText: label }).click();
    await expect(page.locator("#mount-a .sn.active")).toContainText(label);
  }

  expect(page.url()).toBe(startUrl);
  expect(await page.evaluate(() => history.length)).toBe(startHistory);
  expect(await page.evaluate(() => location.hash)).toBe("#/portal/dataconnect");
});

// The remote declares requiredVersion "^18.3.1" for react and react-dom (vite.config.ts).
// A host offering a version inside that range is used; anything outside it is rejected in
// favour of the remote's own bundled copy.
const ACCEPTED_REACT_VERSIONS = ["18.3.1", "18.3.9", "18.99.0"];
const REJECTED_REACT_VERSIONS = ["18.2.0", "17.0.2", "19.0.0", "16.14.0"];

for (const version of ACCEPTED_REACT_VERSIONS) {
  test(`a host offering React ${version} is used (shared singleton)`, async ({ page }) => {
    const result = await gotoScenario(page, `version&v=${version}`);
    expect(result.ok, String(result.error)).toBe(true);
    await expect(page.locator("#mount-a .dc-root")).toBeVisible();
    const calls = result.sharedCalls as { react: number; "react-dom": number };
    // Only react is asserted: the exposed module graph never imports react-dom (the host's createRoot
    // does) — see the shared-singleton test above.
    expect(calls.react).toBeGreaterThan(0);
  });
}

for (const version of REJECTED_REACT_VERSIONS) {
  test(`a host offering React ${version} is rejected in favour of the remote's own copy`, async ({ page }) => {
    const result = await gotoScenario(page, `version&v=${version}`);
    expect(result.ok, String(result.error)).toBe(true);
    await expect(page.locator("#mount-a .dc-root")).toBeVisible();
    const calls = result.sharedCalls as { react: number; "react-dom": number };
    expect(calls.react).toBe(0);
  });
}

test("a screen that crashes while rendering shows a contained message, tells the host, and recovers on navigation", async ({ page }) => {
  // Renders an object where text is expected ("Objects are not valid as a React child").
  const badPipelines = [{
    id: "p-bad", org_id: "org_test", name: { oops: "not a string" }, status: "running",
    topic_name: "t", connector_name: "c", mode: "cdc", zone: "raw", generated_config: {},
  }];
  const result = await gotoScenario(page, "crash", async () => {
    await page.route("**/api/v1/dataconnect/pipelines", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(badPipelines),
    }));
  });
  expect(result.ok, String(result.error)).toBe(true);
  await expect(page.locator("#mount-a .dc-root")).toBeVisible();

  await page.locator("#mount-a .sn", { hasText: "Pipelines" }).click();

  const fallback = page.locator("#mount-a").getByRole("alert").filter({ hasText: "hit an unexpected error" });
  await expect(fallback).toBeVisible();
  // The shell around the broken screen survives, and so does the host.
  await expect(page.locator("#mount-a .sn", { hasText: "Connections" })).toBeVisible();
  await expect(page.locator("#host-alive")).toBeVisible();
  // The host was told.
  const errors = await page.evaluate(() => (window as unknown as { __errors__: string[] }).__errors__);
  expect(errors.length).toBeGreaterThan(0);

  // Navigating to a healthy screen clears the fallback.
  await page.locator("#mount-a .sn", { hasText: "Connections" }).click();
  await expect(fallback).toHaveCount(0);
  await expect(page.locator("#mount-a .sn.active")).toContainText("Connections");
});
