/*
 * Pipeline creation + lifecycle action tests — the wizard and per-row
 * deploy/pause/resume/restart/delete actions in src/pages/Pipelines.tsx.
 */
import { test, expect } from "@playwright/test";
import { SINK_TEMPLATE, clickDeploy, freshState, mockBackend, switchRole, choose, rowsOf, rowAction, menuItem, rowMenuButton } from "./fixtures";

test("creates a pipeline from a template + connection through the wizard", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  await expect(page.locator(".modal")).toBeVisible();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ingest-sales");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  await expect(page.locator(".tbody")).toContainText("ingest-sales");
});

// FE-BUG-08: the wizard asks the backend for the real topic name (see
// app/services/topic_naming.py, BUG-01) instead of reconstructing it
// itself — this proves the preview actually reflects the identity fields
// as they're edited, not just a static string.
test("the New pipeline wizard shows a live topic name preview from the backend", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  await expect(page.locator(".modal")).toContainText("org_test-raw-crm-salesforce-accounts-api-v1");

  await page.locator(".fld", { hasText: "Domain" }).locator("input").fill("erp");
  await expect(page.locator(".modal")).toContainText("org_test-raw-erp-salesforce-accounts-api-v1");
});

// The topic naming convention is the BACKEND's (app/services/topic_naming.py): seven fields joined with "-" only, so "-" cannot appear
// inside a field. The wizard has no naming logic of its own; these prove it shows what the backend says, including why it refused.
test("the topic preview is the backend's hyphen-joined name, with no dots (naming is backend-driven)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  const preview = page.locator(".modal .ab", { hasText: "Topic:" });
  await expect(preview).toContainText("org_test-raw-crm-salesforce-accounts-api-v1");
  await expect(preview).not.toContainText(".");     // the old "." join is gone
});

test("a field the backend refuses (a hyphen, which is the separator) shows the backend's own explanation, then recovers", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  const source = page.locator(".fld", { hasText: "Source system" }).locator("input");
  await source.fill("sales-force");
  const error = page.getByTestId("topic-preview-error");
  await expect(error).toContainText("'-' is the separator");
  await expect(error).toContainText("use '_' instead, e.g. 'sales_force'");   // actionable, from the backend — not a generic "check the fields"
  await expect(page.locator(".modal .ab", { hasText: "Topic:" })).toHaveCount(0);

  await source.fill("sales_force");
  await expect(page.locator(".modal")).toContainText("org_test-raw-crm-sales_force-accounts-api-v1");
  await expect(error).toHaveCount(0);
});

test("when the preview cannot be reached at all the wizard stays generic (it only quotes the backend for a 4xx)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);
  await page.route("**/pipelines/topic-name-preview**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "BACKEND_UNAVAILABLE", message: "db at 10.0.0.5 down", retryable: true }) }));
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  const error = page.getByTestId("topic-preview-error");
  await expect(error).toContainText("Couldn't preview the topic name");
  await expect(error).not.toContainText("10.0.0.5");
});

