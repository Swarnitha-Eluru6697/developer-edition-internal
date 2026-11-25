/*
 * E2E happy path — a single walkthrough touching every major area of the
 * app in one pass: role switching, connections, pipeline creation +
 * deployment, quality policies, schema drift review, and the read-only
 * areas (lineage, monitoring, audit), finishing back where a session
 * would naturally end up.
 */
import { test, expect } from "@playwright/test";
import { clickDeploy, freshState, mockBackend, switchRole, expectActiveRole } from "./fixtures";

test("admin walks the whole app end to end", async ({ page }) => {
  const state = freshState();
  state.driftApprovals.push({
    id: "drift-happy", org_id: "org_test", schema_version_id: "sv-happy",
    observed_schema: {},
    drift_events: [{
      field: "email", drift_type: "type_changed", action: "quarantine",
      details: { approved: { name: "email", type: "STRING" }, observed: { name: "email", type: "OBJECT" } },
    }],
    status: "pending",
  });
  await mockBackend(page, state);

  // 1. Land on the dashboard, backend reachable, default role is Admin.
  await page.goto("/");
  await expect(page.locator(".tb-conn")).toHaveCount(0);   // the top bar no longer carries a backend status badge
  await expectActiveRole(page, "Admin");

  // 2. Create a connection.
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
  await page.locator(".iconadd").click();
  await page.locator('input[placeholder^="my-postgres"]').fill("happy-path-db");
  await page.locator('input[placeholder="db.internal"]').fill("db.internal.test");
  await page.locator(".fld", { hasText: "Database" }).locator("input").fill("salesdb");
  await page.locator(".fld", { hasText: "Username" }).locator("input").fill("dataconnect");
  await page.locator(".fld", { hasText: "Password" }).locator("input").fill("hunter2");
  await page.getByRole("button", { name: "Create connection" }).click();
  await expect(page.locator(".tbody")).toContainText("happy-path-db");

  // 3. Create and deploy a pipeline against that connection.
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await page.locator(".fld", { hasText: "Name" }).locator("input").fill("happy-path-pipeline");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  const pipeRow = page.locator(".tr", { hasText: "happy-path-pipeline" });
  await expect(pipeRow).toBeVisible();
  await clickDeploy(page, pipeRow);
  await expect(pipeRow).toContainText("running");

  // 4. Create a quality policy.
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();
  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("happy-path-quality");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "Save Quality Check" }).click();
  await expect(page.locator(".tbody")).toContainText("happy-path-quality");

  // 5. Review and approve the pending schema drift.
  await page.locator(".sn", { hasText: "Schemas" }).click();
  await expect(page.locator(".alert-w")).toContainText("Schema change detected");
  await page.getByRole("button", { name: "Review changes" }).click();
  await expect(page.locator(".modal")).toContainText("email");
  await page.locator(".modal").getByRole("button", { name: "Approve new schema version" }).click();
  await expect(page.locator(".alert-w")).toHaveCount(0);

  // 6. The read-only areas load without error.
  for (const label of ["Lineage", "Monitoring", "Audit / History"]) {
    await page.locator(".sn", { hasText: label }).click();
    await expect(page.locator(".alert-w")).toHaveCount(0);
  }

  // 7. Switch to Viewer and confirm write actions are gone, but everything
  //    just built is still visible read-only. Templates and Settings are
  //    both readable by every role now (view_connector_templates /
  //    view_settings — see BUG-06); only writing to either stays Admin-only.
  await switchRole(page, "Viewer");
  await expect(page.locator("aside.sb")).toContainText("Templates");
  await expect(page.locator("aside.sb")).toContainText("Settings");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(page.locator(".tbody")).toContainText("happy-path-pipeline");
  await expect(page.getByRole("button", { name: "+ New pipeline" })).toHaveCount(0);
});
