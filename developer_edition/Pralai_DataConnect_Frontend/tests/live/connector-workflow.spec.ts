/*
 * FE-04: the connector workflow, end to end, with NOTHING mocked. The browser drives this UI; the UI talks to the real FastAPI backend, which uses the
 * real PostgreSQL, Kafka and Kafka Connect of the Docker Compose stack. After each UI step the test looks at the real system underneath (the Connect
 * REST API, the topics, the source database) so a passing screen is proven against what actually happened.
 *
 *   connection -> discovery -> quality policy -> pipeline -> deploy -> quarantine -> failure + restart -> schema drift -> cleanup
 *
 * Run it with `npm run test:live` (or scripts/live-e2e.sh, which also starts the stack). Skipped unless DATACONNECT_LIVE_E2E=1.
 */
import { test, expect, type Page } from "@playwright/test";
import { choose } from "../e2e/fixtures";
import {
  ORG, SOURCE, api, breakSource, connectStatus, createSource, dropSource, ensureOrg, psql, purgeOrg, repairSource, requireStack, topicRecords, waitFor,
} from "./stack";

test.describe.configure({ mode: "serial" });

// Kafka Connect keeps a source connector's read position under its NAME, and keeps it after the connector is deleted. A rerun that reused the
// name would resume past rows that no longer exist and see an empty table. So every run gets its own version, hence its own connector and topics.
const RUN = String(Math.floor(Date.now() / 1000) % 1_000_000);
const NAMES = {
  connection: "live-source",
  policy: "live-orders-amount",
  pipeline: "live-orders-ingest",
  domain: "live", system: "postgres", entity: SOURCE.table, version: `v${RUN}`,     // the entity IS the source table: the readiness check probes it
};
// The names the platform derives from the pipeline (see app/services/pipeline naming): checked against the real system below.
const CONNECTOR = `${ORG}--${NAMES.domain}--${NAMES.system}--${NAMES.entity}--batch--${NAMES.version}`;
const TOPIC = `${ORG}-raw-${NAMES.domain}-${NAMES.system}-${NAMES.entity}-batch-${NAMES.version}`;

let page: Page;

test.beforeAll(async ({ browser }) => {
  await requireStack();
  ensureOrg();
  await purgeOrg();
  createSource();
  const context = await browser.newContext();
  // Standalone dev build: the backend and tenant come from these two keys (the same ones Settings writes). The role is Admin.
  await context.addInitScript((org) => {
    localStorage.setItem("dc_org_id", org);
    localStorage.setItem("dc_role", "DataConnect Admin");
  }, ORG);
  page = await context.newPage();
});

test.afterAll(async () => {
  await page?.context().close();
  if (process.env.DATACONNECT_LIVE_KEEP === "1") return;          // keep everything as proof of the run
  await purgeOrg();
  dropSource();
});

/** Choose the option whose visible text contains `text` (option labels carry decorations such as "(postgresql)"). */
async function pick(selector: string, text: string) {
  await page.locator(selector).click();
  await page.locator(".ss-menu [role=option]", { hasText: text }).first().click();
}
const nav = (label: string) => page.locator(".sn", { hasText: label }).first().click();

// ── 1. Connection ──────────────────────────────────────────────────────────────────────────────────────────────────────
test("1. create a connection to the real source and test it", async () => {
  await page.goto("/");
  await nav("Connections");
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgresql$/ }) }).click();
  await page.getByRole("button", { name: "New postgresql connection" }).click();

  const drawer = page.locator(".modal");
  await page.locator("#conn-name").fill(NAMES.connection);
  await page.locator("#conn-field-hostname").fill(SOURCE.host);
  await page.locator("#conn-field-port").fill(String(SOURCE.port));
  await page.locator("#conn-field-database").fill(SOURCE.db);
  await page.locator("#conn-field-username").fill(SOURCE.user);
  await page.locator("#conn-field-password").fill(SOURCE.password);
  await page.locator("#conn-field-tables").fill(SOURCE.table);

  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(drawer.locator(".badge", { hasText: /^ok$/i })).toBeVisible();   // the real backend really connected to the real database

  await page.getByRole("button", { name: "Create connection" }).click();
  await expect(page.locator(".tbody .tr", { hasText: NAMES.connection })).toContainText("active");

  const stored = await api<{ name: string; status: string }[]>("GET", "/connections");
  expect(stored.body.find((c) => c.name === NAMES.connection)?.status).toBe("active");
});