test("choosing the THRESHOLD error-handling policy sends its configured params", async ({ page }) => {
  const state = freshState();
  state.templates.push(SINK_TEMPLATE); // THRESHOLD counts a dead-letter queue, which only a sink connector has
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  let capturedBody: Record<string, unknown> | null = null;
  page.on("request", (req) => {
    if (req.url().endsWith("/pipelines") && req.method() === "POST") capturedBody = req.postDataJSON();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  await expect(page.locator(".modal")).toBeVisible();
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-jdbc-sink");
  await page.locator("#pipe-sink-topic").fill("org_test-raw-sales-pg-orders-batch-v1");
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ingest-threshold");
  await choose(page.locator(".fld", { hasText: "On a bad record" }).locator(".ss-trigger"), "THRESHOLD");
  await page.locator(".fld", { hasText: "Max errors" }).locator("input").fill("50");
  await page.locator(".fld", { hasText: "Window (seconds)" }).locator("input").fill("120");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(capturedBody).not.toBeNull();
  expect(capturedBody!.error_handling_policy).toBe("THRESHOLD");
  expect(capturedBody!.error_threshold_max_errors).toBe(50);
  expect(capturedBody!.error_threshold_window_seconds).toBe(120);
});

test("a source pipeline defaults to skip-and-log and is never offered a dead-letter queue", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  let capturedBody: Record<string, unknown> | null = null;
  page.on("request", (req) => {
    if (req.url().endsWith("/pipelines") && req.method() === "POST") capturedBody = req.postDataJSON();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  // The default template is a SOURCE: Kafka Connect gives it no dead-letter queue.
  const policy = page.locator("#pipe-err-policy");
  await expect(await rowsOf(policy)).toHaveText([
    "Skip bad records & log them (no dead-letter queue)", "Fail immediately",
  ]);
  await expect(policy).toHaveAttribute("data-value", "SKIP_AND_LOG");
  await expect(page.getByText("only provides a dead-letter queue for sink connectors")).toBeVisible();

  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ingest-default");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(capturedBody!.error_handling_policy).toBe("SKIP_AND_LOG");
  expect(capturedBody!.error_threshold_max_errors).toBeUndefined();
  expect(capturedBody!.error_threshold_window_seconds).toBeUndefined();
});

test("a sink pipeline defaults to the dead-letter queue and can choose any of the four policies", async ({ page }) => {
  const state = freshState();
  state.templates.push(SINK_TEMPLATE);
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  let capturedBody: Record<string, unknown> | null = null;
  page.on("request", (req) => {
    if (req.url().endsWith("/pipelines") && req.method() === "POST") capturedBody = req.postDataJSON();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-jdbc-sink");

  const policy = page.locator("#pipe-err-policy");
  await expect(await rowsOf(policy)).toHaveCount(4);
  await expect(policy).toHaveAttribute("data-value", "DLQ_AND_CONTINUE");

  await page.locator("#pipe-sink-topic").fill("org_test-raw-sales-pg-orders-batch-v1");
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("sink-default");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(capturedBody!.error_handling_policy).toBe("DLQ_AND_CONTINUE");
});

// FE-BUG-23: the Designer must not assume every connector supports every
// mode/DLQ routing/SMT — it has to read that from the selected template's
// own capabilities instead. tpl-postgres-cdc (freshState's default, and
// every other test above's implicit template) is deliberately left
// unrestricted (no supported_modes/capabilities set) so none of those tests
// needed touching — this template is the one exercising the restricted
// path.
const RESTRICTED_TEMPLATE = {
  id: "tpl-legacy-file",
  name: "Legacy File Source",
  connector_type: "source",
  category: "file",
  connector_class: "org.apache.kafka.connect.file.FileStreamSourceConnector",
  runtime_pool_type: "kafka-connect",
  config_schema: {},
  default_config: {},
  default_transforms: [],
  supported_transforms: [],
  version: "v1",
  is_active: true,
  supported_modes: ["batch"],
  supports_dlq: false,
  supports_schema_discovery: false,
  capabilities: { cdc: false, snapshot: false, schemaDiscovery: false, supportsSMT: false, supportsDLQ: false },
};

test("the Designer only offers modes and DLQ options the selected template's capabilities allow (FE-BUG-23)", async ({ page }) => {
  const state = freshState();
  state.templates.push(RESTRICTED_TEMPLATE, SINK_TEMPLATE);
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  // Precise ids, not a text-based .fld locator: "Mode" is also a substring
  // of "Snapshot mode" (the default template's own initial-load section),
  // which would otherwise double-count that select's options too.
  const modeSelect = page.locator("#pipe-mode");
  const errPolicySelect = page.locator("#pipe-err-policy");

  // Unrestricted default template (tpl-postgres-cdc, first in the list) — every mode is offered; it is a
  // SOURCE, so only the two policies that don't need a dead-letter queue are.
  await expect(await rowsOf(modeSelect)).toHaveCount(7);
  await expect(await rowsOf(errPolicySelect)).toHaveCount(2);

  // A sink with full capabilities gets all four.
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-jdbc-sink");
  await expect(await rowsOf(errPolicySelect)).toHaveCount(4);

  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-legacy-file");

  // Restricted template — only "batch" (its one declared supported_modes entry) remains, and with
  // supportsDLQ: false only the two no-dead-letter policies, skip-and-log first (the default).
  await expect(await rowsOf(modeSelect)).toHaveCount(1);
  await expect(modeSelect).toHaveAttribute("data-value", "batch");
  await expect(await rowsOf(errPolicySelect)).toHaveCount(2);
  await expect(errPolicySelect).toHaveAttribute("data-value", "SKIP_AND_LOG");
  await expect(page.getByText("only provides a dead-letter queue for sink connectors")).toBeVisible();

  // No initial-load/snapshot section for a template with capabilities.snapshot: false,
  // even though its connector_class doesn't match any of the hardcoded
  // Debezium/JDBC/Mongo regexes anyway — the capability flag is checked first.
  await expect(page.locator(".fld", { hasText: "Snapshot mode" })).toHaveCount(0);
});

// Flags only: this template declares NO supported_modes (so "undeclared = unrestricted" would
// show everything) and still lists transforms, yet its capabilities say otherwise. The Designer
// must follow the capability flags themselves, not infer them from those other fields.
const FLAGS_ONLY_TEMPLATE = {
  ...RESTRICTED_TEMPLATE,
  id: "tpl-flags-only",
  name: "Flags Only Source",
  supported_modes: [],
  supported_transforms: [{ type: "MaskField", description: "Mask a field", params: [] }],
  supports_dlq: true,
  capabilities: { cdc: false, snapshot: false, schemaDiscovery: false, supportsSMT: false, supportsDLQ: true },
};

test("the Designer follows the capability flags themselves: no cdc/snapshot modes, no transformations, a schema-discovery note (FE-BUG-23)", async ({ page }) => {
  const state = freshState();
  state.templates.push(FLAGS_ONLY_TEMPLATE);
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  // The unrestricted default template: every mode, transformations, no capability notes.
  await expect(await rowsOf(page.locator("#pipe-mode"))).toHaveCount(7);
  await expect(page.getByText("doesn't support transformations")).toHaveCount(0);
  await expect(page.getByText("doesn't support schema discovery")).toHaveCount(0);

  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-flags-only");

  const modes = await (await rowsOf(page.locator("#pipe-mode"))).allTextContents();
  expect(modes).not.toContain("cdc");
  expect(modes).not.toContain("snapshot");
  expect(modes.length).toBeGreaterThan(0);

  // supportsSMT:false wins over the (stale) transform list: no checkboxes, a plain explanation.
  await expect(page.getByText("doesn't support transformations (SMTs)")).toBeVisible();
  await expect(page.getByLabel("MaskField")).toHaveCount(0);
  await expect(page.getByText("Mask a field")).toHaveCount(0);

  await expect(page.getByText("doesn't support schema discovery")).toBeVisible();

  // And going back to a fully capable template brings everything back.
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-postgres-cdc");
  await expect(await rowsOf(page.locator("#pipe-mode"))).toHaveCount(7);
  await expect(page.getByText("doesn't support transformations")).toHaveCount(0);
  await expect(page.getByText("doesn't support schema discovery")).toHaveCount(0);
});

test("a skipped schema introspection on a connector that can't discover schemas is not reported as an error (FE-BUG-23)", async ({ page }) => {
  const state = freshState();
  state.templates.push(RESTRICTED_TEMPLATE);
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/pipelines", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON();
    return route.fulfill({
      status: 201, contentType: "application/json",
      body: JSON.stringify({
        id: "pipe-new", org_id: "org_test", status: "draft", revision: 1, generated_config: {},
        topic_name: "org_test-raw-x-y-z-batch-v1", connector_name: "c", ...body,
        schema_introspection_status: "skipped",
        schema_introspection_message: "This connector template does not support schema discovery.",
      }),
    });
  });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-legacy-file");
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("legacy-file-pipe");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast", { hasText: "Pipeline created" })).toBeVisible();
  // Expected (the form already said so) — so no red "introspection skipped" toast.
  await expect(page.locator(".toast.err")).toHaveCount(0);
});

test("switching to a template that doesn't support the current mode/DLQ policy corrects both, not just hides the stale option", async ({ page }) => {
  const state = freshState();
  state.templates.push(RESTRICTED_TEMPLATE, SINK_TEMPLATE);
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  let capturedBody: Record<string, unknown> | null = null;
  page.on("request", (req) => {
    if (req.url().endsWith("/pipelines") && req.method() === "POST") capturedBody = req.postDataJSON();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  // Pick "snapshot" and the THRESHOLD dead-letter policy on a fully capable SINK — both valid there, neither
  // valid on the restricted source template.
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-jdbc-sink");
  await choose(page.locator("#pipe-mode"), "snapshot");
  await choose(page.locator("#pipe-err-policy"), "THRESHOLD");
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-legacy-file");

  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ingest-legacy-file");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(capturedBody).not.toBeNull();
  expect(capturedBody!.mode).toBe("batch");
  expect(capturedBody!.error_handling_policy).toBe("SKIP_AND_LOG");  // corrected to the new connector's default
});

// FE-BUG-24: the "Ready to deploy" screen renders exactly what the backend
// reports — no rule about what "ready" means lives in this app's own code
// (see DeployReadinessDialog's own comment) — so this drives it through a
// mocked failing response rather than a real failing check, the same way
// the rest of this file mocks the backend generally.
test("the 'Ready to deploy' screen shows a failed check and keeps Deploy disabled until it's resolved (FE-BUG-24)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-notready", org_id: "org_test", name: "not-ready-pipe", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "not-ready-pipe-connector",
    mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  await page.route("**/api/v1/dataconnect/pipelines/pipe-notready/deploy-readiness", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        pipeline_id: "pipe-notready", topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", ready: false,
        checks: [
          { key: "connection_verified", label: "Connection verified", ok: true, reason: null },
          {
            key: "secret_reference_resolved", label: "Secret reference resolved", ok: false,
            reason: "Secret reference 'vault://dataconnect/org_test/x' did not resolve to a stored secret.",
          },
        ],
      }),
    }),
  );

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "not-ready-pipe" }), "Deploy");

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Ready to deploy");
  await expect(dialog).toContainText("Not ready to deploy");
  await expect(dialog).toContainText("Not ready: Secret reference resolved");
  await expect(dialog).toContainText("did not resolve to a stored secret");
  await expect(dialog.getByRole("button", { name: "Deploy", exact: true })).toBeDisabled();

  await dialog.getByRole("button", { name: "Back" }).click();
  await expect(dialog).toHaveCount(0);
  // Declining at the review screen never submitted a deploy.
  await expect(page.locator(".tr", { hasText: "not-ready-pipe" })).toContainText("draft");
});

test("Re-check runs the backend checks again, and Deploy unlocks once they pass (FE-BUG-24)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-recheck", org_id: "org_test", name: "recheck-pipe", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "recheck-pipe-connector",
    mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  let calls = 0;
  await page.route("**/api/v1/dataconnect/pipelines/pipe-recheck/deploy-readiness", (route) => {
    calls += 1;
    const fixed = calls > 1; // the first look fails; by the second the user has fixed the connection
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pipeline_id: "pipe-recheck", topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", ready: fixed,
        checks: [{
          key: "connection_verified", label: "Connection verified", ok: fixed,
          reason: fixed ? null : "Connection has not been successfully tested (status: error) — use \"Test connection\" first.",
        }],
      }),
    });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "recheck-pipe" }), "Deploy");

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Not ready: Connection verified");
  await expect(dialog.getByRole("button", { name: "Deploy", exact: true })).toBeDisabled();

  await dialog.getByRole("button", { name: "Re-check" }).click();

  await expect(dialog).not.toContainText("Not ready");
  await expect(dialog.getByRole("button", { name: "Deploy", exact: true })).toBeEnabled();
  expect(calls).toBe(2);
});

