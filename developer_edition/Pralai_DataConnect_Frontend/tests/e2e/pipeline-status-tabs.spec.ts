/*
 * The Pipelines list's status counts are tabs: "All 68 · Running 47 · Deployed 0 · Paused 10 · Failed 0 · Draft 9". All shows everything (as the list
 * always did); another shows only the pipelines in that status. Search, the row menu and the list's own scrolling work in every tab.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, rowMenuButton } from "./fixtures";

const pipe = (i: number, status: string) => ({ id: `p${i}`, org_id: "o", name: `pipe-${String(i).padStart(2, "0")}`, status, topic_name: `t${i}`, connector_name: `c${i}`, mode: "batch", generated_config: {} });

async function open(page: Page, many = false) {
  const state = freshState();
  const spec: [string, number][] = many ? [["running", 14], ["paused", 3], ["draft", 2], ["failed", 1]] : [["running", 2], ["paused", 1], ["draft", 1], ["failed", 1]];
  let i = 1;
  for (const [status, n] of spec) for (let k = 0; k < n; k++) state.pipelines.push(pipe(i++, status));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(page.locator(".tbody .tr").first()).toBeVisible();
}
const tab = (page: Page, name: string) => page.getByRole("tab", { name: new RegExp(`^${name}\\b`) });
const names = (page: Page) => page.locator(".tbody .tr").evaluateAll((rows) => rows.map((r) => r.querySelector("[role=cell]")!.textContent!.replace(/^\d+%?/, "").trim()));

test("the title is gone: the status counts lead the card, starting with All, each a tab", async ({ page }) => {
  await open(page);
  await expect(page.getByText(/^Pipelines \(\d+\)$/)).toHaveCount(0);
  await expect(page.getByRole("tab")).toHaveText(["All 5", "Running 2", "Deployed 0", "Paused 1", "Failed 1", "Draft 1"]);
  await expect(tab(page, "All")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".tbody .tr")).toHaveCount(5);                          // All: the whole list, as before
});

test("clicking a status shows only those pipelines, and All brings everything back", async ({ page }) => {
  await open(page);
  await tab(page, "Running").click();
  await expect(tab(page, "Running")).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, "All")).toHaveAttribute("aria-selected", "false");
  expect(await names(page)).toEqual(["pipe-01", "pipe-02"]);
  await expect(page.locator(".tbody .tr .badge", { hasText: /^running$/ })).toHaveCount(2);
  await tab(page, "Paused").click();
  expect(await names(page)).toEqual(["pipe-03"]);
  await tab(page, "Draft").click();
  expect(await names(page)).toEqual(["pipe-04"]);
  await tab(page, "All").click();
  await expect(page.locator(".tbody .tr")).toHaveCount(5);
  await expect(page.getByRole("tab")).toHaveText(["All 5", "Running 2", "Deployed 0", "Paused 1", "Failed 1", "Draft 1"]);   // the counts never change with the tab
});

test("a status with no pipelines says so, and the counts are still there", async ({ page }) => {
  await open(page);
  await tab(page, "Deployed").click();
  await expect(page.locator(".tbody .tr")).toHaveCount(0);
  await expect(page.getByText("No deployed pipelines")).toBeVisible();
  await expect(tab(page, "Running")).toContainText("2");
});

test("the arrow keys, Home and End move between the tabs", async ({ page }) => {
  await open(page);
  await tab(page, "All").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Running")).toBeFocused();
  await expect(tab(page, "Running")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tab(page, "Draft")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");                                           // wraps round to All
  await expect(tab(page, "All")).toHaveAttribute("aria-selected", "true");
});

test("a pipeline picked in the top search narrows the chosen tab too, and the chip lifts it", async ({ page }) => {
  await open(page);
  await tab(page, "Running").click();
  await page.getByRole("combobox", { name: "Search DataConnect" }).fill("pipe-03");       // paused: not in the Running tab
  await page.locator(".hs-opt").first().click();
  await expect(page.getByRole("button", { name: /Clear filter pipe-03/ })).toBeVisible();
  await expect(page.locator(".tbody .tr")).toHaveCount(0);
  await expect(page.getByText("No pipelines match")).toBeVisible();
  await page.getByRole("button", { name: /Clear filter/ }).click();
  expect(await names(page)).toEqual(["pipe-01", "pipe-02"]);
});

test("the row menu works from a filtered tab, and the list still scrolls inside itself, with no scrollbar and the header staying put", async ({ page }) => {
  await page.setViewportSize({ width: 1300, height: 640 });
  await open(page, true);
  await tab(page, "Running").click();
  await expect(page.locator(".tbody .tr")).toHaveCount(14);
  const body = page.locator("#pipelines-table .tbody");
  const m = await body.evaluate((e) => ({ scroll: e.scrollHeight > e.clientHeight, bar: e.offsetWidth - e.clientWidth }));
  expect(m.scroll).toBe(true);                                                         // it scrolls inside the card, not the page …
  expect(m.bar).toBe(0);                                                               // … and draws no scrollbar
  const headY = (await page.locator("#pipelines-table .thead").boundingBox())!.y;
  await body.evaluate((e) => { e.scrollTop = 300; });
  expect((await page.locator("#pipelines-table .thead").boundingBox())!.y).toBe(headY);      // the column header stays put
  await tab(page, "Paused").click();                                                   // a new tab starts from its first row
  expect(await body.evaluate((e) => e.scrollTop)).toBe(0);
  await rowMenuButton(page.locator(".tr", { hasText: "pipe-15" })).click();
  await expect(page.getByRole("menuitem", { name: /^Resume pipe-15$/ })).toBeEnabled();     // a paused pipeline offers Resume
});
