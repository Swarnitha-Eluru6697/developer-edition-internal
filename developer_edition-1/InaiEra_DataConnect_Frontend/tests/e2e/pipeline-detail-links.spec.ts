/*
 * The Pipeline detail drawer names what the pipeline is wired to: its connector template, schema, SMT (with its Type) and, when one is attached,
 * its quality policy (with how it is applied).
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, rowAction } from "./fixtures";

const TEMPLATE = {
  id: "tpl-pg", name: "Postgres CDC Source", connector_type: "source", category: "database", connector_class: "x.y.Z", runtime_pool_type: "kafka-connect",
  config_schema: {}, default_config: {}, default_transforms: [], supported_transforms: [], version: "v1", is_active: true, supports_dlq: false,
};
const base = (id: string, name: string, extra: Record<string, unknown>) => ({
  id, org_id: "o", name, status: "draft", topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "cdc", zone: "raw", template_id: "tpl-pg", generated_config: {}, ...extra,
});

async function openDetail(page: Page, name: string) {
  const state = freshState();
  state.templates.splice(0, state.templates.length, TEMPLATE);
  state.schemas.push({ id: "s1", org_id: "o", domain: "crm", source_system: "salesforce", entity: "orders", version: "v2", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] } });
  state.transforms.push(
    { id: "tf-c", org_id: "o", name: "mask-pii", description: "mine", origin: "custom", transforms: [] },
    { id: "tf-a", org_id: "o", name: "wired-pipeline-transforms", description: "x", origin: "auto", transforms: [] },
  );
  state.qualityPolicies.push({ id: "q1", org_id: "o", name: "orders-rules", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  state.pipelines.push(
    base("p-full", "wired-pipeline", { schema_version_id: "s1", transform_policy_id: "tf-c", quality_policy_id: "q1", quality_mode: "ENFORCE" }),
    base("p-auto", "auto-smt-pipeline", { transform_policy_id: "tf-a", quality_policy_id: "q1" }),
    base("p-bare", "bare-pipeline", {}),
  );
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: name }), "View pipeline detail");
  await expect(page.locator(".drawer, .modal").last()).toContainText("Configuration");
}

const row = (page: Page, key: string) => page.locator(".kv", { has: page.getByText(key, { exact: true }) });

test("the detail names the connector template, schema, SMT with its Type, and the quality policy with how it is applied", async ({ page }) => {
  await openDetail(page, "wired-pipeline");
  await expect(row(page, "Connector template")).toContainText("Postgres CDC Source");
  await expect(row(page, "Schema")).toContainText("crm.salesforce.orders · v2");
  await expect(row(page, "SMT chain")).toContainText("mask-pii");
  await expect(row(page, "SMT chain")).toContainText("Custom");
  await expect(row(page, "Quality Check")).toContainText("orders-rules · Enforce");
});

test("an SMT a pipeline made for itself is shown as Standard; a quality policy with no mode set reads Observe", async ({ page }) => {
  await openDetail(page, "auto-smt-pipeline");
  await expect(row(page, "SMT chain")).toContainText("wired-pipeline-transforms");
  await expect(row(page, "SMT chain")).toContainText("Standard");
  await expect(row(page, "Quality Check")).toContainText("orders-rules · Observe");
});

test("with nothing attached: the schema and SMT rows show a dash and there is no quality policy row at all", async ({ page }) => {
  await openDetail(page, "bare-pipeline");
  await expect(row(page, "Connector template")).toContainText("Postgres CDC Source");
  await expect(row(page, "Schema")).toContainText("—");
  await expect(row(page, "SMT chain")).toContainText("—");
  await expect(page.getByText("Quality Check", { exact: true })).toHaveCount(0);
});