// FE-BUG-25: the button must go busy ("Deploying…", disabled) the instant
// a click fires — before the (deliberately slow, here) network response
// even lands — so a rapid second click physically can't register. The
// backend's own atomic claim (claim_lifecycle_transition) is the real
// guard against two Operations; this proves the UI-side half of the fix.
test("clicking Deploy immediately shows Deploying… and disables the row's lifecycle buttons (FE-BUG-25)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-slow-deploy", org_id: "org_test", name: "slow-deploy-pipe", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "slow-deploy-pipe-connector",
    mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  let deployCallCount = 0;
  let resolveDeploy: () => void = () => {};
  const deployRequestSeen = new Promise<void>((resolve) => { resolveDeploy = resolve; });
  await page.route("**/api/v1/dataconnect/pipelines/pipe-slow-deploy/deploy", async (route) => {
    deployCallCount += 1;
    resolveDeploy();
    await new Promise((r) => setTimeout(r, 800));
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        id: "op-slow-1", org_id: "org_test", operation_type: "pipeline_deploy",
        resource_type: "pipeline", resource_id: "pipe-slow-deploy", status: "running",
        result: null, error_message: null, error_code: null, retryable: null, correlation_id: null,
      }),
    });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "slow-deploy-pipe" });

  // FE-BUG-24: Deploy goes through the "Ready to deploy" review screen
  // first — clickDeploy clicks through it the same way a real user would.
  await clickDeploy(page, row);
  await deployRequestSeen;

  // Immediate — asserted while the deliberately-slow response is still in
  // flight, before it ever resolves.
  await expect(rowMenuButton(row).locator(".spin")).toBeVisible();          // the three dots turn into a spinner …
  // … and the whole menu is closed to every action, not just Deploy — the backend would reject any of them right now as well.
  await expect(rowMenuButton(row)).toBeDisabled();

  expect(deployCallCount).toBe(1);
});

// FE-BUG-25: "refreshing the page should rediscover the active operation" —
// seeded here exactly as a page LOAD (not a click) would find it: a
// pipeline already sitting in a transitional status with
// active_operation_id set, from an operation this specific page load never
// itself submitted.
test("loading the page rediscovers an already-in-flight deploy — no click needed, and the button starts disabled (FE-BUG-25)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-inflight", org_id: "org_test", name: "inflight-pipe", status: "deploying",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "inflight-pipe-connector",
    mode: "cdc", generated_config: {}, active_operation_id: "op-inflight-1",
  });
  state.operations.push({
    id: "op-inflight-1", org_id: "org_test", operation_type: "pipeline_deploy",
    resource_type: "pipeline", resource_id: "pipe-inflight", status: "running",
    result: null, error_message: null, error_code: null, retryable: null, correlation_id: null,
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "inflight-pipe" });

  await expect(rowMenuButton(row).locator(".spin")).toBeVisible();
  await expect(rowMenuButton(row)).toBeDisabled();
});

test("a paused pipeline offers Resume, not Deploy — the backend would 409 a deploy from paused", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    { id: "pipe-paused", org_id: "org_test", name: "paused-pipe", status: "paused",
      topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "paused-pipe-connector",
      mode: "api", generated_config: {} },
    { id: "pipe-running", org_id: "org_test", name: "running-pipe", status: "running",
      topic_name: "org_test-raw-crm-salesforce-contacts-batch-v1", connector_name: "running-pipe-connector",
      mode: "api", generated_config: {} },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const paused = page.locator(".tr", { hasText: "paused-pipe" });
  const deploy = await menuItem(paused, "Deploy paused-pipe");
  await expect(deploy).toBeDisabled();
  await expect(deploy).toHaveAttribute("title", /pipeline is paused — already deployed — use Resume or Restart/);
  await expect(await menuItem(paused, "Pause paused-pipe")).toBeDisabled();
  await expect(await menuItem(paused, "Resume paused-pipe")).toBeEnabled();
  await expect(await menuItem(paused, "Restart paused-pipe")).toBeEnabled();
  await page.keyboard.press("Escape");

  const running = page.locator(".tr", { hasText: "running-pipe" });
  await expect(await menuItem(running, "Deploy running-pipe")).toBeDisabled();
  await expect(await menuItem(running, "Resume running-pipe")).toBeDisabled();
  await expect(await menuItem(running, "Pause running-pipe")).toBeEnabled();
});

test("runs the full deploy → pause → resume → restart → delete lifecycle", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-seed", org_id: "org_test", name: "lifecycle-pipe", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "lifecycle-pipe-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "lifecycle-pipe" });

  await clickDeploy(page, row);
  await expect(page.locator(".toast").last()).toContainText("deploy");
  await expect(row).toContainText("running");

  await rowAction(row, "Pause");
  await expect(page.locator(".toast").last()).toContainText("pause");
  await expect(row).toContainText("paused");

  await rowAction(row, "Resume");
  await expect(page.locator(".toast").last()).toContainText("resume");

  await rowAction(row, "Restart");
  await expect(page.locator(".toast").last()).toContainText("restart");

  await rowAction(row, "Delete");
  await expect(page.locator(".modal")).toContainText('Delete "lifecycle-pipe"?');
  await expect(page.locator(".modal")).toContainText("stop the Kafka Connect connector");
  await page.getByRole("button", { name: "Delete pipeline" }).click();
  await expect(page.locator(".tbody")).not.toContainText("lifecycle-pipe");
});

test("a deploy failure shows the specific DataConnect reason, not a generic toast (FE-BUG-04)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-fail", org_id: "org_test", name: "PostgreSQL CDC", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "postgres-cdc-connector",
    mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  // Override just the deploy submission + its operation poll — the real
  // backend returns 202 + a queued Operation that later resolves to
  // status: "failed" with error_code/error_message/retryable/correlation_id
  // straight from SafeError's classification (app/services/safe_error.py)
  // of the KafkaConnectConfigError _raise_for_connect raises for a 409 from
  // Kafka Connect (app/services/runtime_orchestrator.py) — this is exactly
  // that shape, hand-built since the fixture's own deploy handler only
  // ever simulates success.
  await page.route("**/api/v1/dataconnect/pipelines/pipe-fail/deploy", async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        id: "op-fail-1", org_id: "org_test", operation_type: "pipeline_deploy",
        resource_type: "pipeline", resource_id: "pipe-fail", status: "queued", result: null,
        error_message: null, error_code: null, retryable: null, correlation_id: null,
      }),
    });
  });
  await page.route("**/api/v1/dataconnect/operations/op-fail-1", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "op-fail-1", org_id: "org_test", operation_type: "pipeline_deploy",
        resource_type: "pipeline", resource_id: "pipe-fail", status: "failed", result: null,
        error_message: 'Kafka Connect error (409): Replication slot "inaiera_orders_01" already exists.',
        error_code: "KAFKA_CONNECT_CONFIG_INVALID", retryable: false, correlation_id: "corr-abc123",
      }),
    });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await clickDeploy(page, page.locator(".tr", { hasText: "PostgreSQL CDC" }));

  // A structured dialog naming the pipeline and Kafka Connect's own reason —
  // not a bare "Request failed"/"deploy failed" toast.
  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Deploy failed");
  await expect(dialog).toContainText("Kafka Connect rejected the connector configuration");
  await expect(dialog).toContainText("PostgreSQL CDC");
  await expect(dialog).toContainText('Replication slot "inaiera_orders_01" already exists');
  // retryable: false → no Retry button offered.
  await expect(dialog.getByRole("button", { name: "Retry" })).toHaveCount(0);

  await dialog.getByRole("button", { name: "View details" }).click();
  await expect(dialog).toContainText("KAFKA_CONNECT_CONFIG_INVALID");
  await expect(dialog).toContainText("corr-abc123");
});

