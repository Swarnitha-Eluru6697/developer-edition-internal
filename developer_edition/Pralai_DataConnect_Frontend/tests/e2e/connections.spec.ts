/*
 * Connection form tests — the create/edit drawer in src/pages/Connections.tsx,
 * plus the RBAC gating on it (only Admin/Integrator get "manage_connections").
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, switchRole, choose } from "./fixtures";

/** The request body a test captured, or a clear failure if none arrived (a captured value is assigned inside a route callback). */
function requireBody(body: Record<string, unknown> | null): Record<string, unknown> {
  if (!body) throw new Error("no request body was captured");
  return body;
}


test("creates a postgres connection through the form", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();

  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator(".iconadd").click();

  await expect(page.locator(".modal")).toBeVisible();
  await page.locator('input[placeholder^="my-postgres"]').fill("sales-db");
  // postgres's schema (app/services/connection_config_schema.py) requires
  // Host + Database + Username + Password — the connection name alone
  // isn't enough to pass client-side validation before the request is even
  // sent, and neither is Host + Database alone (verified against the real
  // backend, which 422s on exactly that).
  await page.locator('input[placeholder="db.internal"]').fill("db.internal.test");
  await page.locator(".fld", { hasText: "Database" }).locator("input").fill("salesdb");
  await page.locator(".fld", { hasText: "Username" }).locator("input").fill("dataconnect");
  await page.locator(".fld", { hasText: "Password" }).locator("input").fill("hunter2");
  await page.getByRole("button", { name: "Create connection" }).click();

  await expect(page.locator(".toast").first()).toBeVisible();
  await expect(page.locator(".tbody")).toContainText("sales-db");
});

test("requires a connection name before submitting", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator(".iconadd").click();

  await page.getByRole("button", { name: "Create connection" }).click();

  await expect(page.locator(".toast")).toContainText(/required/i);
  await expect(page.locator(".tbody")).toContainText("No postgres connections yet");
});

test("testing a brand-new (not-yet-saved) connection sends no connection_id", async ({ page }) => {
  await mockBackend(page);
  let capturedUrl: string | null = null;
  await page.route("**/api/v1/dataconnect/connections/test-connection**", async (route) => {
    capturedUrl = route.request().url();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok", message: "Connected" }) });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator(".iconadd").click();
  await page.locator('input[placeholder^="my-postgres"]').fill("brand-new-db");
  await page.locator('input[placeholder="db.internal"]').fill("db.internal.test");
  await page.locator(".fld", { hasText: "Database" }).locator("input").fill("salesdb");
  await page.locator(".fld", { hasText: "Username" }).locator("input").fill("dataconnect");
  await page.locator(".fld", { hasText: "Password" }).locator("input").fill("hunter2");
  await page.getByRole("button", { name: "Test connection" }).click();

  await expect.poll(() => capturedUrl).not.toBeNull();
  expect(capturedUrl).not.toContain("connection_id");
});

test("testing an existing connection from the edit form tags the probe with its id", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-existing-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb", username: "dataconnect", password: "********" },
    secret_ref: "secret://dataconnect/connections/conn-existing-1", status: "active",
  });
  await mockBackend(page, state);
  let capturedUrl: string | null = null;
  let capturedBody: Record<string, unknown> | null = null;
  await page.route("**/api/v1/dataconnect/connections/test-connection**", async (route) => {
    capturedUrl = route.request().url();
    capturedBody = route.request().postDataJSON();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok", message: "Connected" }) });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Edit"]').click();
  await expect(page.locator(".modal")).toBeVisible();
  // FE-BUG-11: the password was never touched — shown as a read-only
  // "Configured" indicator, not an editable field — and Test connection
  // must still work without forcing it to be re-entered.
  await expect(page.locator(".fld", { hasText: "Password" })).toContainText("Configured");
  await expect(page.locator(".fld", { hasText: "Password" }).locator("input")).toHaveCount(0);
  await page.getByRole("button", { name: "Test connection" }).click();

  await expect.poll(() => capturedUrl).not.toBeNull();
  expect(capturedUrl).toContain("connection_id=conn-existing-1");
  // The mask must never be echoed back as if it were real secret data —
  // and the connection's own secret_ref rides along instead, so the
  // backend can transiently hydrate the REAL stored password for this one
  // probe (see app/services/secret_store.hydrate_connection_secrets).
  const cfg = requireBody(capturedBody).connection_config as Record<string, unknown>;
  expect(cfg.password).toBeUndefined();
  expect(requireBody(capturedBody).secret_ref).toBe("secret://dataconnect/connections/conn-existing-1");
});

