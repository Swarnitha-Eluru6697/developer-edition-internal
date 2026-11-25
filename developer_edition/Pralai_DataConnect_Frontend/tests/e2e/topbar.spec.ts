/*
 * The top bar: the page's heading appears once (the page's own, never repeated in the bar), the bar's one search finds everything in
 * DataConnect — pipelines, connections, schemas, quality policies, transform policies, templates and the pages — and a picked result lands on
 * the item. (The role pills are covered in user-simulator.spec.ts.)
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const TEMPLATE = {
  id: "tpl-orders", name: "Orders Feed Source", connector_type: "source", category: "api", connector_class: "x.y.Z", runtime_pool_type: "kafka-connect",
  config_schema: {}, default_config: {}, default_transforms: [], supported_transforms: [], version: "v1", is_active: true, supports_dlq: false,
  description: "Orders feed",
};
const SCHEMA = {
  id: "sch-orders", org_id: "org_test", domain: "crm", source_system: "salesforce", entity: "orders", version: "v1", schema_format: "json_schema",
  compatibility: "BACKWARD", status: "active", schema_definition: { fields: [{ name: "id", type: "integer", nullable: false }] },
};
const POLICY = { id: "qp-orders", org_id: "org_test", name: "orders-rules", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" };

async function seeded(page: Page) {
  const state = freshState();
  state.pipelines.push({
    id: "p-orders", org_id: "org_test", name: "orders-cdc", status: "running", topic_name: "org_test-raw-crm-salesforce-orders-cdc-v1",
    connector_name: "orders-cdc-connector", mode: "cdc", generated_config: {},
  }, { id: "p-other", org_id: "org_test", name: "inventory-sync", status: "paused", topic_name: "t-2", connector_name: "c-2", mode: "api", generated_config: {} });
  state.connections.push({ id: "c-orders", org_id: "org_test", name: "orders-db", source_type: "postgresql", connection_config: { hostname: "orders.internal", database: "shop" }, status: "active" });
  state.schemas.push(SCHEMA);
  state.qualityPolicies.push(POLICY);
  state.transforms.push({ id: "tf-orders", org_id: "org_test", name: "orders-mask", description: "mask", transforms: [] });
  state.templates.push(TEMPLATE);
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/schemas/sch-orders", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SCHEMA) }));
  await page.route("**/api/v1/dataconnect/quality-policies/qp-orders", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(POLICY) }));
  await page.goto("/");
}

const search = (page: Page) => page.getByRole("combobox", { name: "Search DataConnect" });
const options = (page: Page) => page.locator(".hs-pop .hs-opt");
const groups = (page: Page) => page.locator(".hs-pop .hs-grp");

test("a page's heading appears once: the top bar carries no title of its own", async ({ page }) => {
  await seeded(page);
  for (const title of ["Dashboard", "Monitoring", "Connections", "Pipelines", "Schemas", "Quality", "Lineage", "Transforms", "Templates", "Settings"]) {
    await page.locator(".sn", { hasText: title }).click();
    await expect(page.locator(".bc")).toHaveCount(0);
    await expect(page.locator(".topbar")).not.toContainText(title);
    await expect(page.locator(".ph")).toBeVisible();                // the page's own heading is the one that names it
  }
});

test("the top bar is the search on the left and the user on the right (no role switcher)", async ({ page }) => {
  await seeded(page);
  const bar = (await page.locator(".topbar").boundingBox())!;
  const box = (await page.locator(".hs-box").boundingBox())!;
  const avatar = (await page.locator(".tb-av").boundingBox())!;
  await expect(page.locator(".role-switch, .user-sim-btn")).toHaveCount(0);
  expect(box.x - bar.x).toBeLessThan(40);                                          // the search starts at the left edge
  expect(box.x + box.width).toBeLessThan(avatar.x);
  expect(box.height).toBe(38);                                                      // Workspace's header search
  await expect(page.locator(".hs-kbd")).toHaveText("/");
});

test("one search finds pipelines, connections, schemas, quality and transform policies and templates, grouped", async ({ page }) => {
  await seeded(page);
  await search(page).fill("orders");
  await expect(groups(page)).toHaveText(["Pipelines", "Connections", "Schemas", "Quality Checks", "SMT chains", "Templates"]);
  await expect(options(page)).toHaveText([/orders-cdc/, /orders-db/, /crm\.salesforce\.orders/, /orders-rules/, /orders-mask/, /Orders Feed Source/]);
  await expect(page.locator(".hs-opt", { hasText: "inventory-sync" })).toHaveCount(0);       // only what matches

  await search(page).fill("orders.internal");                                                // also by what's inside: a connection's host
  await expect(options(page)).toHaveText([/orders-db/]);
  await search(page).fill("raw crm orders");                                                 // several words, in any order: all must match
  await expect(options(page)).toContainText([/orders-cdc/]);
  await search(page).fill("zzzz");
  await expect(page.locator(".hs-none")).toContainText("No matches for “zzzz”");
});