test("the browser losing its polling connection is shown as still-processing, never as a failure (FE-BUG-05)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-flaky", org_id: "org_test", name: "flaky-pipe", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "flaky-pipe-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  // The deploy submission itself succeeds (202 + a queued Operation) — it's
  // every subsequent poll of that operation that fails at the network
  // level (route.abort(), simulating a dropped connection), never
  // resolving to a real terminal status either way. This must render as
  // "still processing" (browser observation), never "Deploy failed"
  // (server state) — the exact conflation FE-BUG-05 flags.
  await page.route("**/api/v1/dataconnect/pipelines/pipe-flaky/deploy", async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        id: "op-flaky-1", org_id: "org_test", operation_type: "pipeline_deploy",
        resource_type: "pipeline", resource_id: "pipe-flaky", status: "queued", result: null,
        error_message: null, error_code: null, retryable: null, correlation_id: null,
      }),
    });
  });
  await page.route("**/api/v1/dataconnect/operations/op-flaky-1", (route) => route.abort());

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await clickDeploy(page, page.locator(".tr", { hasText: "flaky-pipe" }));

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Deploy still in progress");
  await expect(dialog).toContainText("lost connection while this tab was checking on it");
  await expect(dialog).toContainText("It may still be in progress");
  await expect(dialog).toContainText("flaky-pipe");
  // Never the failure dialog's own wording — a materially different claim.
  await expect(dialog).not.toContainText("Deploy failed");
  await expect(dialog.getByRole("button", { name: "Continue monitoring" })).toBeVisible();

  // Clicking it resumes watching the SAME operation, not a resubmission —
  // once it can actually reach the operation again and sees it succeeded,
  // that's what's reported.
  await page.unroute("**/api/v1/dataconnect/operations/op-flaky-1");
  await page.route("**/api/v1/dataconnect/operations/op-flaky-1", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "op-flaky-1", org_id: "org_test", operation_type: "pipeline_deploy",
        resource_type: "pipeline", resource_id: "pipe-flaky", status: "succeeded",
        result: { pipeline_id: "pipe-flaky", status: "running", connector_name: "flaky-pipe-connector", topic_name: "t", runtime: {} },
        error_message: null, error_code: null, retryable: null, correlation_id: null,
      }),
    });
  });
  await dialog.getByRole("button", { name: "Continue monitoring" }).click();
  await expect(page.locator(".toast").last()).toContainText("deploy");
  await expect(dialog).toHaveCount(0);
});

test("the runtime drawer distinguishes desired vs. observed state instead of a single collapsed status (FE-BUG-07)", async ({ page }) => {
  const now = new Date();
  const twelveSecondsAgo = new Date(now.getTime() - 12_000).toISOString();
  const eightMinutesAgo = new Date(now.getTime() - 8 * 60_000).toISOString();

  const state = freshState();
  state.pipelines.push(
    {
      id: "pipe-converged", org_id: "org_test", name: "converged-pipe", status: "running",
      topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "converged-pipe-connector",
      mode: "api", generated_config: {},
      desired_state: "running", observed_state: "running", last_observed_at: twelveSecondsAgo, runtime_error: null,
    },
    {
      id: "pipe-syncing", org_id: "org_test", name: "syncing-pipe", status: "paused",
      topic_name: "org_test-raw-crm-salesforce-contacts-batch-v1", connector_name: "syncing-pipe-connector",
      mode: "api", generated_config: {},
      desired_state: "running", observed_state: "paused", last_observed_at: twelveSecondsAgo, runtime_error: null,
    },
    {
      id: "pipe-unreachable", org_id: "org_test", name: "unreachable-pipe", status: "running",
      topic_name: "org_test-raw-crm-salesforce-leads-batch-v1", connector_name: "unreachable-pipe-connector",
      mode: "api", generated_config: {},
      desired_state: "running", observed_state: "unknown", last_observed_at: eightMinutesAgo,
      runtime_error: "Could not reach Kafka Connect.",
    },
    {
      id: "pipe-never-deployed", org_id: "org_test", name: "draft-pipe", status: "draft",
      topic_name: "org_test-raw-crm-salesforce-deals-batch-v1", connector_name: "draft-pipe-connector",
      mode: "api", generated_config: {},
    },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  // FE-BUG-19: a never-deployed pipeline still gets the detail affordance —
  // it has real, read-only configuration to show even with no runtime
  // state yet (see the "never been deployed" assertion below).
  await rowAction(page.locator(".tr", { hasText: "draft-pipe" }), "View pipeline detail");
  await expect(page.locator(".modal")).toContainText("Configuration");
  await expect(page.locator(".modal")).toContainText("never been deployed");
  await page.locator(".modal .ff").getByRole("button", { name: "Close" }).click();

  // Converged: desired == observed, a real fresh observation -> "Last synced".
  await rowAction(page.locator(".tr", { hasText: "converged-pipe" }), "View pipeline detail");
  let dialog = page.locator(".modal");
  await expect(dialog).toContainText("RUNNING");
  await expect(dialog).toContainText("Last synced");
  // Not a hardcoded "12 sec ago" — this test now opens an earlier drawer
  // first (FE-BUG-19's draft-pipe step above), so real wall-clock time has
  // moved on by the time this assertion runs; relativeTime's own rounding
  // means any single-digit-or-low-double-digit "sec ago" value is correct.
  await expect(dialog).toContainText(/\d+ sec ago/);
  await expect(dialog).not.toContainText("Synchronizing");
  await dialog.locator(".ff").getByRole("button", { name: "Close" }).click();

  // Mid-reconciliation: desired != observed, but observed is a real, known
  // state (not unknown) -> "Synchronizing...", no stale timestamp shown.
  await rowAction(page.locator(".tr", { hasText: "syncing-pipe" }), "View pipeline detail");
  dialog = page.locator(".modal");
  await expect(dialog).toContainText("Desired");
  await expect(dialog).toContainText("RUNNING");
  await expect(dialog).toContainText("Runtime");
  await expect(dialog).toContainText("PAUSED");
  await expect(dialog).toContainText("Synchronizing...");
  await expect(dialog).not.toContainText("Last synced");
  await dialog.locator(".ff").getByRole("button", { name: "Close" }).click();

  // Kafka Connect unreachable: observed is "unknown" -> "Last observed",
  // an honestly stale timestamp, plus the runtime error surfaced.
  await rowAction(page.locator(".tr", { hasText: "unreachable-pipe" }), "View pipeline detail");
  dialog = page.locator(".modal");
  await expect(dialog).toContainText("UNKNOWN");
  await expect(dialog).toContainText("Last observed");
  await expect(dialog).toContainText("8 min ago");
  await expect(dialog).toContainText("Could not reach Kafka Connect.");
  // Never falsely implies a fresh sync just because the pipeline's own
  // (stale) `status` column still says "running".
  await expect(dialog).not.toContainText("Last synced");
});

test("the list badge never presents a stale database status as live runtime state (FE-BUG-07)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    {
      id: "pipe-stale", org_id: "org_test", name: "stale-pipe", status: "running",
      topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "stale-pipe-connector",
      mode: "api", generated_config: {}, desired_state: "running", observed_state: "unknown",
    },
    {
      id: "pipe-syncing", org_id: "org_test", name: "syncing-pipe", status: "paused",
      topic_name: "org_test-raw-crm-salesforce-contacts-batch-v1", connector_name: "syncing-pipe-connector",
      mode: "api", generated_config: {}, desired_state: "running", observed_state: "paused",
    },
    {
      id: "pipe-draft", org_id: "org_test", name: "draft-pipe", status: "draft",
      topic_name: "org_test-raw-crm-salesforce-deals-batch-v1", connector_name: "draft-pipe-connector",
      mode: "api", generated_config: {},
    },
  );
  await mockBackend(page, state);
  // Kafka Connect only knows about the paused connector; the "running" one is
  // missing from it (e.g. Connect restarted / unreachable for that connector).
  await page.route("**/runtime/flow", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      reachable: true, connect_url: "", bootstrap: "", topics: [], sinks: [], message: "",
      connectors: [{ name: "syncing-pipe-connector", pipeline_id: "pipe-syncing", type: "source", state: "PAUSED", tasks: ["PAUSED"], topics: [] }],
    }),
  }));

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  const stale = page.locator(".tr", { hasText: "stale-pipe" });
  await expect(stale).toContainText("unknown");
  await expect(stale).not.toContainText("running");

  const syncing = page.locator(".tr", { hasText: "syncing-pipe" });
  await expect(syncing).toContainText("paused");
  await expect(syncing).toContainText("→ running");

  // A draft has no runtime to misreport: its database status is the truth.
  await expect(page.locator(".tr", { hasText: "draft-pipe" })).toContainText("draft");
});

