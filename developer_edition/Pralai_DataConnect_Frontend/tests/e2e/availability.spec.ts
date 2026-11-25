/*
 * NEW-09: the UI shows what the backend says can actually be executed. QUALITY options that are `unavailable` are DISABLED (with their reason). CONNECTORS are
 * never disabled or hidden: the environment this app is deployed to may have plugins this one lacks, so every connector is selectable and an unavailable one only
 * gets a heads-up (deploy itself checks the plugin). These tests drive the real forms against a
 * mocked backend and check both what is shown and what is sent.
 */
import { test, expect } from "@playwright/test";
import { QUALITY_CAPABILITIES, SINK_TEMPLATE, freshState, mockBackend, choose, openList } from "./fixtures";

const AVAILABLE = { state: "supported", code: null, message: null, remediation: null, verified: true };
const MISSING = {
  state: "unavailable", code: "PLUGIN_NOT_INSTALLED", verified: true,
  message: "Connector class 'com.example.Gone' is not installed on the Kafka Connect worker.", remediation: "Install the connector plugin on the Kafka Connect worker.",
};
const NEEDS_CONFIG = {
  state: "requires_configuration", code: "RUNTIME_UNREACHABLE", verified: false,
  message: "Kafka Connect could not be reached, so whether this connector can run could not be confirmed.", remediation: "Check that Kafka Connect is running.",
};

const tpl = (id: string, name: string, availability?: unknown) => ({ ...SINK_TEMPLATE, id, name, ...(availability === undefined ? {} : { availability }) });