test("the pages themselves are searchable too", async ({ page }) => {
  await seeded(page);
  await search(page).fill("sett");
  await expect(groups(page)).toHaveText(["Pages"]);
  await options(page).first().click();
  await expect(page.locator(".ph")).toContainText("Settings");
  await expect(search(page)).toHaveValue("");                                                // picking clears the box
});

test("picking a pipeline lands on Pipelines, narrowed to it", async ({ page }) => {
  await seeded(page);
  await search(page).fill("orders-cdc");
  await options(page).first().click();
  await expect(page.locator(".ph")).toContainText("Pipelines");
  await expect(page.locator(".srch")).toHaveCount(0);                                        // the page has no search box of its own
  await expect(page.getByRole("button", { name: "Clear filter orders-cdc" })).toBeVisible();
  await expect(page.locator(".tbody .tr")).toHaveCount(1);
  await expect(page.locator(".tbody .tr")).toContainText("orders-cdc");
  await page.getByRole("button", { name: "Clear filter orders-cdc" }).click();               // the chip lifts the filter
  await expect(page.locator(".tbody .tr")).toHaveCount(2);
  await expect(page.getByRole("button", { name: /Clear filter/ })).toHaveCount(0);
});

test("picking a connection lands on its type's list with the search on its name", async ({ page }) => {
  await seeded(page);
  await search(page).fill("orders-db");
  await options(page).first().click();
  await expect(page.locator(".ph")).toContainText("postgresql");
  await expect(page.locator(".srch input")).toHaveValue("orders-db");
  await expect(page.locator(".tbody .tr")).toContainText("orders-db");
});

test("a connection TYPE is found too, and picking it opens that type's list", async ({ page }) => {
  await seeded(page);
  await search(page).fill("postgres");
  await expect(groups(page)).toContainText(["Connection types"]);
  const typeOption = page.locator(".hs-opt", { hasText: "1 connection" }).first();           // postgresql: one connection in the seed data
  await expect(typeOption).toContainText("postgresql");
  await typeOption.click();
  await expect(page.locator(".ph")).toContainText("postgresql");
  await expect(page.locator(".srch input")).toHaveValue("");                                  // the whole list, not filtered to a name
  await expect(page.locator(".tbody .tr")).toContainText("orders-db");
});

test("picking a schema, a quality check, an SMT chain or a template opens its details", async ({ page }) => {
  await seeded(page);
  for (const [query, page_, shows] of [
    ["crm.salesforce.orders", "Schemas", "crm.salesforce.orders"],
    ["orders-rules", "Quality", "orders-rules"],
    ["orders-mask", "Transforms", "orders-mask"],
    ["Orders Feed", "Templates", "Orders Feed Source"],
  ] as const) {
    await search(page).fill(query);
    await options(page).first().click();
    await expect(page.locator(".ph")).toContainText(page_);
    await expect(page.locator(".modal, .drawer").last()).toContainText(shows);
    await page.keyboard.press("Escape");
    await page.locator(".modal .ft button, .drawer .ft button", { hasText: "Close" }).first().click({ timeout: 1500 }).catch(() => undefined);
  }
});

test("it works from the keyboard: / focuses it, the arrows move, Enter opens, Esc closes", async ({ page }) => {
  await seeded(page);
  await page.locator("body").click({ position: { x: 600, y: 400 } });
  await page.keyboard.press("/");
  await expect(search(page)).toBeFocused();

  await search(page).fill("orders");
  await expect(options(page).first()).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowDown");
  await expect(options(page).nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(options(page).first()).toHaveAttribute("aria-selected", "false");
  await page.keyboard.press("Escape");
  await expect(page.locator(".hs-pop")).toHaveCount(0);

  await search(page).fill("orders-db");
  await page.keyboard.press("Enter");                                                         // the highlighted result opens
  await expect(page.locator(".ph")).toContainText("postgresql");
});

test("typing '/' inside a text field types a slash and does not steal the focus", async ({ page }) => {
  await seeded(page);
  await search(page).fill("orders-db");
  await options(page).first().click();                                                       // lands on the connections list, which has a text field
  await page.locator(".srch input").first().fill("a");
  await page.locator(".srch input").first().press("/");
  await expect(page.locator(".srch input").first()).toHaveValue("a/");
  await expect(search(page)).not.toBeFocused();
});

test("nothing is fetched for the search until the box is used", async ({ page }) => {
  const seen: string[] = [];
  page.on("request", (r) => { if (/\/api\/v1\/dataconnect\/(schemas|quality-policies|transform-policies|connector-templates)$/.test(r.url())) seen.push(new URL(r.url()).pathname.split("/").pop()!); });
  await seeded(page);
  await expect(page.locator(".ph")).toContainText("Dashboard");
  await page.waitForTimeout(800);
  expect(seen).toEqual([]);                                                                   // the Dashboard doesn't need them: the search hasn't asked either
  await search(page).focus();
  await expect.poll(() => seen.sort()).toEqual(["connector-templates", "quality-policies", "schemas", "transform-policies"]);
});