test("opening and immediately closing the New pipeline drawer shows no unsaved-changes prompt (FE-BUG-13)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  // The template/connection pickers auto-select their first option and the
  // transform picker resets itself the moment a template is known — none of
  // that is a user edit, so closing right away must not warn.
  await page.locator(".mx").click();
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".unsaved-guard")).toHaveCount(0);
});

test("closing the New pipeline drawer after an edit prompts Unsaved changes; Continue editing preserves the draft (FE-BUG-13)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("in-progress-pipeline");

  // The × icon.
  await page.locator(".mx").click();
  await expect(page.locator(".unsaved-guard")).toBeVisible();
  await expect(page.locator(".unsaved-guard")).toContainText("Unsaved changes");
  await expect(page.locator(".unsaved-guard")).toContainText("You have unsaved pipeline changes.");

  // "Continue editing" dismisses the prompt without losing the draft — the
  // form underneath was never unmounted, just covered.
  await page.getByRole("button", { name: "Continue editing" }).click();
  await expect(page.locator(".unsaved-guard")).toHaveCount(0);
  await expect(page.locator(".modal")).toBeVisible();
  await expect(page.locator(".fld", { hasText: "Name" }).locator("input")).toHaveValue("in-progress-pipeline");

  // The footer Cancel button is a different close path but goes through the
  // exact same guard.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".unsaved-guard")).toBeVisible();

  // "Discard" actually closes it this time.
  await page.getByRole("button", { name: "Discard" }).click();
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".unsaved-guard")).toHaveCount(0);
});

test("clicking the dimmed backdrop while the pipeline form is dirty also prompts Unsaved changes (FE-BUG-13)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("in-progress-pipeline");

  // Click the dimmed backdrop itself (the `.ov` element outside the `.modal`
  // panel) — a different code path (Modal.tsx's own onMouseDown) than the ×
  // icon or the footer Cancel button, but routed through the same guard.
  await page.locator(".ov").click({ position: { x: 5, y: 5 } });
  await expect(page.locator(".unsaved-guard")).toBeVisible();
  await page.getByRole("button", { name: "Continue editing" }).click();
  await expect(page.locator(".modal")).toBeVisible();
});

test("successfully creating a pipeline closes the drawer without an unsaved-changes prompt (FE-BUG-13)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ingest-sales-2");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".unsaved-guard")).toHaveCount(0);
});

test("a dirty pipeline draft arms the browser's own close/reload confirmation, a clean one doesn't (FE-BUG-13)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres",
    connection_config: {}, status: "active",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  // Dispatching the event directly and reading back whether the handler
  // called preventDefault()/set returnValue is what actually decides
  // whether the browser shows its own native confirmation — a real
  // navigation-triggered dialog is what a user sees, but this is the exact
  // contract our beforeunload handler is responsible for.
  const armsBeforeUnload = () => page.evaluate(() => {
    // A plain Event's `returnValue` already defaults to `true` (it's the
    // legacy cancellation flag, not an app value) — so the only reliable
    // signal that our handler actually ran is `defaultPrevented`, or
    // `returnValue` having been overwritten to the empty string the handler
    // sets, not merely "is defined".
    const evt = new Event("beforeunload", { cancelable: true }) as Event & { returnValue: unknown };
    window.dispatchEvent(evt);
    return evt.defaultPrevented || evt.returnValue === "";
  });

  // Clean: none of the auto-defaulting on open counts as a user edit.
  expect(await armsBeforeUnload()).toBe(false);

  // Dirty: an actual edit arms it.
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("in-progress-pipeline");
  expect(await armsBeforeUnload()).toBe(true);
});

// NEW-01: a quality policy is applied in a stated mode. Observe counts failures and copies them to quarantine while the data topic still
// carries every record; Enforce sends only valid records to the prepared topic. The form must let the user choose, say what each means,
// and never offer Enforce without a policy.
test("the quality mode is disabled until a policy is chosen, defaults to Observe, and Enforce is sent with the policy (NEW-01)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.url().endsWith("/pipelines") && req.method() === "POST") bodies.push(req.postDataJSON()); });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();

  const mode = page.locator("#pipe-quality-mode");
  await expect(mode).toBeDisabled();                                   // no policy yet
  await expect(mode).toHaveAttribute("data-value", "OBSERVE");
  await expect(page.locator("#pipe-quality-mode-help")).toContainText("Choose a quality check");

  await choose(page.locator("#pipe-quality"), "qp-1");
  await expect(mode).toBeEnabled();
  await expect(mode).toHaveAttribute("data-value", "OBSERVE");                           // the default: what pipelines always did
  await expect(page.locator("#pipe-quality-mode-help")).toContainText("still carries every record");

  await choose(mode, "ENFORCE");
  await expect(page.locator("#pipe-quality-mode-help")).toContainText("prepared topic");
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("enforced-orders");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(bodies).toHaveLength(1);
  expect(bodies[0]!.quality_policy_id).toBe("qp-1");
  expect(bodies[0]!.quality_mode).toBe("ENFORCE");
});

// A SINK that enforces writes only valid records to its target and sends the failing ones to quarantine. It has no prepared topic for
// anyone to read (the hand-off between its check and its connector is internal), so the form must not talk about one.
test("for a sink, the quality mode speaks of the target and quarantine, never of a prepared topic", async ({ page }) => {
  const state = freshState();
  state.templates.push(SINK_TEMPLATE);
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await choose(page.locator(".fld", { hasText: "Connector template" }).locator(".ss-trigger"), "tpl-jdbc-sink");
  await choose(page.locator("#pipe-quality"), "qp-1");
  const help = page.locator("#pipe-quality-mode-help");
  await expect(help).toContainText("Every record is written to the target");

  await choose(page.locator("#pipe-quality-mode"), "ENFORCE");
  await expect(help).toContainText("failing records go to quarantine only and never reach it");
  await expect(help).not.toContainText("prepared topic");
  await expect(page.locator(".modal")).not.toContainText("_dataconnect.gate");
});

test("a pipeline with no policy sends no quality mode, and dropping the policy resets Enforce to Observe (NEW-01)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.url().endsWith("/pipelines") && req.method() === "POST") bodies.push(req.postDataJSON()); });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await choose(page.locator("#pipe-quality"), "qp-1");
  await choose(page.locator("#pipe-quality-mode"), "ENFORCE");
  await choose(page.locator("#pipe-quality"), "");               // the policy is removed again
  await expect(page.locator("#pipe-quality-mode")).toHaveAttribute("data-value", "OBSERVE");
  await expect(page.locator("#pipe-quality-mode")).toBeDisabled();

  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("plain-orders");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(bodies[0]!.quality_policy_id).toBeUndefined();
  expect(bodies[0]!.quality_mode).toBeUndefined();                     // nothing to enforce: the backend default (Observe) applies
});