// ── 2. Discovery ───────────────────────────────────────────────────────────────────────────────────────────────────────
test("2. discover the source's tables from the UI", async () => {
  await page.locator(`.ai[aria-label="Discover ${NAMES.connection}"]`).click();

  const modal = page.locator(".modal");
  await expect(modal).toContainText("Discovered objects");
  await expect(modal.locator(".tbody")).toContainText(SOURCE.table);              // found by the real backend, in the real database
  await modal.locator(".ff").getByRole("button", { name: "Close" }).click();
  await expect(modal).toHaveCount(0);
});

// ── 3. Quality policy ──────────────────────────────────────────────────────────────────────────────────────────────────
test("3. create a quality policy that routes failing records to quarantine", async () => {
  await nav("Quality");
  await page.getByRole("button", { name: "+ New Quality Check" }).click();

  await page.locator("#quality-name").fill(NAMES.policy);
  await choose(page.locator("#quality-on-failure"), "route_to_quarantine");
  await choose(page.locator("#quality-quarantine-sink"), "kafka");
  await page.locator('[id^="quality-rule-field-"]').first().fill("amount");
  await choose(page.locator('[id^="quality-rule-check-"]').first(), "min_max_numeric");
  await page.locator('[id^="quality-rule-param-"][id$="-min"]').first().fill("1");
  await page.locator('[id^="quality-rule-param-"][id$="-max"]').first().fill("1000");
  await page.getByRole("button", { name: "Save Quality Check" }).click();

  await expect(page.locator(".tbody .tr", { hasText: NAMES.policy })).toContainText("route_to_quarantine");
  const stored = await api<{ name: string; rules: { field: string; min: number; max: number }[] }[]>("GET", "/quality-policies");
  const policy = stored.body.find((p) => p.name === NAMES.policy);
  expect(policy?.rules[0]).toMatchObject({ field: "amount", min: 1, max: 1000 });
});

// ── 4. Pipeline ────────────────────────────────────────────────────────────────────────────────────────────────────────
test("4. create the pipeline from the JDBC Source template, with enforcement on", async () => {
  await nav("Pipelines");
  await page.getByRole("button", { name: "+ New pipeline" }).click();

  await choose(page.locator("#pipe-template"), "tpl_jdbc_source");
  await expect(page.locator("#pipe-template-availability")).toHaveCount(0);       // the catalog says this connector can run here (FE availability)
  await pick("#pipe-connection", NAMES.connection);
  await page.locator("#pipe-name").fill(NAMES.pipeline);
  await choose(page.locator("#pipe-zone"), "raw");
  await choose(page.locator("#pipe-mode"), "batch");
  await page.locator("#pipe-ver").fill(NAMES.version);
  await page.locator("#pipe-domain").fill(NAMES.domain);
  await page.locator("#pipe-source").fill(NAMES.system);
  await page.locator("#pipe-entity").fill(NAMES.entity);
  await page.locator("#pipe-tables").fill(SOURCE.table);
  await pick("#pipe-quality", NAMES.policy);
  await choose(page.locator("#pipe-quality-mode"), "ENFORCE");
  await page.screenshot({ path: "test-results/live-pipeline-form.png" });
  await page.locator(".modal .ff").getByRole("button", { name: "Create", exact: true }).click();

  await expect(page.locator(".tbody .tr", { hasText: NAMES.pipeline })).toContainText(/draft/i);
});

// ── 5. Deploy ──────────────────────────────────────────────────────────────────────────────────────────────────────────
const row = () => page.locator(".tbody .tr", { hasText: NAMES.pipeline });

test("5. review readiness, deploy from the UI, and the connector really runs and fills the topic", async () => {
  await row().locator(`.ai[aria-label="Deploy ${NAMES.pipeline}"]`).click();

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText(/ready/i);
  await page.screenshot({ path: "test-results/live-deploy-readiness.png" });
  await dialog.locator(".ff").getByRole("button", { name: "Deploy", exact: true }).click();

  await expect(row()).toContainText(/running/i, { timeout: 120_000 });

  // The real Kafka Connect, not the UI's opinion of it.
  const status = await waitFor("the connector to be RUNNING in Kafka Connect", async () => {
    const s = await connectStatus(CONNECTOR);
    return s && s.connector.state === "RUNNING" && s.tasks.length > 0 && s.tasks.every((t) => t.state === "RUNNING") ? s : false;
  });
  expect(status.tasks.length).toBeGreaterThan(0);

  // The real rows of the real source arrived on the real topic.
  await waitFor("all six source rows on the topic", () => topicRecords(TOPIC, { max: 6, timeoutMs: 5000 }).length >= 6, { timeoutMs: 120_000 });
  const records = topicRecords(TOPIC, { max: 50 });
  expect(records.length).toBe(6);
  const customers = records.map((r) => (r.payload as { customer: string }).customer).sort();
  expect(customers).toEqual(["alice", "bob", "carol", "dave", "erin", "frank"]);
});

