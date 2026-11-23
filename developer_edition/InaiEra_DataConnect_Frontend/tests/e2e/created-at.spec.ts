/*
 * "Created at": Connections, Pipelines, Schemas, Quality policies and SMT chains show when each item was created, in the column right after the
 * first one; the Connection and Pipeline detail panels show it too (the Schema's does not, as asked). A missing time is a dash.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, rowAction } from "./fixtures";

const WHEN = "2026-10-22T07:53:00Z";
const when = new Date(WHEN).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });   // the viewer's own zone and format

async function seeded(page: Page, nav: string) {
  const state = freshState();
  state.connections.push({ id: "c1", org_id: "o", name: "shop-db", source_type: "postgresql", connection_config: { hostname: "db", database: "shop" }, status: "active", created_at: WHEN },
    { id: "c2", org_id: "o", name: "old-db", source_type: "postgresql", connection_config: { hostname: "db2", database: "old" }, status: "active" });
  state.pipelines.push({ id: "p1", org_id: "o", name: "orders-cdc", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {}, created_at: WHEN },
    { id: "p2", org_id: "o", name: "undated", status: "draft", topic_name: "t2", connector_name: "c2", mode: "batch", generated_config: {} });
  state.schemas.push({ id: "s1", org_id: "o", domain: "crm", source_system: "sf", entity: "orders", version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] }, created_at: WHEN });
  state.qualityPolicies.push({ id: "q1", org_id: "o", name: "orders-rules", rules: [], on_failure: "log", quarantine_sink: "kafka", created_at: WHEN });
  state.transforms.push({ id: "t1", org_id: "o", name: "mask-pii", description: "d", transforms: [], origin: "custom", created_at: WHEN });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: nav }).click();
}
const heads = async (page: Page) => {
  await expect(page.locator(".tbody .tr").first()).toBeVisible();                         // the table, not the loading state
  return page.getByRole("columnheader").allTextContents();
};
const cell = (page: Page, rowText: string, col: number) => page.locator(".tbody .tr", { hasText: rowText }).locator("[role=cell]").nth(col);

test("Connections: Created at follows Friendly Name, and the detail panel shows it", async ({ page }) => {
  await seeded(page, "Connections");
  await page.locator(".conn-grid >> text=postgresql").first().click();
  expect((await heads(page)).map((h) => h.replace(/[▲▼⇅\s]+$/, "").trim()).slice(0, 2)).toEqual(["Friendly Name", "Created at"]);
  await expect(cell(page, "shop-db", 1)).toHaveText(when);
  await expect(cell(page, "old-db", 1)).toHaveText("—");                                   // no time recorded: a dash, not "Invalid Date"
  await expect(cell(page, "shop-db", 1).locator("[title]")).toHaveAttribute("title", /2026/);   // the full time on hover
  await page.getByRole("button", { name: "View shop-db" }).click();
  await expect(page.locator(".kv", { has: page.getByText("Created at", { exact: true }) })).toContainText(when);
});

test("Pipelines: Created at follows Name, and the detail panel shows it", async ({ page }) => {
  await seeded(page, "Pipelines");
  expect((await heads(page)).slice(0, 3)).toEqual(["Name", "Created at", "Status"]);
  await expect(cell(page, "orders-cdc", 1)).toHaveText(when);
  await expect(cell(page, "undated", 1)).toHaveText("—");
  await rowAction(page.locator(".tr", { hasText: "orders-cdc" }), "View pipeline detail");
  await expect(page.locator(".kv", { has: page.getByText("Created at", { exact: true }) })).toContainText(when);
});

test("Schemas: Created at follows Contract, and the detail panel does not show it", async ({ page }) => {
  await seeded(page, "Schemas");
  expect((await heads(page)).slice(0, 3)).toEqual(["Contract", "Created at", "Pipeline Name"]);
  await expect(cell(page, "crm.sf.orders", 1)).toHaveText(when);
  await page.getByRole("button", { name: "View", exact: true }).first().click();
  await expect(page.locator(".drawer, .modal").last()).toBeVisible();
  await expect(page.locator(".drawer, .modal").last().getByText("Created at")).toHaveCount(0);
});

test("Dashboard: the deployed-pipelines table has Created at right after Pipeline, with the time the pipeline was created", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p1", org_id: "o", name: "orders-cdc", status: "running", topic_name: "t", connector_name: "c1", mode: "cdc", generated_config: {}, direction: "SOURCE", created_at: WHEN });
  await mockBackend(page, state);
  await page.goto("/");
  const table = page.getByRole("table", { name: "Deployed pipelines" });
  await expect(table.locator(".tbody .tr").first()).toBeVisible();
  await expect(table.getByRole("columnheader")).toHaveText(["Pipeline", "Created at", "Type", "Worker pool", "State", "Records read / written", ""]);
  await expect(table.locator(".tbody .tr").first().locator("[role=cell]").nth(1)).toHaveText(when);
});

test("Quality: Created at follows Quality Check Name", async ({ page }) => {
  await seeded(page, "Quality");
  expect((await heads(page)).slice(0, 3)).toEqual(["Quality Check Name", "Created at", "Rules"]);
  await expect(cell(page, "orders-rules", 1)).toHaveText(when);
});

test("Transforms: Created at follows Chain Name", async ({ page }) => {
  await seeded(page, "Transforms");
  expect((await heads(page)).slice(0, 3)).toEqual(["Chain Name", "Created at", "Type"]);
  await expect(cell(page, "mask-pii", 1)).toHaveText(when);
});

test("Connections: the list is in the order the backend sends (newest first) and every column heading looks the same, with no sort control", async ({ page }) => {
  const state = freshState();
  const c = (id: string, name: string, created_at?: string) => ({ id, org_id: "o", name, source_type: "postgresql", connection_config: { hostname: "h", database: name }, status: "active", ...(created_at ? { created_at } : {}) });
  // The backend sends newest first (ConnectionProfileRepository.newest_first); the page must not reorder it, whatever the names are.
  state.connections.push(c("c2", "zeta-newest", "2026-10-22T10:00:00Z"), c("c3", "mid", "2026-10-10T10:00:00Z"), c("c1", "alpha-oldest", "2026-10-01T10:00:00Z"));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".conn-grid >> text=postgresql").first().click();
  const names = () => page.locator(".tbody .tr").evaluateAll((rows) => rows.map((r) => r.querySelector("[role=cell]")!.textContent));
  await expect.poll(names).toEqual(["zeta-newest", "mid", "alpha-oldest"]);                     // as sent, not alphabetical

  const heads = page.locator(".thead [role=columnheader]");
  await expect(heads).toHaveText(["Friendly Name", "Created at", "Host", "Database", "Status", "Actions"]);   // plain text: no ⇅ / ▲ / ▼
  await expect(page.locator(".thead button")).toHaveCount(0);                                   // nothing is clickable to re-sort
  await heads.nth(0).click();
  await heads.nth(1).click();
  await expect.poll(names).toEqual(["zeta-newest", "mid", "alpha-oldest"]);                     // clicking a heading changes nothing
});

test("Pipelines, Schemas, Quality and Transforms list what the backend sends, newest first, in that order", async ({ page }) => {
  const state = freshState();
  const stamp = (n: number) => `2026-10-${String(10 + n).padStart(2, "0")}T10:00:00Z`;
  // The backend sends newest first; the pages must not reorder it (no name sort sneaks in).
  state.pipelines.push(...["zeta-new", "alpha-mid", "mike-old"].map((name, i) => ({ id: `p${i}`, org_id: "o", name, status: "draft", topic_name: `t${i}`, connector_name: `c${i}`, mode: "batch", generated_config: {}, created_at: stamp(3 - i) })));
  state.qualityPolicies.push(...["zeta-new", "alpha-mid", "mike-old"].map((name, i) => ({ id: `q${i}`, org_id: "o", name, rules: [], on_failure: "log", quarantine_sink: "kafka", created_at: stamp(3 - i) })));
  state.transforms.push(...["zeta-new", "alpha-mid", "mike-old"].map((name, i) => ({ id: `t${i}`, org_id: "o", name, description: "d", transforms: [], origin: "custom", created_at: stamp(3 - i) })));
  state.schemas.push(...["zeta", "alpha", "mike"].map((entity, i) => ({ id: `s${i}`, org_id: "o", domain: "d", source_system: "s", entity, version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] }, created_at: stamp(3 - i) })));
  await mockBackend(page, state);
  await page.goto("/");
  for (const [nav, first] of [["Pipelines", "zeta-new"], ["Quality", "zeta-new"], ["Transforms", "zeta-new"], ["Schemas", "d.s.zeta"]] as const) {
    await page.locator(".sn", { hasText: nav }).click();
    await expect(page.locator(".tbody .tr").first()).toContainText(first);
    await expect(page.locator(".tbody .tr").last()).toContainText(nav === "Schemas" ? "d.s.mike" : "mike-old");
  }
});