test("editing shows an enforcing pipeline's mode and prepared topic, and saves a switch back to Observe (NEW-01)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.pipelines.push({
    id: "pipe-enf", org_id: "org_test", name: "enforced-orders", status: "draft", mode: "cdc",
    topic_name: "org_test-raw-crm-sf-accounts-cdc-v1", connector_name: "enforced-orders-connector", generated_config: {}, revision: 1,
    error_handling_policy: "SKIP_AND_LOG", quality_policy_id: "qp-1", quality_mode: "ENFORCE",
    prepared_topic: "org_test-prepared-crm-sf-accounts-cdc-v1",
  });
  await mockBackend(page, state);
  const patches: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.method() === "PATCH" && req.url().includes("/pipelines/")) patches.push(req.postDataJSON()); });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "enforced-orders" }), "Edit");

  await expect(page.locator("#pipe-edit-quality-mode")).toHaveAttribute("data-value", "ENFORCE");
  await expect(page.locator("#pipe-edit-quality-mode-help")).toContainText("org_test-prepared-crm-sf-accounts-cdc-v1");

  await choose(page.locator("#pipe-edit-quality-mode"), "OBSERVE");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(patches[0]!.quality_mode).toBe("OBSERVE");
  expect(patches[0]!.quality_policy_id).toBe("qp-1");
});

test("removing the policy from an enforcing pipeline saves Observe with it, so the backend is never asked for Enforce without a policy (NEW-01)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.pipelines.push({
    id: "pipe-enf2", org_id: "org_test", name: "enforced-two", status: "draft", mode: "cdc",
    topic_name: "org_test-raw-crm-sf-leads-cdc-v1", connector_name: "enforced-two-connector", generated_config: {}, revision: 1,
    error_handling_policy: "SKIP_AND_LOG", quality_policy_id: "qp-1", quality_mode: "ENFORCE",
  });
  await mockBackend(page, state);
  const patches: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.method() === "PATCH" && req.url().includes("/pipelines/")) patches.push(req.postDataJSON()); });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "enforced-two" }), "Edit");
  await expect(page.locator("#pipe-edit-quality-mode")).toHaveAttribute("data-value", "ENFORCE");

  await choose(page.locator("#pipe-edit-quality"), "");
  await expect(page.locator("#pipe-edit-quality-mode")).toHaveAttribute("data-value", "OBSERVE");
  await expect(page.locator("#pipe-edit-quality-mode")).toBeDisabled();
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(patches[0]!.quality_policy_id).toBeNull();
  expect(patches[0]!.quality_mode).toBe("OBSERVE");
});

test("an unknown mode from the server is shown as Observe, never as Enforce (NEW-01)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Non-null ids", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.pipelines.push({
    id: "pipe-odd", org_id: "org_test", name: "odd-mode", status: "draft", mode: "cdc",
    topic_name: "org_test-raw-crm-sf-contacts-cdc-v1", connector_name: "odd-connector", generated_config: {}, revision: 1,
    error_handling_policy: "SKIP_AND_LOG", quality_policy_id: "qp-1", quality_mode: "BLOCK_ON_FAILURE",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "odd-mode" }), "Edit");

  await expect(page.locator("#pipe-edit-quality-mode")).toHaveAttribute("data-value", "OBSERVE");
});

test("editing a pipeline saves normally when nothing else changed it (FE-BUG-14)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-edit-1", org_id: "org_test", name: "orders-cdc", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "orders-cdc-connector",
    mode: "cdc", generated_config: {}, revision: 1, error_handling_policy: "DLQ_AND_CONTINUE",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "orders-cdc" }), "Edit");
  await expect(page.locator(".modal")).toContainText("Edit pipeline");

  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("orders-cdc-v2");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("updated");
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".tbody")).toContainText("orders-cdc-v2");
  expect(state.pipelines.find((p) => p.id === "pipe-edit-1")!.revision).toBe(2);
});

test("saving a pipeline that changed underneath shows a conflict prompt instead of silently overwriting it (FE-BUG-14)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-conflict", org_id: "org_test", name: "orders-cdc", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "orders-cdc-connector",
    mode: "cdc", generated_config: {}, revision: 1, error_handling_policy: "DLQ_AND_CONTINUE",
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "orders-cdc" }), "Edit");
  await expect(page.locator(".modal")).toContainText("Edit pipeline");

  // Integrator B saves first — simulated directly against the shared mock
  // state (standing in for a second session's PATCH landing first).
  const server = state.pipelines.find((p) => p.id === "pipe-conflict")!;
  server.name = "orders-cdc-renamed-by-b";
  server.revision = 2;

  // Integrator A, still holding revision 1, tries to save a conflicting edit.
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("orders-cdc-renamed-by-a");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.locator(".modal")).toContainText("This pipeline changed since you opened it.");
  await expect(page.locator(".modal")).toContainText("orders-cdc");
  await expect(page.getByRole("button", { name: "Review latest" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Compare changes" })).toBeVisible();

  // B's save is genuinely intact — A's conflicting request never landed.
  expect(server.name).toBe("orders-cdc-renamed-by-b");
  expect(server.revision).toBe(2);

  await page.getByRole("button", { name: "Compare changes" }).click();
  await expect(page.locator(".modal")).toContainText("What changed");
  await expect(page.locator(".modal .tbody")).toContainText("Name");
  await expect(page.locator(".modal .tbody")).toContainText("orders-cdc-renamed-by-b");

  // "Review latest" closes the drawer — the user has to reopen Edit and
  // reapply anything they still want, never a silent merge/overwrite.
  await page.getByRole("button", { name: "Review latest" }).click();
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".tbody")).toContainText("orders-cdc-renamed-by-b");
});

test("deleting a never-deployed draft shows draft-specific copy, not the connector/topic warning (FE-BUG-15)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-draft", org_id: "org_test", name: "brand-new-draft", status: "draft",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "brand-new-draft-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "brand-new-draft" }), "Delete");

  await expect(page.locator(".modal")).toContainText('Delete "brand-new-draft"?');
  await expect(page.locator(".modal")).toContainText("never been deployed");
  await expect(page.locator(".modal")).not.toContainText("stop the Kafka Connect connector");
  await expect(page.locator(".modal")).not.toContainText("preserve existing Kafka topic data");

  await page.getByRole("button", { name: "Delete pipeline" }).click();
  await expect(page.locator(".tbody")).not.toContainText("brand-new-draft");
});

test("deleting a deployed pipeline explains exactly what happens, including the topic that's preserved (FE-BUG-15)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-deployed", org_id: "org_test", name: "ERP Customer CDC", status: "running",
    topic_name: "org_1001__raw__crm__postgres__products__api__v21",
    connector_name: "erp-customer-cdc-connector", mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "ERP Customer CDC" }), "Delete");

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText('Delete "ERP Customer CDC"?');
  await expect(dialog).toContainText("stop the Kafka Connect connector");
  await expect(dialog).toContainText("remove its DataConnect pipeline definition");
  await expect(dialog).toContainText("preserve existing Kafka topic data");
  await expect(dialog).toContainText("org_1001__raw__crm__postgres__products__api__v21");
  await expect(page.getByRole("button", { name: "Delete pipeline" })).toBeVisible();

  // Cancel leaves the pipeline untouched.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.locator(".tbody")).toContainText("ERP Customer CDC");
});

test("Viewer sees pipeline status but no lifecycle controls", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-seed", org_id: "org_test", name: "readonly-pipe", status: "running",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "readonly-pipe-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(page.locator(".tbody")).toContainText("readonly-pipe");
  await expect(page.getByRole("button", { name: "+ New pipeline" })).toHaveCount(0);
  const row = page.locator(".tr", { hasText: "readonly-pipe" });
  await rowMenuButton(row).click();                                         // a Viewer's menu holds only what a Viewer may do
  await expect(page.getByRole("menuitem")).toHaveText(["View details", "View metrics"]);
});

