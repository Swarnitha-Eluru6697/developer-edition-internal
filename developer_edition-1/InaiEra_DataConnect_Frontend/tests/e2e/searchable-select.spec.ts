/*
 * The Lineage page's pipeline picker is a searchable dropdown (components/SearchSelect.tsx — the Catalog's), not the browser's <select>: a button showing
 * the current pipeline, and under it a "Type to filter…" box above a scrolling list.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const NAMES = ["pg-products-cdc", "pg-products-sink", "ingest-accounts2", "ingest-accounts3", "splunk-s2s-demo", "file-demo-orders"];

async function openLineage(page: Page) {
  const state = freshState();
  NAMES.forEach((name, i) => state.pipelines.push({
    id: `pipe-${i + 1}`, org_id: "org_test", name, status: "running", topic_name: `t-${i}`, connector_name: `c-${i}`, mode: "cdc", generated_config: {},
  }));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  return page.getByRole("button", { name: "Select pipeline" });
}

test("the picker shows the current pipeline on a button, and nothing is open until it is clicked", async ({ page }) => {
  const btn = await openLineage(page);
  await expect(btn).toContainText("pg-products-cdc");
  await expect(btn).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator(".ss-menu")).toHaveCount(0);
});

test("opening it shows a filter box (focused) above the list of every pipeline", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  const search = page.getByPlaceholder("Type to filter…");
  await expect(search).toBeFocused();
  await expect(page.getByRole("option")).toHaveText(NAMES);
  await expect(btn).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("option", { name: "pg-products-cdc" })).toHaveAttribute("aria-selected", "true");
});

test("typing narrows the list (case-insensitive, anywhere in the name)", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  await page.getByPlaceholder("Type to filter…").fill("ACCOUNTS");
  await expect(page.getByRole("option")).toHaveText(["ingest-accounts2", "ingest-accounts3"]);
  await page.getByPlaceholder("Type to filter…").fill("products");
  await expect(page.getByRole("option")).toHaveText(["pg-products-cdc", "pg-products-sink"]);
});

test("a filter that matches nothing says so instead of showing an empty box", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  await page.getByPlaceholder("Type to filter…").fill("zzz-nothing");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(page.locator(".ss-empty")).toHaveText("No matching pipelines");
});

test("clicking an option selects it, closes the list, and the graph follows the choice", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  await page.getByRole("option", { name: "splunk-s2s-demo" }).click();
  await expect(page.locator(".ss-menu")).toHaveCount(0);
  await expect(btn).toContainText("splunk-s2s-demo");
  await btn.click();
  await expect(page.getByRole("option", { name: "splunk-s2s-demo" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByPlaceholder("Type to filter…")).toHaveValue("");              // reopened fresh, not with the old filter
});

test("keyboard: type, arrow down, Enter chooses; Escape closes without changing", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  await page.keyboard.type("ingest");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(btn).toContainText("ingest-accounts3");
  await btn.click();
  await page.keyboard.press("Escape");
  await expect(page.locator(".ss-menu")).toHaveCount(0);
  await expect(btn).toContainText("ingest-accounts3");
});

test("the list opens from the keyboard too (arrow down on the button)", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByPlaceholder("Type to filter…")).toBeFocused();
});

test("clicking elsewhere closes it", async ({ page }) => {
  const btn = await openLineage(page);
  await btn.click();
  await page.getByText("Visual data flow from source to sink").click();
  await expect(page.locator(".ss-menu")).toHaveCount(0);
});

test("a long list scrolls inside the box instead of growing the page", async ({ page }) => {
  const state = freshState();
  for (let i = 0; i < 40; i++) state.pipelines.push({ id: `p${i}`, org_id: "org_test", name: `pipeline-${i}`, status: "running", topic_name: "t", connector_name: "c", mode: "cdc", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await page.getByRole("button", { name: "Select pipeline" }).click();
  const box = await page.locator(".ss-list").evaluate((el) => ({ client: el.clientHeight, scroll: el.scrollHeight }));
  expect(box.client).toBeLessThanOrEqual(248);
  expect(box.scroll).toBeGreaterThan(box.client);
});

test("with no pipelines the button says so and is disabled, and the graph shows its empty state", async ({ page }) => {
  await mockBackend(page, freshState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await expect(page.getByText("Select a pipeline (deploy one first to build lineage)")).toBeVisible();
  const btn = page.getByRole("button", { name: "Select pipeline" });
  await expect(btn).toBeDisabled();
  await expect(btn).toContainText("— no pipelines —");
});

test("the long list still scrolls, but draws no scrollbar", async ({ page }) => {
  const state = freshState();
  for (let i = 0; i < 40; i++) state.pipelines.push({ id: `p${i}`, org_id: "org_test", name: `pipeline-${i}`, status: "running", topic_name: "t", connector_name: "c", mode: "cdc", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await page.getByRole("button", { name: "Select pipeline" }).click();
  const list = page.locator(".ss-list");
  expect(await list.evaluate((el) => getComputedStyle(el).scrollbarWidth)).toBe("none");                      // no bar is drawn ...
  expect(await list.evaluate((el) => el.offsetWidth - el.clientWidth)).toBe(0);                                 // ... and none takes up room
  await list.hover();
  await page.mouse.wheel(0, 300);
  await expect.poll(() => list.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);                              // ... but it still scrolls
});

// The list used to be positioned inside the "Pipeline flow" card, whose overflow:hidden cut it off at the card's bottom edge — with a
// short (empty-state) card only two names showed, half hidden. It is now placed against the window, so nothing around it can clip it.
async function openWithManyPipelines(page: Page) {
  const state = freshState();
  for (let i = 0; i < 30; i++) state.pipelines.push({
    id: `pipe-${i + 1}`, org_id: "org_test", name: `pipeline-${String(i + 1).padStart(2, "0")}`, status: "draft",
    topic_name: `t-${i}`, connector_name: `c-${i}`, mode: "batch", generated_config: {},
  });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  const btn = page.getByRole("button", { name: "Select pipeline" });
  await btn.click();
  return btn;
}

/** Is the option's centre actually painted on screen (not cut off by a parent, not covered)? */
async function visibleAtItsCentre(page: Page, name: string) {
  return page.getByRole("option", { name, exact: true }).evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit === el || el.contains(hit);
  });
}

test("the list is not cut off by the short Pipeline flow card around it", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openWithManyPipelines(page);
  const card = await page.locator(".card", { hasText: "Pipeline flow" }).boundingBox();
  const pop = await page.locator(".ss-menu").boundingBox();
  expect(pop!.y + pop!.height).toBeGreaterThan(card!.y + card!.height);   // reaches past the card's bottom edge
  expect(pop!.y + pop!.height).toBeLessThanOrEqual(900);                     // and stays inside the window
  for (const name of ["pipeline-01", "pipeline-04", "pipeline-06"]) expect(await visibleAtItsCentre(page, name)).toBe(true);
  await page.getByRole("option", { name: "pipeline-06", exact: true }).click();
  await expect(page.getByRole("button", { name: "Select pipeline" })).toContainText("pipeline-06");
});

test("with no room below, the list opens above the button and fits the space", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 330 });
  const btn = await openWithManyPipelines(page);
  const b = await btn.boundingBox();
  const pop = await page.locator(".ss-menu").boundingBox();
  expect(pop!.y + pop!.height).toBeLessThanOrEqual(b!.y);   // entirely above the button
  expect(pop!.y).toBeGreaterThanOrEqual(0);
  expect(await visibleAtItsCentre(page, "pipeline-01")).toBe(true);
  await expect(page.getByPlaceholder("Type to filter…")).toBeFocused();
});
