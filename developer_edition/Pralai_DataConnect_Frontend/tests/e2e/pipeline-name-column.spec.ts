/*
 * Schemas and Transforms each have a "Pipeline Name" column beside their first one: the pipelines that use that contract / policy
 * (a pipeline records them in schema_version_id / transform_policy_id). Several pipelines: the first two and "+N"; none: a dash.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const pipe = (id: string, name: string, extra: Record<string, unknown>) => ({
  id, org_id: "o", name, status: "running", topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "batch", generated_config: {}, ...extra,
});
const schema = (id: string, entity: string) => ({
  id, org_id: "o", domain: "crm", source_system: "sf", entity, version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active",
  schema_definition: { fields: [] },
});

async function seeded(page: Page, nav: string) {
  const state = freshState();
  state.schemas.push(schema("s1", "orders"), schema("s2", "accounts"), schema("s3", "unused"));
  state.transforms.push(
    { id: "tf1", org_id: "o", name: "mask-pii", description: "d", transforms: [] },
    { id: "tf2", org_id: "o", name: "shared-policy", description: "d", transforms: [] },
    { id: "tf3", org_id: "o", name: "idle-policy", description: "d", transforms: [] },
  );
  state.pipelines.push(
    pipe("p1", "orders-cdc", { schema_version_id: "s1", transform_policy_id: "tf1" }),
    pipe("p2", "accounts-sync", { schema_version_id: "s2", transform_policy_id: "tf2" }),
    pipe("p3", "accounts-sink", { schema_version_id: "s2", transform_policy_id: "tf2" }),
    pipe("p4", "accounts-audit", { schema_version_id: "s2", transform_policy_id: "tf2" }),
    pipe("p5", "no-links", {}),
  );
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: nav }).click();
}

const cell = (page: Page, rowText: string, col: number) => page.locator(".tbody .tr", { hasText: rowText }).locator("[role=cell]").nth(col);

test("Schemas: a Pipeline Name column (after Created at) lists the pipelines using each contract", async ({ page }) => {
  await seeded(page, "Schemas");
  await expect(page.getByRole("columnheader")).toHaveText(["Contract", "Created at", "Pipeline Name", "Status", "Version", "Compatibility", "Format", "Actions"]);
  await expect(cell(page, "crm.sf.orders", 2)).toHaveText("orders-cdc");
  await expect(cell(page, "crm.sf.accounts", 2)).toContainText("accounts-sync, accounts-sink");   // two shown …
  await expect(cell(page, "crm.sf.accounts", 2)).toContainText("+1");                              // … and the rest counted
  await expect(cell(page, "crm.sf.accounts", 2).locator("[title]")).toHaveAttribute("title", "accounts-sync, accounts-sink, accounts-audit");
  await expect(cell(page, "crm.sf.unused", 2)).toHaveText("—");                                    // a contract no pipeline uses
});

test("Transforms: a Pipeline Name column lists the pipelines using each SMT chain", async ({ page }) => {
  await seeded(page, "Transforms");
  await expect(page.getByRole("columnheader")).toHaveText(["Chain Name", "Created at", "Type", "Pipeline Name", "SMTs", "Description", "Actions"]);
  await expect(cell(page, "mask-pii", 3)).toHaveText("orders-cdc");
  await expect(cell(page, "shared-policy", 3)).toContainText("accounts-sync, accounts-sink");
  await expect(cell(page, "shared-policy", 3)).toContainText("+1");
  await expect(cell(page, "idle-policy", 3)).toHaveText("—");
});

test("the header and the rows stay in step: the Pipeline Name heading sits over its cells", async ({ page }) => {
  await seeded(page, "Schemas");
  const offset = async (name: string, col: number) => (await page.getByRole("columnheader", { name }).boundingBox())!.x - (await cell(page, "crm.sf.orders", col).boundingBox())!.x;
  const contract = await offset("Contract", 0);                                // how the table already lines up (its header text is inset a little from the cell)
  expect(Math.abs((await offset("Pipeline Name", 2)) - contract)).toBeLessThan(2);   // the new column lines up the same way
});

test("Transforms' button says \"+ New SMT chain\"; the form it opens is titled \"Custom SMT chain\"", async ({ page }) => {
  await seeded(page, "Transforms");
  await expect(page.getByRole("button", { name: "+ New Quality Check" })).toHaveCount(0);
  await page.getByRole("button", { name: "+ New SMT chain" }).click();
  await expect(page.locator(".drawer, .modal").last().getByText("Custom SMT chain", { exact: true })).toBeVisible();
  await expect(page.locator(".drawer, .modal").last().getByText("New SMT chain", { exact: true })).toHaveCount(0);
  await expect(page.getByText("New transform policy")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save SMT chain" })).toBeVisible();                  // the form's save button too
  await expect(page.getByRole("button", { name: "Save policy" })).toHaveCount(0);
});

test("Transforms: a Type column says Custom for an SMT chain made with + New SMT chain and Standard for one a pipeline made", async ({ page }) => {
  const state = freshState();
  state.transforms.push(
    { id: "t1", org_id: "o", name: "by-hand", description: "mine", transforms: [], origin: "custom" },
    { id: "t2", org_id: "o", name: "by-pipeline", description: "x", transforms: [], origin: "auto" },
    { id: "t3", org_id: "o", name: "older-server-auto", description: "Auto-created from selected source transformations", transforms: [] },   // no origin field: read from the description
    { id: "t4", org_id: "o", name: "older-server-custom", description: "mine too", transforms: [] },
  );
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await expect(cell(page, "by-hand", 2)).toHaveText("Custom");
  await expect(cell(page, "by-pipeline", 2)).toHaveText("Standard");
  await expect(cell(page, "older-server-auto", 2)).toHaveText("Standard");
  await expect(cell(page, "older-server-custom", 2)).toHaveText("Custom");
});

test("a chain made with + New SMT chain is sent as custom and shows Custom", async ({ page }) => {
  const state = freshState();
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await page.getByRole("button", { name: "+ New SMT chain" }).click();
  await page.getByLabel("Name", { exact: true }).fill("hand-made");
  await page.getByRole("button", { name: "Save SMT chain" }).click();
  await expect(page.locator(".toast").last()).toContainText("created");
  expect((state.transforms[0] as { origin?: string }).origin ?? "custom").toBe("custom");
  await expect(cell(page, "hand-made", 2)).toHaveText("Custom");
});

test("Audit: a transform policy's Resource reads Custom SMT chain or Standard SMT chain, not 'transform policy'", async ({ page }) => {
  const state = freshState();
  const at = "2026-10-20T10:00:00Z";
  state.audit.push(
    { id: "a1", action: "created", resource_type: "transform_policy", resource_id: "t1", actor: "me", created_at: at, before_state: null, after_state: { origin: "custom", name: "x" } },
    { id: "a2", action: "created", resource_type: "transform_policy", resource_id: "t2", actor: "me", created_at: at, before_state: null, after_state: { origin: "auto", name: "y" } },
    { id: "a3", action: "deleted", resource_type: "transform_policy", resource_id: "t3", actor: "me", created_at: at, before_state: { origin: "auto" }, after_state: null },
    { id: "a4", action: "created", resource_type: "transform_policy", resource_id: "t4", actor: "me", created_at: at, before_state: null, after_state: { description: "Auto-created from selected source transformations" } },
    { id: "a5", action: "created", resource_type: "transform_policy", resource_id: "t5", actor: "me", created_at: at, before_state: null, after_state: { name: "old entry, no origin" } },
    { id: "a6", action: "created", resource_type: "quality_policy", resource_id: "q1", actor: "me", created_at: at, before_state: null, after_state: {} },
  );
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Audit" }).click();
  const resource = (id: string) => page.locator(".tbody .tr", { hasText: id }).locator("[role=cell]").nth(3);
  await expect(resource("t1")).toHaveText("Custom SMT chain");
  await expect(resource("t2")).toHaveText("Standard SMT chain");
  await expect(resource("t3")).toHaveText("Standard SMT chain");                  // a delete keeps what it was
  await expect(resource("t4")).toHaveText("Standard SMT chain");                  // an older entry: told apart by the description
  await expect(resource("t5")).toHaveText("Custom SMT chain");
  await expect(resource("q1")).toHaveText("Quality Check");            // other resources read as before
  await expect(page.getByText("transform policy")).toHaveCount(0);
});