test("changing an existing connection's password sends only the new value, never the mask (FE-BUG-11)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-existing-2", org_id: "org_test", name: "sales-db-2", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb", username: "dataconnect", password: "********" },
    secret_ref: "secret://dataconnect/connections/conn-existing-2", status: "active",
  });
  await mockBackend(page, state);
  let capturedBody: Record<string, unknown> | null = null;
  await page.route("**/api/v1/dataconnect/connections/conn-existing-2", async (route) => {
    if (route.request().method() === "PUT") capturedBody = route.request().postDataJSON();
    return route.fallback();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Edit"]').click();
  await page.locator(".fld", { hasText: "Password" }).getByRole("button", { name: "Change" }).click();
  await expect(page.locator(".fld", { hasText: "Password" }).locator("input")).toHaveValue("");
  await page.locator(".fld", { hasText: "Password" }).locator("input").fill("brand-new-password");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect.poll(() => capturedBody).not.toBeNull();
  const cfg = requireBody(capturedBody).connection_config as Record<string, unknown>;
  // The new value the user actually typed — never "********" (see the
  // previous test for the untouched-field case, which must never send this
  // key at all).
  expect(cfg.password).toBe("brand-new-password");
});

test("discovering a connection pages through results and supports search (GAP-10)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-existing-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb" }, status: "active",
  });
  // Seeded against the connection id — the mock backend re-tags these onto
  // the DiscoveryRun's own id once POST .../discover is called, the same
  // way the real backend generates fresh DiscoveryObject rows per run.
  state.discoveryObjects.push(
    { id: "obj-1", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "customers" },
    { id: "obj-2", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "orders" },
    { id: "obj-3", discovery_run_id: "conn-existing-1", object_type: "schema", schema_name: null, name: "public" },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Discover"]').click();

  await expect(page.locator(".modal")).toBeVisible();
  await expect(page.locator(".modal")).toContainText("3 total");
  await expect(page.locator(".modal .tbody")).toContainText("customers");
  await expect(page.locator(".modal .tbody")).toContainText("orders");

  await page.locator(".modal .srch input").fill("custom");
  await expect(page.locator(".modal .tbody")).toContainText("customers");
  await expect(page.locator(".modal .tbody")).not.toContainText("orders");
});

test("discovery at scale: schema/type filters narrow the list and reset (FE-BUG-12)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-existing-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb" }, status: "active",
  });
  // A source with more than one schema and a mix of object types — enough
  // to exercise both filter dropdowns without needing thousands of rows.
  state.discoveryObjects.push(
    { id: "obj-1", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "customers" },
    { id: "obj-2", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "orders" },
    { id: "obj-3", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "sales", name: "leads" },
    { id: "obj-4", discovery_run_id: "conn-existing-1", object_type: "schema", schema_name: null, name: "public" },
    { id: "obj-5", discovery_run_id: "conn-existing-1", object_type: "schema", schema_name: null, name: "sales" },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Discover"]').click();
  await expect(page.locator(".modal")).toContainText("5 total");

  // Object type filter narrows to just the two schema rows.
  await choose(page.locator('.modal .ss-trigger[aria-label="Filter by object type"]'), "schema");
  await expect(page.locator(".modal .tbody .tr")).toHaveCount(2);
  await expect(page.locator(".modal .tbody")).toContainText("public");
  await expect(page.locator(".modal .tbody")).not.toContainText("customers");
  await choose(page.locator('.modal .ss-trigger[aria-label="Filter by object type"]'), "");

  // Schema filter narrows to just the "sales" schema's one table.
  await choose(page.locator('.modal .ss-trigger[aria-label="Filter by schema"]'), "sales");
  await expect(page.locator(".modal .tbody .tr")).toHaveCount(1);
  await expect(page.locator(".modal .tbody")).toContainText("leads");
  await choose(page.locator('.modal .ss-trigger[aria-label="Filter by schema"]'), "");

  // With both filters reset, every discovered object is listed again.
  await expect(page.locator(".modal .tbody .tr")).toHaveCount(5);
});