// ── 6. Quarantine ──────────────────────────────────────────────────────────────────────────────────────────────────────
const QUARANTINE_TOPIC = `${ORG}-quarantine-${NAMES.domain}-${NAMES.system}-${NAMES.entity}-validation-${NAMES.version}`;
const PREPARED_TOPIC = `${ORG}-prepared-${NAMES.domain}-${NAMES.system}-${NAMES.entity}-batch-${NAMES.version}`;

test("6. records that break the policy are quarantined: the UI shows them and Kafka holds them apart from the good ones", async () => {
  await nav("Quality");

  // The running pipeline's enforcement worker evaluated the six real rows: two are outside 1..1000 (dave 5000.00, frank -3.00).
  const result = page.locator(".hrow", { hasText: NAMES.pipeline });
  await expect(result).toContainText("2/6 failed", { timeout: 90_000 });
  await result.click();

  const failures = page.locator(".modal");
  await expect(failures).toContainText(`Failed records — ${NAMES.pipeline}`);
  await expect(failures.getByText("1 rule broken")).toHaveCount(2);                 // two records, each broke the one rule
  await expect(failures).toContainText("offset 3");                                 // dave   (5000.00): the 4th row the connector read
  await expect(failures).toContainText("offset 5");                                 // frank  (-3.00):   the 6th
  await failures.locator(".ff").getByRole("button", { name: "Close" }).click();

  // Kafka: the two bad rows are on the quarantine topic, the four good ones on the prepared topic, none crossed over.
  await waitFor("the quarantine and prepared topics to be filled", () => topicRecords(QUARANTINE_TOPIC, { max: 10, timeoutMs: 4000 }).length >= 2
    && topicRecords(PREPARED_TOPIC, { max: 10, timeoutMs: 4000 }).length >= 4, { timeoutMs: 90_000 });
  const quarantined = topicRecords(QUARANTINE_TOPIC, { max: 50 });
  const prepared = topicRecords(PREPARED_TOPIC, { max: 50 });
  expect(quarantined).toHaveLength(2);
  expect(prepared).toHaveLength(4);
  const amountsOf = (rs: Record<string, unknown>[]) => rs.map((r) => {
    const original = (r.original_record ?? r.record ?? r) as { payload?: { amount?: unknown }; amount?: unknown };
    return Number(original.payload?.amount ?? original.amount);
  }).sort((a, b) => a - b);
  expect(amountsOf(quarantined)).toEqual([-3, 5000]);
  expect(amountsOf(prepared).every((n) => n >= 1 && n <= 1000)).toBe(true);
});

// ── 7. Failure and recovery ────────────────────────────────────────────────────────────────────────────────────────────
test("7. the source really fails, the UI shows the failure, and Restart brings the pipeline back and keeps enforcing", async () => {
  // Take the database away from the connector (login disabled, sessions killed) and add a row that can only arrive after recovery.
  breakSource();
  psql(`INSERT INTO ${SOURCE.table} (customer, amount) VALUES ('grace', 10.00)`);

  const failed = await waitFor("the connector task to FAIL in Kafka Connect", async () => {
    const s = await connectStatus(CONNECTOR);
    return s && (s.connector.state === "FAILED" || s.tasks.some((t) => t.state === "FAILED")) ? s : false;
  }, { timeoutMs: 200_000, intervalMs: 3000 });
  expect(JSON.stringify(failed)).toMatch(/FAILED/);

  // The UI reports it. While the source is down the platform keeps restarting the task (it fails again within seconds), so the failure is visible
  // in windows rather than constantly: sample the page often (a reload fetches fresh runtime state) until it shows the failure.
  await nav("Pipelines");
  await waitFor("the Pipelines page to show the failure", async () => {
    const text = (await row().innerText()).toLowerCase();
    if (/failed|degraded/.test(text)) return true;
    await page.reload();
    await row().waitFor();
    return false;
  }, { timeoutMs: 240_000, intervalMs: 2000 });
  await page.screenshot({ path: "test-results/live-pipeline-failed.png" });

  // Fix the cause, then recover from the UI.
  repairSource();
  psql(`INSERT INTO ${SOURCE.table} (customer, amount) VALUES ('heidi', 2500.00)`);       // out of range: must be quarantined after recovery too
  await row().locator(`.ai[aria-label="Restart ${NAMES.pipeline}"]`).click();

  await waitFor("the connector to be RUNNING again after Restart", async () => {
    const s = await connectStatus(CONNECTOR);
    return s && s.connector.state === "RUNNING" && s.tasks.length > 0 && s.tasks.every((t) => t.state === "RUNNING");
  }, { timeoutMs: 120_000 });
  await expect(row()).toContainText(/running/i, { timeout: 90_000 });

  // The rows written while it was down arrived, in the same topic, once each.
  await waitFor("the two new rows on the topic", () => topicRecords(TOPIC, { max: 50, timeoutMs: 4000 }).length >= 8, { timeoutMs: 120_000 });
  const customers = topicRecords(TOPIC, { max: 50 }).map((r) => (r.payload as { customer: string }).customer).sort();
  expect(customers).toEqual(["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi"]);

  // Enforcement carried on: heidi (2500) is quarantined, grace (10) passed.
  await nav("Quality");
  await expect(page.locator(".hrow", { hasText: NAMES.pipeline })).toContainText("3/8 failed", { timeout: 90_000 });
  await waitFor("heidi on the quarantine topic", () => topicRecords(QUARANTINE_TOPIC, { max: 20, timeoutMs: 4000 }).length >= 3, { timeoutMs: 60_000 });
  const quarantinedCustomers = topicRecords(QUARANTINE_TOPIC, { max: 20 }).map((r) => (r.original_record as { customer: string }).customer).sort();
  expect(quarantinedCustomers).toEqual(["dave", "frank", "heidi"]);
});