test("the pipeline detail drawer shows a genuine read-only configuration view, not a hidden or disabled form (FE-BUG-19)", async ({ page }) => {
  const state = freshState();
  state.connections.push({
    id: "conn-erp", org_id: "org_test", name: "erp-prod-db", source_type: "postgres",
    connection_config: { hostname: "erp-db.internal", port: 5432, database: "erp" }, status: "active",
  });
  state.pipelines.push({
    id: "pipe-detail", org_id: "org_test", name: "ERP Customer CDC", status: "draft",
    template_id: "tpl-postgres-cdc", connection_profile_id: "conn-erp",
    topic_name: "org_test-raw-crm-erp-customers-cdc-v1", connector_name: "erp-customer-cdc-connector",
    zone: "raw", mode: "cdc", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "ERP Customer CDC" }), "View pipeline detail");

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Configuration");
  await expect(dialog).toContainText("Source");
  await expect(dialog).toContainText("postgres");
  await expect(dialog).toContainText("Postgres CDC");
  await expect(dialog).toContainText("Host");
  await expect(dialog).toContainText("erp-db.internal");
  await expect(dialog).toContainText("Database");
  await expect(dialog).toContainText("erp");
  await expect(dialog).toContainText("Mode");
  await expect(dialog).toContainText("CDC");
  // Read-only presentation, not a form full of disabled inputs.
  await expect(dialog.locator("input, textarea")).toHaveCount(0);
});


test("the pipeline detail keeps a failed schema introspection visible, with its reason (BUG-10)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    {
      id: "pipe-introspect-failed", org_id: "org_test", name: "introspect-failed-pipe", status: "draft",
      topic_name: "org_test-raw-crm-salesforce-invoices-batch-v1", connector_name: "introspect-failed-connector",
      mode: "api", generated_config: {},
      schema_introspection_status: "failed",
      schema_introspection_message: "Could not introspect schema: the source did not respond within 8s (connection timed out)",
    },
    {
      id: "pipe-introspect-linked", org_id: "org_test", name: "introspect-linked-pipe", status: "draft",
      topic_name: "org_test-raw-crm-salesforce-refunds-batch-v1", connector_name: "introspect-linked-connector",
      mode: "api", generated_config: {},
      schema_introspection_status: "linked_existing", schema_introspection_message: null,
    },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await rowAction(page.locator(".tr", { hasText: "introspect-failed-pipe" }), "View pipeline detail");
  let dialog = page.locator(".modal");
  await expect(dialog).toContainText("Schema introspection");
  await expect(dialog).toContainText("FAILED");
  await expect(dialog.getByRole("alert")).toContainText("did not respond within 8s");
  await dialog.locator(".ff").getByRole("button", { name: "Close" }).click();

  await rowAction(page.locator(".tr", { hasText: "introspect-linked-pipe" }), "View pipeline detail");
  dialog = page.locator(".modal");
  await expect(dialog).toContainText("LINKED TO EXISTING");
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});


test("a sink shows the topic it reads, not a generated output topic (GAP-12)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    {
      id: "pipe-sink", org_id: "org_test", name: "orders-sink", status: "draft", direction: "SINK",
      topic_name: null, catalog_qualified_name: null, connector_name: "orders-sink-connector",
      mode: "batch", generated_config: {}, connector_config: { topics: "org_test-raw-sales-pg-orders-batch-v1" },
    },
    {
      id: "pipe-source", org_id: "org_test", name: "orders-source", status: "draft", direction: "SOURCE",
      topic_name: "org_test-raw-sales-pg-orders-batch-v1", connector_name: "orders-source-connector",
      mode: "batch", generated_config: {}, connector_config: { tables: ["orders"] },
    },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  // The table: the sink's cell is marked as an input, the source's is its own topic.
  await expect(page.locator(".tr", { hasText: "orders-sink" })).toContainText("← org_test-raw-sales-pg-orders-batch-v1");
  await expect(page.locator(".tr", { hasText: "orders-source" })).not.toContainText("←");

  // The detail view labels it as an input.
  await rowAction(page.locator(".tr", { hasText: "orders-sink" }), "View pipeline detail");
  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Input topic");
  await expect(dialog).toContainText("org_test-raw-sales-pg-orders-batch-v1");
  await expect(dialog).not.toContainText("null");
});


test("the pipeline detail names the worker pool its connector runs in (runtime design)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    {
      id: "pipe-cdc", org_id: "org_test", name: "orders-cdc", status: "draft", direction: "SOURCE", runtime_pool_id: "connect-cdc-pool",
      topic_name: "org_test-raw-sales-pg-orders-cdc-v1", connector_name: "orders-cdc-connector", mode: "cdc", generated_config: {},
      connector_config: { tables: ["orders"] },
    },
    {
      id: "pipe-sink", org_id: "org_test", name: "orders-sink", status: "draft", direction: "SINK", runtime_pool_id: "connect-sink-pool",
      topic_name: null, connector_name: "orders-sink-connector", mode: "batch", generated_config: {},
      connector_config: { topics: "org_test-raw-sales-pg-orders-cdc-v1" },
    },
  );
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  // By the exact name cell: with real (hyphenated) topic names the sink's input topic also contains the text "orders-cdc".
  await rowAction(page.locator(".tr").filter({ has: page.getByText("orders-cdc", { exact: true }) }), "View pipeline detail");
  let dialog = page.locator(".modal");
  await expect(dialog).toContainText("Worker pool");
  await expect(dialog.locator(".kv", { hasText: "Worker pool" })).toContainText("CDC");
  await dialog.locator(".ff").getByRole("button", { name: "Close" }).click();

  await rowAction(page.locator(".tr", { hasText: "orders-sink" }), "View pipeline detail");
  dialog = page.locator(".modal");
  await expect(dialog.locator(".kv", { hasText: "Worker pool" })).toContainText("Sink");
});

test("a row's metrics icon opens that pipeline's metrics in a side panel on the same page; its button leads on to Monitoring", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    { id: "p-a", org_id: "org_test", name: "alpha-pipe", status: "running", topic_name: "ta", connector_name: "conn-a", mode: "api", generated_config: {} },
    { id: "p-b", org_id: "org_test", name: "beta-pipe", status: "running", topic_name: "tb", connector_name: "conn-b", mode: "api", generated_config: {} },
  );
  await mockBackend(page, state);
  const asked: string[] = [];
  await page.route("**/api/v1/dataconnect/runtime/connectors/*/metrics*", (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split("/").slice(-2)[0]!);
    asked.push(name);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      connector_name: name, pipeline_id: "p-b", pipeline_name: "beta-pipe", direction: "source", runtime_pool_id: "connect-jdbc-pool", range: "1h", step_seconds: 60,
      observed_at: new Date().toISOString(), prometheus_enabled: true, reachable: true, error: null,
      metrics: [{ id: "source_poll_rate", label: "Records polled", unit: "records/s", description: "", current: 12.5, series: [] }], task_columns: [], tasks: [],
    }) });
  });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  const row = page.locator(".tr", { hasText: "beta-pipe" });
  await rowAction(row, "View metrics");

  const panel = page.locator(".modal");
  await expect(page.locator(".sn.active")).toContainText("Pipelines");                  // still on the Pipelines page
  await expect(panel.locator("#modal-title")).toContainText("beta-pipe");
  await expect(panel.locator("#connector-metrics")).toContainText("Records polled");
  await expect(panel.getByRole("tab", { name: "1h" })).toHaveAttribute("aria-selected", "true");
  expect(asked).toEqual(["conn-b"]);                                                    // only this pipeline's connector

  await panel.getByRole("button", { name: "Open in Monitoring" }).click();             // the way on to the full page
  await expect(page.locator(".sn.active")).toContainText("Monitoring");
  await expect(page.locator(".rail .ri.on")).toContainText("beta-pipe");
  await expect(page.locator(".modal")).toHaveCount(0);
});