test("a brand-new connector type gets a working form from backend field metadata alone, with zero frontend code (schema-driven forms)", async ({ page }) => {
  // A source type this frontend has never heard of before — no hardcoded
  // branch anywhere in this codebase mentions "widgetdb". Proves the form
  // (and the tile grid) are genuinely driven by whatever the backend's
  // connection-config-schemas response says, not a per-connector React
  // component or a hardcoded per-type branch.
  await mockBackend(page);
  await page.route("**/api/v1/dataconnect/connection-config-schemas", async (route) => {
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([
        {
          source_type: "widgetdb",
          fields: [
            { key: "hostname", label: "Host", type: "string", required: true, placeholder: "widget.internal" },
            { key: "port", label: "Port", type: "number", default: 4242 },
            { key: "region", label: "Region", type: "enum", required: true, options: ["us", "eu", "apac"] },
            { key: "use_tls", label: "Use TLS", type: "boolean" },
            { key: "api_key", label: "API key", type: "secret", required: true, persist: false },
            { key: "widgets", label: "Widgets", type: "list", description: "Widget names to sync" },
          ],
        },
      ]),
    });
  });

  let capturedBody: Record<string, unknown> | null = null;
  await page.route("**/api/v1/dataconnect/connections", async (route) => {
    if (route.request().method() === "POST") capturedBody = route.request().postDataJSON();
    return route.fallback();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  // The tile grid itself is schema-driven too — "widgetdb" simply appears.
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^widgetdb$/ }) }).click();
  await page.locator(".iconadd").click();

  await expect(page.locator(".modal")).toContainText("New widgetdb connection");
  await page.locator('input[placeholder^="my-widgetdb"]').fill("widget-conn");
  await page.locator('input[placeholder="widget.internal"]').fill("widget.internal.test");
  // enum -> a real <select>, not a free-text field the user has to type
  // the right word into.
  await choose(page.locator(".fld", { hasText: "Region" }).locator(".ss-trigger"), "eu");
  await page.locator(".fld", { hasText: "Use TLS" }).locator('input[type="checkbox"]').check();
  await page.locator(".fld", { hasText: "API key" }).locator('input[type="password"]').fill("widget-secret-123");
  await page.locator(".fld", { hasText: "Widgets" }).locator("input").fill("lamp, gadget");
  await page.getByRole("button", { name: "Create connection" }).click();

  await expect(page.locator(".tbody")).toContainText("widget-conn");
  expect(capturedBody).not.toBeNull();
  const cfg = requireBody(capturedBody).connection_config as Record<string, unknown>;
  expect(cfg.hostname).toBe("widget.internal.test");
  expect(cfg.region).toBe("eu");
  expect(cfg.use_tls).toBe(true);
  expect(cfg.api_key).toBe("widget-secret-123");
  expect(cfg.widgets).toEqual(["lamp", "gadget"]);
  // port was left at its schema-declared default, never entered by hand,
  // and it still made it into the submitted config as a real number.
  expect(cfg.port).toBe(4242);
});

test("Viewer cannot see connection management controls", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();

  await expect(page.locator(".iconadd")).toHaveCount(0);
});

test("Viewer sees a genuine read-only field-by-field connection detail, not a hidden or disabled form (FE-BUG-19)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-view-1", org_id: "org_test", name: "erp-prod-db", source_type: "postgres",
    connection_config: {
      hostname: "erp-db.internal", port: 5432, database: "erp", username: "dataconnect", password: "********",
    },
    status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();

  const row = page.locator(".tr", { hasText: "erp-prod-db" });
  await expect(row.locator('[title="View"]')).toBeVisible();
  // No edit/delete affordance for Viewer.
  await expect(row.locator('[title="Edit"]')).toHaveCount(0);
  await expect(row.locator('[title="Delete"]')).toHaveCount(0);

  await row.locator('[title="View"]').click();
  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Source");
  await expect(dialog).toContainText("postgres");
  await expect(dialog).toContainText("Host");
  await expect(dialog).toContainText("erp-db.internal");
  await expect(dialog).toContainText("Database");
  await expect(dialog).toContainText("erp");
  // Secret fields never show the mask/value — just a Configured indicator.
  await expect(dialog).toContainText("Configured");
  await expect(dialog).not.toContainText("********");
  // Read-only presentation, not a form full of disabled inputs.
  await expect(dialog.locator("input, textarea")).toHaveCount(0);
});

