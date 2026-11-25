/*
 * Quality rule builder tests — the dynamic add/remove rule rows in
 * src/pages/Quality.tsx's QualityForm (one row per field+check, built into
 * the `rules` array the backend actually receives).
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend, switchRole, choose } from "./fixtures";

test("builds a two-rule policy from the rule builder", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();

  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("Customer PII validation");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");

  await page.getByRole("button", { name: "+ Add rule" }).click();
  await page.locator('input[placeholder="e.g. email"]').nth(1).fill("age");
  await choose(page.locator(".fld", { hasText: "Check" }).nth(1).locator(".ss-trigger"), "min_max_numeric");
  await page.locator('input[placeholder="0"]').fill("0");
  await page.locator('input[placeholder="100000"]').fill("120");

  await page.getByRole("button", { name: "Save Quality Check" }).click();

  await expect(page.locator(".toast").last()).toContainText("Quality check created (2 rules)");
  const row = page.locator(".tr", { hasText: "Customer PII validation" });
  await expect(row).toContainText("2 rules");
});

test("removing a rule row drops it from the submitted policy", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();

  await page.locator('input[placeholder="e.g. Customer PII validation"]').fill("Single rule policy");
  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "+ Add rule" }).click();
  await page.locator('input[placeholder="e.g. email"]').nth(1).fill("to-be-removed");

  await page.locator('[title="Remove rule"]').nth(1).click();
  await expect(page.locator('input[placeholder="e.g. email"]')).toHaveCount(1);

  await page.getByRole("button", { name: "Save Quality Check" }).click();
  await expect(page.locator(".toast").last()).toContainText("Quality check created (1 rules)");
});

test("rejects submission with no policy name", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("button", { name: "+ New Quality Check" }).click();

  await page.locator('input[placeholder="e.g. email"]').first().fill("email");
  await page.getByRole("button", { name: "Save Quality Check" }).click();

  await expect(page.locator(".toast").last()).toContainText(/name/i);
});

test("Viewer cannot open the quality policy builder", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Quality" }).click();

  await expect(page.getByRole("button", { name: "+ New Quality Check" })).toHaveCount(0);
});

test("Quality separates validation failures (by rule) from ingestion failures (by cause) (FE-BUG-17)", async ({ page }) => {
  await mockBackend(page);
  await page.route("**/runtime/metrics/failures*", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      window_seconds: 3600,
      validation: {
        total: 3, by_rule: [{ rule: "not_null:customer_id", count: 2 }, { rule: "regex:email", count: 1 }],
        by_check: [{ quality_policy_id: "qp-pii", name: "customer-pii-check", count: 3, pipelines: ["orders-a", "orders-b", "orders-c"] }],
      },
      ingestion: {
        total: 5, classified: 2, unclassified: 3,
        by_category: [
          { category: "serialization", label: "Serialization", count: 1 },
          { category: "connector", label: "Connector errors", count: 1 },
        ],
        recent: [{
          pipeline_name: "orders-sink", category: "connector", stage: "TASK_PUT",
          exception_class: "java.sql.SQLException", message: "connection reset", at: new Date().toISOString(),
        }],
      },
    }),
  }));

  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();

  const validation = page.getByRole("region", { name: "Validation failures" });
  await expect(validation).toContainText("customer-pii-check");                                   // the quality check, not its rules
  await expect(validation).toContainText("orders-a, orders-b +1");
  await expect(validation.locator(".hrow")).toHaveCount(1);
  await expect(validation).not.toContainText("not_null:customer_id");
  await expect(validation).not.toContainText("regex:email");
  await expect(validation).not.toContainText("Serialization");

  const ingestion = page.getByRole("region", { name: "Ingestion failures" });
  await expect(ingestion).toContainText("Serialization");
  await expect(ingestion).toContainText("Connector errors");
  await expect(ingestion).toContainText("3 of 5 records are still being classified");
  await expect(ingestion).not.toContainText("not_null:customer_id");

  await ingestion.getByText("Recent ingestion errors").click();
  await expect(ingestion).toContainText("java.sql.SQLException — connection reset");
});

// NEW-02: a sink may read several topics, so a failure has to say which topic (and where in it) it was read from.
test("the failed-records drawer shows the topic, partition and offset each failure came from, for every input topic (NEW-02)", async ({ page }) => {
  const state = freshState();
  state.qualityPolicies.push({ id: "qp-1", org_id: "org_test", name: "Stock range", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.pipelines.push({ id: "pipe-multi", org_id: "org_test", name: "orders-multi-sink", status: "running", direction: "SINK", mode: "batch", generated_config: {}, revision: 1 });
  await mockBackend(page, state);
  const failure = (topic: string, partition: number | undefined, offset: number | undefined) => ({
    original_topic: topic, pipeline_id: "pipe-multi", schema_id: null, quality_policy_id: "qp-1", failure_type: "quality_rule_failed",
    failed_rules: ["stock_range"], received_at: new Date(Date.now() - 600_000).toISOString(), original_record: { stock_quantity: 1 },
    ...(partition !== undefined ? { original_partition: partition } : {}), ...(offset !== undefined ? { original_offset: offset } : {}),
  });
  await page.route("**/quality-stream-stats", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify([{
      id: "s1", org_id: "org_test", pipeline_id: "pipe-multi", quality_policy_id: "qp-1", total_count: 12, failed_count: 3, passed_count: 9,
      updated_at: new Date(Date.now() - 600_000).toISOString(),
      sample_failures: [
        failure("org_test-raw-ext-feed-alpha-batch-v1", 0, 3),
        failure("org_test-raw-ext-feed-beta-batch-v1", 1, 3),
        failure("org_test-raw-ext-feed-gamma-batch-v1", 0, 0),     // partition 0 / offset 0 are real positions, not "missing"
        failure("org_test-raw-ext-feed-delta-batch-v1", undefined, undefined),   // a manual check has no Kafka position
      ],
    }]),
  }));

  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();
  await page.getByRole("tab", { name: "History" }).click();
  await page.locator(".hrow", { hasText: "orders-multi-sink" }).click();

  const drawer = page.locator(".drawer, .modal").last();
  for (const topic of ["alpha", "beta", "gamma", "delta"]) await expect(drawer).toContainText(`org_test-raw-ext-feed-${topic}-batch-v1`);
  await expect(drawer).toContainText("partition 0 · offset 3");
  await expect(drawer).toContainText("partition 1 · offset 3");
  await expect(drawer).toContainText("partition 0 · offset 0");
  await expect(drawer.locator("text=/partition/")).toHaveCount(3);          // the manual one shows none, rather than "undefined"
});
