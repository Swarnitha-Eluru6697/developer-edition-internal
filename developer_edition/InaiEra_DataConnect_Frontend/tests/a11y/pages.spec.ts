/*
 * FE-BUG-21: axe-core scans of every main page, plus the busiest dialogs
 * (a create form, a detail/read-only drawer, a destructive confirmation).
 * Reuses tests/e2e/fixtures.ts's mocked backend so pages render populated,
 * realistic content instead of empty-state placeholders — an empty page
 * has almost nothing for axe to check (no table rows, no badges, no
 * status text), so it would make this gate far weaker than it looks.
 */
import { test } from "@playwright/test";
import { freshState, mockBackend, rowAction } from "../e2e/fixtures";
import { expectNoViolations } from "./helpers";

function seedRichState() {
  const state = freshState();
  state.connections.push({
    id: "conn-a11y", org_id: "org_test", name: "erp-prod-db", source_type: "postgres",
    connection_config: { hostname: "erp-db.internal", port: 5432, database: "erp", username: "svc", password: "********" },
    status: "active", verification_status: "ok",
  });
  state.pipelines.push({
    id: "pipe-a11y", org_id: "org_test", name: "ERP Customer CDC", status: "running",
    template_id: "tpl-postgres-cdc", connection_profile_id: "conn-a11y",
    topic_name: "org_test-raw-crm-erp-customers-cdc-v1", connector_name: "erp-customer-cdc",
    zone: "raw", mode: "cdc", generated_config: {},
    desired_state: "running", observed_state: "running", last_observed_at: new Date().toISOString(),
  });
  state.qualityPolicies.push({ id: "qp-a11y", org_id: "org_test", name: "PII validation", on_failure: "route_to_quarantine", rules: [{ name: "email_format", type: "format", field: "email" }] });
  state.transforms.push({ id: "tf-a11y", org_id: "org_test", name: "Mask PII", description: "mask email/phone", transforms: [{ order: 1, name: "maskPII", type: "MaskField", scope: "value", config: { fields: "email" } }] });
  state.schemas.push({ id: "sv-a11y", org_id: "org_test", domain: "crm", source_system: "erp", entity: "customers", version: "v1", status: "active", compatibility: "BACKWARD", schema_format: "json_schema", schema_definition: { fields: [{ name: "id", type: "string", nullable: false, primary_key: true }] } });
  state.driftApprovals.push({
    id: "drift-a11y", org_id: "org_test", schema_version_id: "sv-a11y", pipeline_id: "pipe-a11y", status: "pending",
    observed_schema: {},
    drift_events: [{
      field: "credit_limit", drift_type: "type_changed", action: "quarantine",
      details: { approved: { name: "credit_limit", type: "INT" }, observed: { name: "credit_limit", type: "DECIMAL" } },
    }],
  });
  state.audit.push({ id: "au-a11y", org_id: "org_test", actor: "lithish", action: "created", resource_type: "pipeline", resource_id: "pipe-a11y", created_at: new Date().toISOString() });
  state.lineageAssets.push(
    { id: "a-source", org_id: "org_test", asset_type: "source", name: "ERP System", qualified_name: "org_test:source:erp" },
    { id: "a-topic", org_id: "org_test", asset_type: "kafka_topic", name: "raw.customers", qualified_name: "org_test:topic:raw" },
  );
  state.lineageEdges.push({ id: "e-a11y", org_id: "org_test", from_asset_id: "a-source", to_asset_id: "a-topic", edge_type: "CONNECTS_TO", pipeline_id: "pipe-a11y" });
  return state;
}

const PAGES = ["Dashboard", "Connections", "Pipelines", "Quality", "Transforms", "Schemas", "Lineage", "Monitoring", "Alerts", "Templates", "Settings", "Audit / History"];

for (const label of PAGES) {
  test(`${label} has no WCAG A/AA violations`, async ({ page }) => {
    await mockBackend(page, seedRichState());
    await page.goto("/");
    if (label !== "Dashboard") await page.locator(".sn", { hasText: label }).click();
    await expectNoViolations(page);
  });
}

test("the New pipeline create form has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expectNoViolations(page);
});

test("the connection create form, including a validation error, has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('button[title^="New"]').click();
  // Trigger the per-field error state (FE-BUG-21) so the scan also covers
  // aria-invalid/aria-describedby + the visible error text, not just the
  // clean form.
  await page.getByRole("button", { name: "Create connection" }).click();
  await expectNoViolations(page);
});

test("the pipeline detail drawer (read-only configuration + runtime state) has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "ERP Customer CDC" }), "View pipeline detail");
  await expectNoViolations(page);
});

test("the 'Ready to deploy' review screen (FE-BUG-24) has no WCAG A/AA violations", async ({ page }) => {
  const state = seedRichState();
  state.pipelines.forEach((p) => { p.status = "draft"; });   // only a draft row offers Deploy
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "ERP Customer CDC" }), "Deploy");
  await page.locator(".modal").getByText("Ready to deploy").waitFor();
  await expectNoViolations(page);
});

test("the schema drift review dialog has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Schemas" }).click();
  await page.getByRole("button", { name: "Review changes" }).click();
  await expectNoViolations(page);
});

test("the quality Execute drawer has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.locator(".tr", { hasText: "PII validation" }).locator('[title="Execute"]').click();
  await page.getByRole("dialog").waitFor();
  await expectNoViolations(page);
});

test("the new quality policy form has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();
  await page.getByRole("dialog").waitFor();
  await expectNoViolations(page);
});

test("the transform view and preview drawers have no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await rowAction(page.locator(".tr", { hasText: "Mask PII" }), "View Mask PII");
  await page.getByRole("dialog").waitFor();
  await expectNoViolations(page);
  await page.keyboard.press("Escape");
  await rowAction(page.locator(".tr", { hasText: "Mask PII" }), "Preview Mask PII");
  await page.getByRole("dialog").waitFor();
  await expectNoViolations(page);
});

test("the connection detail drawer has no WCAG A/AA violations", async ({ page }) => {
  await mockBackend(page, seedRichState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator('[aria-label^="View erp-prod-db"]').click();
  await page.getByRole("dialog").waitFor();
  await expectNoViolations(page);
});

test("the 'no access' screen has no WCAG A/AA violations", async ({ page }) => {
  const state = seedRichState();
  state.me = { user: "Lithish", userId: "u-1", tenant: "org_1001", permissions: [] };
  await mockBackend(page, state);
  await page.goto("/");
  await page.getByTestId("dc-access-denied").waitFor();
  await expectNoViolations(page);
});