async function openNewPipeline(page: import("@playwright/test").Page, templates: unknown[]) {
  const state = freshState();
  state.templates.splice(0, state.templates.length, ...(templates as Record<string, unknown>[]));      // exactly these: the default fixture template is not part of the scenario
  state.connections.push({ id: "conn-seed", org_id: "org_test", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();
  return state;
}

// ── Connectors ─────────────────────────────────────────────────────────────────────────────────────────────────────────

test("every connector is selectable and listed by its plain name, whatever the backend says about it", async ({ page }) => {
  await openNewPipeline(page, [tpl("a", "Alpha sink", AVAILABLE), tpl("g", "Gone sink", MISSING), tpl("n", "Needs sink", NEEDS_CONFIG)]);

  const list = await openList(page.locator("#pipe-template"));
  await expect(list.rows).toHaveCount(3);
  for (const [value, name] of [["a", "Alpha sink"], ["g", "Gone sink"], ["n", "Needs sink"]] as const) {
    await expect(list.row(value)).toBeEnabled();                 // none is disabled…
    await expect(list.row(value)).toHaveText(name);               // …and none is relabelled "unavailable"
  }
});

test("the connector list stays alphabetical: available and unavailable are not separated", async ({ page }) => {
  await openNewPipeline(page, [tpl("1", "Aaa unavailable", MISSING), tpl("2", "Bbb ok", AVAILABLE), tpl("3", "Ccc unavailable", MISSING), tpl("4", "Ddd ok", AVAILABLE)]);

  const list = await openList(page.locator("#pipe-template"));
  await expect(list.rows).toHaveText(["Aaa unavailable", "Bbb ok", "Ccc unavailable", "Ddd ok"]);
});

test("the form starts on a connector the backend says can run, when there is one", async ({ page }) => {
  await openNewPipeline(page, [tpl("first", "First but gone", MISSING), tpl("second", "Second but unconfirmed", NEEDS_CONFIG), tpl("third", "Third is fine", AVAILABLE)]);

  await expect(page.locator("#pipe-template")).toHaveAttribute("data-value", "third");
  await expect(page.locator("#pipe-template-availability")).toHaveCount(0);                  // nothing to warn about
});

test("the form starts on JDBC Source whenever that template exists, wherever it is in the list", async ({ page }) => {
  await openNewPipeline(page, [
    tpl("aaa_first", "Aaa first", AVAILABLE), tpl("tpl_debezium_postgres", "Debezium PostgreSQL CDC", AVAILABLE),
    tpl("tpl_jdbc_source", "JDBC Source", MISSING), tpl("zzz_last", "Zzz last", AVAILABLE),
  ]);

  await expect(page.locator("#pipe-template")).toHaveAttribute("data-value", "tpl_jdbc_source");              // even though another connector is first, and this one is flagged
});

test("a connector that needs configuration can be chosen and the reason is shown", async ({ page }) => {
  await openNewPipeline(page, [tpl("ok", "Alpha", AVAILABLE), tpl("n", "Needs sink", NEEDS_CONFIG)]);

  await choose(page.locator("#pipe-template"), "n");

  const note = page.locator("#pipe-template-availability");
  await expect(note).toBeVisible();
  await expect(note).toContainText("Could not be confirmed");                                // verified:false is said plainly
  await expect(note).toContainText("Kafka Connect could not be reached");
  await expect(note).toContainText("Check that Kafka Connect is running.");                  // and what to do about it
  await expect(note).toHaveAttribute("data-availability-code", "RUNTIME_UNREACHABLE");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();     // not blocked: choosing it is not wrong
});

test("an unavailable connector can be chosen: the reason is shown as a heads-up and Create is NOT blocked", async ({ page }) => {
  await openNewPipeline(page, [tpl("g1", "Gone one", MISSING), tpl("g2", "Gone two", MISSING)]);

  const note = page.locator("#pipe-template-availability");
  await expect(note).toContainText("is not installed on the Kafka Connect worker");
  await expect(note).toContainText("Install the connector plugin");
  await expect(note).toContainText("Heads up");
  await expect(note).toHaveAttribute("role", "status");                                     // a warning, not an error alert
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
});

test("a connector with no availability information is left selectable and unflagged", async ({ page }) => {
  await openNewPipeline(page, [tpl("plain", "Plain sink"), tpl("junk", "Junk availability", { state: "maybe" })]);

  const list = await openList(page.locator("#pipe-template"));
  await expect(list.row("plain")).toBeEnabled();
  await expect(list.row("junk")).toBeEnabled();           // an unreadable block is "no information", not "unavailable"
  await expect(page.locator("#pipe-template-availability")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeEnabled();
});

test("a pipeline can be created from a connector the backend calls unavailable, and it sends that connector", async ({ page }) => {
  await openNewPipeline(page, [tpl("gone", "Aaa gone", MISSING), tpl("ok", "Zzz ok", AVAILABLE)]);
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.url().endsWith("/pipelines") && req.method() === "POST") bodies.push(req.postDataJSON()); });

  await choose(page.locator("#pipe-template"), "gone");
  await page.locator("#pipe-sink-topic").fill("org_test-raw-sales-pg-orders-batch-v1");
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("uses-the-unavailable-connector");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(bodies[0]!.template_id).toBe("gone");
});

// ── Quality policy options ─────────────────────────────────────────────────────────────────────────────────────────────

async function openNewPolicy(page: import("@playwright/test").Page, state = freshState()) {
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();
  await expect(page.locator("#quality-on-failure")).toBeVisible();
  return state;
}

test("unavailable failure actions and quarantine destinations are disabled with their state, from the backend's answer (NEW-09)", async ({ page }) => {
  await openNewPolicy(page);

  const onFailure = await openList(page.locator("#quality-on-failure"));
  await expect(onFailure.rows).toHaveCount(4);                                                 // all four are listed ...
  await expect(onFailure.row("route_to_quarantine")).toBeEnabled();
  await expect(onFailure.row("log")).toBeEnabled();
  for (const id of ["drop", "block"]) {
    await expect(onFailure.row(id)).toBeDisabled();                                            // ... the two that do not exist are disabled
    await expect(onFailure.row(id)).toContainText("unavailable");
  }
  await page.keyboard.press("Escape");
  const sinks = await openList(page.locator("#quality-quarantine-sink"));
  await expect(sinks.row("kafka")).toBeEnabled();
  await expect(sinks.row("object_storage")).toBeDisabled();
  await expect(sinks.row("object_storage")).toHaveText("Object storage — unavailable");
});