// ── 8. Schema drift ────────────────────────────────────────────────────────────────────────────────────────────────────
const payloads = (records: Record<string, unknown>[]) => records.map((r) => r.payload);

test("8. a real column added at the source is detected as drift, held for approval, and approved from the UI", async () => {
  await nav("Schemas");

  // Infer a contract from what the connector really produced (the Infer drawer; it needs the tenant header, which it once lacked).
  const before = payloads(topicRecords(TOPIC, { max: 50 }));
  await page.getByRole("button", { name: "Infer", exact: true }).click();
  await page.locator("#schema-infer-records").fill(JSON.stringify(before));
  await page.locator(".modal .ff").getByRole("button", { name: "Infer", exact: true }).click();
  const inferred = JSON.parse(await page.locator(".modal .codeblk").first().innerText()) as { name: string }[];
  expect(inferred.map((f) => f.name).sort()).toEqual(["amount", "customer", "id"]);
  await page.locator(".modal .ff").getByRole("button", { name: "Close" }).click();

  // Creating the pipeline already registered the real table's contract (auto-discovered from the source) — it is the approved schema.
  const contract = page.locator(".tbody .tr", { hasText: `${NAMES.domain}.${NAMES.system}.${NAMES.entity}` });
  await expect(contract).toBeVisible();

  // The source changes for real: a new column, and a row that carries it. The connector picks it up.
  psql(`ALTER TABLE ${SOURCE.table} ADD COLUMN loyalty_tier TEXT NOT NULL DEFAULT 'gold'`);
  psql(`INSERT INTO ${SOURCE.table} (customer, amount, loyalty_tier) VALUES ('ivan', 60.50, 'silver')`);
  const observed = await waitFor("the drifted row on the topic", () => {
    const found = payloads(topicRecords(TOPIC, { max: 50, timeoutMs: 4000 })).filter((p) => p && "loyalty_tier" in (p as object));
    return found.length ? found : false;
  }, { timeoutMs: 120_000 });
  expect(observed).toEqual([expect.objectContaining({ customer: "ivan", loyalty_tier: "silver" })]);

  // Detect the drift from the UI with those real records.
  await contract.locator('.ai[aria-label="View"]').click();
  await page.locator(".modal textarea").fill(JSON.stringify(observed));
  await page.getByRole("button", { name: "Run drift detection" }).click();
  await expect(page.locator(".modal")).toContainText("approval_required: true");
  await expect(page.locator(".modal")).toContainText("loyalty_tier");
  await page.locator(".modal .ff").getByRole("button", { name: "Close" }).click();

  // The pending change is shown, reviewed field by field, and approved.
  await expect(page.locator(".alert-w", { hasText: "Schema change detected" })).toContainText(/\d+ fields? changed/);
  await page.getByRole("button", { name: "Review changes" }).click();
  await expect(page.locator(".modal")).toContainText("loyalty_tier");
  await page.locator("#drift-comment").fill("live e2e: new loyalty tier column is expected");
  await page.locator(".modal .ff").getByRole("button", { name: /Approve/ }).click();

  await expect(page.locator(".alert-w", { hasText: "Schema change detected" })).toHaveCount(0);
  const approvals = await api<{ status: string; drift_events: { field: string; drift_type: string; action: string }[] }[]>("GET", "/schemas/drift-approvals");
  const mine = approvals.body.find((a) => a.drift_events.some((e) => e.field === "loyalty_tier"));
  expect(mine?.status).toBe("approved");
  expect(mine?.drift_events.find((e) => e.field === "loyalty_tier")).toMatchObject({ drift_type: "new_required_field", action: "hold_for_approval" });
});
