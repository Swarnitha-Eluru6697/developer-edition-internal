/*
 * Schema drift approval tests — the pending-approval banner and
 * SchemaDriftDialog (FE-BUG-18) in src/pages/Schemas.tsx, gated to
 * Capability.APPROVE_SCHEMA_DRIFT (Admin/Integrator only).
 *
 * drift_events below mirrors app/services/schema_drift.py's
 * compare_schema_fields() output exactly — {field, drift_type, action,
 * details: {approved?, observed?}} — not the coarser {field, drift_type}
 * shape this fixture used before FE-BUG-18, so these tests actually
 * exercise the real per-field diff rendering.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend, switchRole } from "./fixtures";

function seedPendingApproval(state: ReturnType<typeof freshState>, overrides: Record<string, unknown> = {}) {
  state.driftApprovals.push({
    id: "drift-seed", org_id: "org_test", schema_version_id: "sv-seed", pipeline_id: "pipe-erp",
    observed_schema: {},
    drift_events: [
      {
        field: "loyalty_tier", drift_type: "new_optional_field", action: "allow_and_notify",
        details: { observed: { name: "loyalty_tier", type: "STRING", nullable: true } },
      },
      {
        field: "credit_limit", drift_type: "type_changed", action: "quarantine",
        details: {
          approved: { name: "credit_limit", type: "INT", nullable: true },
          observed: { name: "credit_limit", type: "DECIMAL", nullable: true },
        },
      },
      {
        field: "legacy_customer_id", drift_type: "field_removed", action: "alert",
        details: { approved: { name: "legacy_customer_id", type: "STRING", nullable: true } },
      },
    ],
    status: "pending",
    ...overrides,
  });
}

test("Integrator can review a pending schema drift's field-by-field diff and approve it", async ({ page }) => {
  const state = freshState();
  seedPendingApproval(state);
  state.pipelines.push({
    id: "pipe-erp", org_id: "org_test", name: "ERP Customers CDC", status: "running",
    topic_name: "raw-crm-postgres-products-api-v21", connector_name: "erp-customers-cdc",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Integrator");
  await page.locator(".sn", { hasText: "Schemas" }).click();

  await expect(page.locator(".alert-w")).toContainText("Schema change detected");
  await expect(page.locator(".alert-w")).toContainText("ERP Customers CDC");
  await expect(page.locator(".alert-w")).toContainText("3 fields changed");
  await expect(page.locator(".alert-w")).toContainText("⚠ Breaking change");

  await page.getByRole("button", { name: "Review changes" }).click();

  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Schema change detected");
  // + loyalty_tier — new, optional, never breaking.
  await expect(dialog).toContainText("loyalty_tier");
  await expect(dialog).toContainText("STRING nullable");
  // ~ credit_limit — the exact ticket example: INT → DECIMAL.
  await expect(dialog).toContainText("credit_limit");
  await expect(dialog).toContainText("INT → DECIMAL");
  // - legacy_customer_id — removed.
  await expect(dialog).toContainText("legacy_customer_id");
  await expect(dialog).toContainText("⚠ Breaking change");
  await expect(dialog).toContainText("Affected pipeline");
  await expect(dialog).toContainText("ERP Customers CDC");
  // Never a raw JSON dump of the drift_events array.
  await expect(dialog).not.toContainText("drift_type");

  await dialog.getByRole("button", { name: "Approve new schema version" }).click();

  await expect(page.locator(".toast").last()).toContainText("approved");
  await expect(page.locator(".alert-w")).toHaveCount(0);
});

test("a purely additive, optional-field drift shows as Compatible, not Breaking", async ({ page }) => {
  const state = freshState();
  seedPendingApproval(state, {
    drift_events: [
      {
        field: "loyalty_tier", drift_type: "new_optional_field", action: "allow_and_notify",
        details: { observed: { name: "loyalty_tier", type: "STRING", nullable: true } },
      },
    ],
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Integrator");
  await page.locator(".sn", { hasText: "Schemas" }).click();

  await expect(page.locator(".alert-w")).toContainText("✓ Compatible");
  await page.getByRole("button", { name: "Review changes" }).click();
  await expect(page.locator(".modal")).toContainText("✓ Compatible");
});

test("Integrator can reject a pending schema drift approval with a comment", async ({ page }) => {
  const state = freshState();
  seedPendingApproval(state);
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Integrator");
  await page.locator(".sn", { hasText: "Schemas" }).click();
  await page.getByRole("button", { name: "Review changes" }).click();

  const dialog = page.locator(".modal");
  await dialog.locator("textarea").fill("breaking change, not approved");
  await dialog.getByRole("button", { name: "Reject" }).click();

  await expect(page.locator(".toast").last()).toContainText("rejected");
});

test("Viewer sees the pending drift banner but cannot review or decide", async ({ page }) => {
  const state = freshState();
  seedPendingApproval(state);
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Schemas" }).click();

  await expect(page.locator(".alert-w")).toContainText("Schema change detected");
  await expect(page.getByRole("button", { name: "Review changes" })).toHaveCount(0);
});

// Seven pending changes used to make seven full-size banners that squeezed the schema table below to 0 px, so no schema
// could be scrolled to or opened. Now they share one banner (scrolling inside), each row names its schema, and the table
// keeps room and scrolls to the last schema.
test("many pending drift changes share one banner and the schema table still scrolls to the last schema", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = freshState();
  for (let i = 0; i < 60; i++) {
    state.schemas.push({
      id: `sv-${i}`, org_id: "org_test", domain: "crm", source_system: "pg", entity: `table_${i}`, version: "v1",
      schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] },
    });
  }
  for (let i = 0; i < 7; i++) seedPendingApproval(state, { id: `drift-${i}`, schema_version_id: `sv-${i}`, pipeline_id: null });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Schemas" }).click();

  await expect(page.locator(".alert-w")).toHaveCount(1);
  await expect(page.locator(".alert-w .at")).toHaveText("7 schema changes detected");
  await expect(page.locator(".drift-row")).toHaveCount(7);
  await expect(page.locator(".drift-row").first()).toContainText("crm.pg.table_0 v1 · 3 fields changed");
  const list = await page.locator(".drift-list").evaluate((el) => ({ h: el.clientHeight, sh: el.scrollHeight }));
  expect(list.h).toBeLessThan(170);
  expect(list.sh).toBeGreaterThan(list.h);

  const body = page.locator(".tbl.scroll .tbody");
  expect(await body.evaluate((el) => el.clientHeight)).toBeGreaterThan(150);
  await body.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await page.locator(".tbl.scroll .tr", { hasText: "crm.pg.table_59" }).getByRole("button", { name: "View" }).click();
  await expect(page.locator(".modal")).toBeVisible();
});