test("each row has one three-dots button under the Actions heading; it opens a vertical menu of icon + name lines", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p-a", org_id: "org_test", name: "alpha-pipe", status: "running", topic_name: "ta", connector_name: "conn-a", mode: "api", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  const row = page.locator(".tr", { hasText: "alpha-pipe" });
  const dots = rowMenuButton(row);
  await expect(row.locator(".acts .ai")).toHaveCount(1);                    // one button, not a row of icons
  await expect(dots).toHaveAttribute("aria-haspopup", "menu");
  await expect(dots).toHaveAttribute("aria-expanded", "false");
  const head = (await page.locator(".th", { hasText: "Actions" }).boundingBox())!;
  const box = (await dots.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - (head.x + head.width / 2))).toBeLessThan(6);   // centred under the heading

  await dots.click();
  await expect(dots).toHaveAttribute("aria-expanded", "true");
  const items = page.getByRole("menuitem");
  await expect(items).toHaveText(["View details", "View metrics", "Edit", "Deploy", "Pause", "Resume", "Restart", "Delete"]);
  for (const item of await items.all()) await expect(item.locator("svg")).toHaveCount(1);        // every line has its icon …
  const boxes = await items.evaluateAll((els) => els.map((e) => { const r = e.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) }; }));
  expect(new Set(boxes.map((b) => b.x)).size).toBe(1);                                          // … in one column
  expect(boxes.map((b) => b.y)).toEqual([...boxes.map((b) => b.y)].sort((x, y) => x - y));      // top to bottom, in order
  expect(new Set(boxes.map((b) => b.y)).size).toBe(boxes.length);
  const menu = (await page.getByRole("menu").boundingBox())!;
  expect(menu.y).toBeGreaterThan(box.y);                                                         // under the button
  expect(Math.abs(menu.x + menu.width - (box.x + box.width))).toBeLessThan(2);                   // right edges line up
  await expect(page.getByRole("menuitem", { name: /^Delete / })).toHaveCSS("color", "rgb(185, 28, 28)");   // delete is red
});

test("the menu is keyboard-operable: arrows move, Enter picks, Esc and an outside click close it and focus returns to the button", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p-a", org_id: "org_test", name: "alpha-pipe", status: "draft", topic_name: "ta", connector_name: "conn-a", mode: "api", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "alpha-pipe" });
  const dots = rowMenuButton(row);

  await dots.focus();
  await page.keyboard.press("ArrowDown");                                   // opens, focus on the first line
  await expect(page.getByRole("menuitem", { name: /^View alpha-pipe detail$/ })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: /^View metrics for / })).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: /^Delete / })).toBeFocused();
  await page.keyboard.press("Home");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(dots).toBeFocused();

  await dots.click();
  await page.locator(".ph").click();                                        // elsewhere
  await expect(page.getByRole("menu")).toHaveCount(0);

  await dots.click();
  await page.getByRole("menuitem", { name: /^View alpha-pipe detail$/ }).press("Enter");
  await expect(page.locator(".modal")).toContainText("Configuration");
  await page.keyboard.press("Escape");
  await expect(dots).toBeFocused();                                         // the panel hands focus back to the three dots
});

test("an action the row can't do stays in the menu, greyed, with the reason on hover, and picking it does nothing", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p-a", org_id: "org_test", name: "draft-pipe", status: "draft", topic_name: "ta", connector_name: "conn-a", mode: "api", generated_config: {} });
  await mockBackend(page, state);
  const calls: string[] = [];
  await page.route("**/api/v1/dataconnect/pipelines/p-a/pause", (route) => { calls.push("pause"); return route.fallback(); });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "draft-pipe" });
  const pause = await menuItem(row, "Pause");
  await expect(pause).toHaveAttribute("aria-disabled", "true");
  await expect(pause).toHaveAttribute("title", /Pause unavailable: pipeline is draft/);
  await pause.click({ force: true });
  await expect(page.getByRole("menu")).toBeVisible();                       // nothing happened: the menu is still open
  expect(calls).toEqual([]);
});

// TimestampConverter with a blank required setting used to be created, "deploy" successfully and then fail when its task
// started ("TimestampConverter requires format option ..."). The form now pre-fills Format, marks every placeholder as an
// example, and refuses to create the pipeline while a required setting is blank.
const TIMESTAMP_CONVERTER_SPEC = {
  type: "TimestampConverter", scope: "value", description: "Convert timestamp field formats",
  params: [
    { key: "field", label: "Timestamp field", type: "text", required: true, placeholder: "created_at" },
    { key: "target.type", label: "Target type", type: "select", required: true, default: "string", options: ["string", "unix", "Date", "Time", "Timestamp"] },
    { key: "format", label: "Format (when target is string)", type: "text", required: false, default: "yyyy-MM-dd HH:mm:ss",
      required_when: { key: "target.type", value: "string" }, placeholder: "yyyy-MM-dd HH:mm:ss" },
  ],
};

async function openWizardWithTimestampConverter(page: import("@playwright/test").Page) {
  const state = freshState();
  state.templates[0]!.supported_transforms = [TIMESTAMP_CONVERTER_SPEC];
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("ts-pipe");
  await page.locator("label", { hasText: "TimestampConverter" }).locator("input[type=checkbox]").check();
  return state;
}

test("TimestampConverter: Format is pre-filled, and Create is refused while the Timestamp field is blank", async ({ page }) => {
  const state = await openWizardWithTimestampConverter(page);
  const field = page.locator("#pipe-tf-TimestampConverter-field");
  await expect(page.locator("#pipe-tf-TimestampConverter-format")).toHaveValue("yyyy-MM-dd HH:mm:ss");
  await expect(field).toHaveValue("");
  await expect(field).toHaveAttribute("placeholder", "e.g. created_at");
  await expect(page.locator("label[for='pipe-tf-TimestampConverter-format']")).toContainText("*");

  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator("#pipe-tf-TimestampConverter-field-error")).toContainText("TimestampConverter: Timestamp field is required.");
  await expect(field).toHaveAttribute("aria-invalid", "true");
  await expect(field).toBeFocused();
  expect(state.transforms).toHaveLength(0);
  expect(state.pipelines).toHaveLength(0);

  await field.fill("created_at");
  await expect(page.locator("#pipe-tf-TimestampConverter-field-error")).toHaveCount(0);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect((state.transforms[0] as { transforms: { config: Record<string, string> }[] }).transforms[0]!.config)
    .toEqual({ field: "created_at", "target.type": "string", format: "yyyy-MM-dd HH:mm:ss" });
  expect((state.transforms[0] as { origin?: string }).origin).toBe("auto");               // the policy the pipeline form made says so: the Transforms page shows it as Type "Auto"
});

test("TimestampConverter: a string target with Format cleared is refused; a unix target doesn't need one", async ({ page }) => {
  const state = await openWizardWithTimestampConverter(page);
  await page.locator("#pipe-tf-TimestampConverter-field").fill("created_at");
  await page.locator("#pipe-tf-TimestampConverter-format").fill("");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator("#pipe-tf-TimestampConverter-format-error")).toContainText("Format (when target is string) is required");
  expect(state.pipelines).toHaveLength(0);

  await choose(page.locator("#pipe-tf-TimestampConverter-target\\.type"), "unix");
  await expect(page.locator("label[for='pipe-tf-TimestampConverter-format']")).not.toContainText("*");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect((state.transforms[0] as { transforms: { config: Record<string, string> }[] }).transforms[0]!.config)
    .toEqual({ field: "created_at", "target.type": "unix" });
});

// The status badge says what Kafka Connect reports; it needs no "●live" tag beside it (the unknown case still says "not live").
test("a running pipeline's status shows just the status — no '●live' tag", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "pipe-run", org_id: "org_test", name: "run-pipe", status: "running", topic_name: "t", connector_name: "run-conn", mode: "batch", generated_config: {}, desired_state: "running", observed_state: "running" });
  await mockBackend(page, state);
  await page.route("**/runtime/flow", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ reachable: true, connect_url: "", bootstrap: "", topics: [], sinks: [], message: "", connectors: [{ name: "run-conn", pipeline_id: "pipe-run", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }] }),
  }));
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "run-pipe" });
  await expect(row).toContainText("running");
  await expect(row).not.toContainText("live");
});