// GAP-02: the verification outcome is its own record on the backend — the edit form shows what it holds
// (status, when, latency, a stable error code) and is honest when an edit made it stale.
async function openEditFor(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Edit"]').click();
  await expect(page.locator(".modal")).toBeVisible();
}

const VERIFIED_BASE = {
  id: "conn-verif-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
  connection_config: { hostname: "db.internal.test", database: "salesdb", username: "dataconnect", password: "********" },
  secret_ref: "secret://dataconnect/connections/conn-verif-1",
};

test("the edit form shows the last test's latency and a failed test's stable error code (GAP-02)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    ...VERIFIED_BASE, status: "error", verification_status: "error", verification_stale: false,
    last_tested_at: "2026-09-21T18:32:00Z", last_test_message: "Connection check failed: timed out",
    latency_ms: 3000.4, error_code: "TIMEOUT",
  });
  await mockBackend(page, state);
  await openEditFor(page);

  const verification = page.locator(".modal .fld", { hasText: "Verification" });
  await expect(verification).toContainText("ERROR");
  await expect(verification).toContainText("3000.4 ms");
  await expect(verification).toContainText("TIMEOUT");
  await expect(verification).toContainText("Connection check failed: timed out");
});

test("a result made stale by an edit reads as unverified and says why (GAP-02)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    ...VERIFIED_BASE, status: "draft", verification_status: "unverified", verification_stale: true,
    last_tested_at: "2026-09-21T18:32:00Z",
    last_test_message: "This connection's settings changed after it was last tested, so that result no longer applies. Test the connection again.",
    latency_ms: null, error_code: null,
  });
  await mockBackend(page, state);
  await openEditFor(page);

  const verification = page.locator(".modal .fld", { hasText: "Verification" });
  await expect(verification).toContainText("UNVERIFIED");
  await expect(verification).toContainText("settings changed since this test");
  await expect(verification).toContainText("Test the connection again");
});

test("saving an edit that changes how the connection connects tries it first, and when it connects the connection is saved as connected", async ({ page }) => {
  const state = freshState();
  state.connections.push({ ...VERIFIED_BASE, status: "active", verification_status: "ok", verification_stale: false, last_tested_at: "2026-09-21T18:32:00Z" });
  await mockBackend(page, state);
  const calls: string[] = [];
  await page.route("**/api/v1/dataconnect/connections/**", (route) => {
    const r = route.request();
    if (r.method() !== "GET") calls.push(`${r.method()} ${new URL(r.url()).pathname.replace("/api/v1/dataconnect", "")}`);
    if (r.method() !== "PUT") return route.fallback();
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ ...VERIFIED_BASE, status: "draft", verification_status: "unverified", verification_stale: true }),
    });
  });
  await openEditFor(page);

  await page.locator(".fld", { hasText: "Host" }).locator("input").first().fill("a-different-host.internal");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page.locator(".toast").last()).toContainText("connected");
  // tried with the new values first, then saved, then recorded on the saved connection (so it does not sit as "draft")
  expect(calls).toEqual(["POST /connections/test-connection", "PUT /connections/conn-verif-1", "POST /connections/conn-verif-1/test-connection"]);
});

// A wrong password used to be saved with a plain "Connection updated" and only break the next deploy.
async function editWithRefusal(page: Page, message = "PostgreSQL auth/connect failed: password authentication failed") {
  const state = freshState();
  state.connections.push({ ...VERIFIED_BASE, status: "active", verification_status: "ok", verification_stale: false, last_tested_at: "2026-09-21T18:32:00Z" });
  await mockBackend(page, state);
  const calls: string[] = [];
  await page.route("**/api/v1/dataconnect/connections/**", (route) => {
    const r = route.request();
    if (r.method() !== "GET") calls.push(`${r.method()} ${new URL(r.url()).pathname.replace("/api/v1/dataconnect", "")}`);
    if (r.method() === "POST" && new URL(r.url()).pathname.endsWith("/connections/test-connection")) {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "failed", message, details: { auth: false } }) });
    }
    if (r.method() === "PUT") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...VERIFIED_BASE, status: "draft", verification_status: "unverified", verification_stale: true }) });
    }
    return route.fallback();
  });
  await openEditFor(page);
  return calls;
}