test("the defaults are the supported choices, and what is sent is what was chosen (NEW-09)", async ({ page }) => {
  await openNewPolicy(page);
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.url().endsWith("/quality-policies") && req.method() === "POST") bodies.push(req.postDataJSON()); });
  await expect(page.locator("#quality-on-failure")).toHaveAttribute("data-value", "route_to_quarantine");
  await expect(page.locator("#quality-quarantine-sink")).toHaveAttribute("data-value", "kafka");

  await choose(page.locator("#quality-on-failure"), "log");
  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("Logged only");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "Save Quality Check" }).click();

  await expect(page.locator(".toast").last()).toContainText("Quality check created");
  expect(bodies[0]).toMatchObject({ on_failure: "log", quarantine_sink: "kafka" });
});

test("a destination that needs configuration is selectable and says why, and the policy can still be saved (NEW-09)", async ({ page }) => {
  const state = freshState();
  const needsKafka = { state: "requires_configuration", code: "KAFKA_BOOTSTRAP_MISSING", message: "Quarantining to Kafka needs a Kafka bootstrap server, and none is set.",
    remediation: "Set DATACONNECT_KAFKA_BOOTSTRAP.", verified: true };
  state.qualityCapabilities = {
    quarantine_sinks: [{ ...QUALITY_CAPABILITIES.quarantine_sinks[0]!, availability: needsKafka }, QUALITY_CAPABILITIES.quarantine_sinks[1]!],
    failure_actions: [{ ...QUALITY_CAPABILITIES.failure_actions[0]!, availability: needsKafka }, ...QUALITY_CAPABILITIES.failure_actions.slice(1)],
  };
  await openNewPolicy(page, state);

  const sinks = await openList(page.locator("#quality-quarantine-sink"));
  await expect(sinks.row("kafka")).toBeEnabled();
  await expect(sinks.row("kafka")).toHaveText("Kafka topic — needs configuration");
  await page.keyboard.press("Escape");
  await expect(page.locator("#quality-quarantine-sink-availability")).toContainText("Set DATACONNECT_KAFKA_BOOTSTRAP.");
  await expect(page.locator("#quality-quarantine-sink-availability")).toHaveAttribute("data-availability-code", "KAFKA_BOOTSTRAP_MISSING");
  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("Needs kafka");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "Save Quality Check" }).click();
  await expect(page.locator(".toast").last()).toContainText("Quality check created");
});

test("if the capabilities cannot be loaded only the options that have always worked are offered (NEW-09)", async ({ page }) => {
  const state = freshState();
  state.qualityCapabilities = "fail";
  await openNewPolicy(page, state);
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (req) => { if (req.url().endsWith("/quality-policies") && req.method() === "POST") bodies.push(req.postDataJSON()); });

  const onFailure = await openList(page.locator("#quality-on-failure"));
  await expect(onFailure.rows).toHaveText(["Route to quarantine", "Log and count only"]);
  await page.keyboard.press("Escape");
  const sinks = await openList(page.locator("#quality-quarantine-sink"));
  await expect(sinks.rows).toHaveText(["Kafka topic"]);          // never an option the API might refuse
  await page.keyboard.press("Escape");
  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("Fallback");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "Save Quality Check" }).click();

  await expect(page.locator(".toast").last()).toContainText("Quality check created");
  expect(bodies[0]).toMatchObject({ on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
});

test("a response with unreadable options falls back safely instead of breaking the form (NEW-09)", async ({ page }) => {
  const state = freshState();
  state.qualityCapabilities = { quarantine_sinks: "nonsense", failure_actions: [{ id: "x" }, { id: "y", availability: { state: "maybe" } }, null, 5] };
  await openNewPolicy(page, state);

  const onFailure = await openList(page.locator("#quality-on-failure"));
  await expect(onFailure.rows).toHaveText(["Route to quarantine", "Log and count only"]);
  await page.keyboard.press("Escape");
  const sinks = await openList(page.locator("#quality-quarantine-sink"));
  await expect(sinks.rows).toHaveText(["Kafka topic"]);
});