test("a refused password is not saved: Save shows why, and nothing is written until you choose Save anyway", async ({ page }) => {
  const calls = await editWithRefusal(page);
  await page.locator(".fld", { hasText: "Host" }).locator("input").first().fill("a-different-host.internal");
  await page.getByRole("button", { name: "Save changes" }).click();

  const banner = page.getByRole("alert").filter({ hasText: "were not saved" });
  await expect(banner).toContainText("password authentication failed");
  await expect(page.getByRole("button", { name: "Save anyway" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save changes" })).toHaveCount(0);
  expect(calls).toEqual(["POST /connections/test-connection"]);                 // tried, refused — and no PUT: nothing saved
  await expect(page.locator(".toast")).toHaveCount(0);                           // and no success message either
});

test("Save anyway saves the refused settings, and says plainly they will not work", async ({ page }) => {
  const calls = await editWithRefusal(page);
  await page.locator(".fld", { hasText: "Host" }).locator("input").first().fill("a-different-host.internal");
  await page.getByRole("button", { name: "Save changes" }).click();
  await page.getByRole("button", { name: "Save anyway" }).click();

  await expect(page.locator(".toast").last()).toContainText("the source refused these settings");
  expect(calls).toEqual(["POST /connections/test-connection", "PUT /connections/conn-verif-1"]);   // saved without a second probe
});

test("changing a setting after a refusal clears the warning, and the next Save tries the connection again", async ({ page }) => {
  const calls = await editWithRefusal(page);
  const host = page.locator(".fld", { hasText: "Host" }).locator("input").first();
  await host.fill("a-different-host.internal");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("button", { name: "Save anyway" })).toBeVisible();

  await host.fill("another-host.internal");
  await expect(page.getByRole("button", { name: "Save anyway" })).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "were not saved" })).toHaveCount(0);
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("button", { name: "Save anyway" })).toBeVisible();
  expect(calls.filter((c) => c.startsWith("POST /connections/test-connection")).length).toBe(2);
});

test("an edit that does not change how it connects (just the name) is saved without trying the connection", async ({ page }) => {
  const calls = await editWithRefusal(page);
  await page.locator("#conn-name").fill("renamed-connection");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.locator(".toast").last()).toBeVisible();
  expect(calls).toEqual(["PUT /connections/conn-verif-1"]);                      // no probe: nothing about the connecting changed
});


test("a truncated discovery run says it is not everything (GAP-10)", async ({ page }) => {
  const state = freshState();
  state.discoveryTruncated = true;
  state.connections.push({
    id: "conn-existing-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb" }, status: "active",
  });
  state.discoveryObjects.push(
    { id: "obj-1", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "customers" },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Discover"]').click();

  await expect(page.locator(".modal").getByRole("status")).toContainText("more objects than a discovery run keeps");
  await expect(page.locator(".modal")).toContainText("1+ total");
});

test("a complete discovery run shows no truncation notice (GAP-10)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-existing-1", org_id: "org_test", name: "sales-db", source_type: "postgres",
    connection_config: { hostname: "db.internal.test", database: "salesdb" }, status: "active",
  });
  state.discoveryObjects.push(
    { id: "obj-1", discovery_run_id: "conn-existing-1", object_type: "table", schema_name: "public", name: "customers" },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('.ai[title="Discover"]').click();

  await expect(page.locator(".modal")).toContainText("1 total");
  await expect(page.locator(".modal").getByRole("status")).toHaveCount(0);
});

test("a source type's connections are all on one page: header and column titles stay put, the rows scroll, and there is no pager", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 24; i++) {
    state.connections.push({
      id: `c-${i}`, org_id: "org_test", name: `postgres${i}`, source_type: "postgresql",
      connection_config: { hostname: "host.docker.internal", port: 5432, database: "sql_practice", username: "u" },
      status: "active", verification_status: "ok",
    });
  }
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 700 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgresql$/ }) }).click();

  await expect(page.locator(".tbody .tr")).toHaveCount(24);
  await expect(page.locator(".pager")).toHaveCount(0);
  await expect(page.getByText("Rows per page")).toHaveCount(0);

  const tbody = page.locator(".tbody");
  expect(await tbody.evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);        // the rows scroll…
  const headerTop = (await page.locator(".thead").boundingBox())!.y;
  await tbody.evaluate((e) => { e.scrollTop = e.scrollHeight; });
  expect((await page.locator(".thead").boundingBox())!.y).toBe(headerTop);                 // …the header does not move
  await expect(page.locator(".tbody .tr", { hasText: "postgres9" })).toBeVisible();
  await expect(page.locator(".tbody .tr", { hasText: "postgres24" })).toBeVisible();
});
