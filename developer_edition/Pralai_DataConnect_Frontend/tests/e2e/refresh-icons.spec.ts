/*
 * Refresh controls are icons, not words: Audit's, Pipelines' and Monitoring's "Refresh" are the same icon button, so the
 * page headers read the same way (Workspace pattern: actions are icons with a tooltip).
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

test("Audit's refresh is an icon button that reloads the page at once, like the browser's own refresh", async ({ page }) => {
  const state = freshState();
  state.audit.push({ id: "a1", action: "created", resource_type: "pipeline", resource_id: "p1", actor: "me", created_at: "2026-10-17T10:00:00Z" });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Audit" }).click();
  const refresh = page.getByRole("button", { name: "Refresh", exact: true });
  await expect(refresh).toBeVisible();
  await expect(refresh).toHaveText("");                              // an icon, no word
  await expect(refresh.locator("svg")).not.toHaveClass(/spin-icon/);
  await page.evaluate(() => { (window as unknown as { __marker: string }).__marker = "same page"; });
  const reloaded = page.waitForEvent("load");
  await refresh.click();
  await reloaded;                                                    // a real page reload, no animation first
  expect(await page.evaluate(() => (window as unknown as { __marker?: string }).__marker)).toBeUndefined();
  await expect(page.locator(".ph")).toContainText("Audit");          // back on the same page
});

test("icon tooltips appear within a fraction of a second, not after the browser's slow title delay", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const refresh = page.getByRole("button", { name: "Refresh", exact: true });
  await refresh.hover();
  await expect(page.getByRole("tooltip")).toHaveText("Refresh", { timeout: 800 });
  await expect(refresh).not.toHaveAttribute("title", /./);              // the slow native one is held back meanwhile
  await page.locator(".ph").hover();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await expect(refresh).toHaveAttribute("title", "Refresh");            // and put back when the pointer leaves
});

test("Pipelines refreshes itself every 20 seconds with no switch, and the refresh icon reloads at once and restarts the 20 seconds", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p1", org_id: "o", name: "steady-pipe", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {} });
  await mockBackend(page, state);
  await page.clock.install();
  let lists = 0;
  await page.route("**/api/v1/dataconnect/pipelines", (route) => { if (route.request().method() === "GET") lists += 1; return route.fallback(); });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect.poll(() => lists).toBe(1);

  // no on/off switch, no word: just the refresh icon, the same one Audit and Monitoring have
  await expect(page.getByRole("button", { name: "Auto-refresh" })).toHaveCount(0);
  await expect(page.locator("label", { hasText: "auto-refresh" })).toHaveCount(0);
  const refresh = page.getByRole("button", { name: "Refresh", exact: true });
  await expect(refresh).toHaveText("");
  await expect(refresh).toHaveClass(/icon-btn/);

  await page.clock.runFor(28_000);                                         // longer than the slowest interval (24s): it refreshed on its own
  await expect.poll(() => lists).toBeGreaterThan(1);

  await page.clock.runFor(0);
  const marker = "same page";
  await page.evaluate((m) => { (window as unknown as { __m: string }).__m = m; }, marker);
  const reloaded = page.waitForEvent("load");
  await refresh.click();                                                   // at once, not at the next 20s mark
  await reloaded;
  expect(await page.evaluate(() => (window as unknown as { __m?: string }).__m)).toBeUndefined();
  await expect(page.locator(".ph")).toContainText("Pipelines");
});

test("Transforms shows three stat cards, none of them empty, filling the row", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  const cards = page.locator(".stats .sc");
  await expect(cards).toHaveCount(3);
  await expect(cards.locator(".sc-l")).toHaveText(["SMT chains", "Total SMTs", "SMT types"]);
  const row = (await page.locator(".stats").boundingBox())!;
  const last = (await cards.last().boundingBox())!;
  expect(Math.round(last.x + last.width)).toBe(Math.round(row.x + row.width));      // no gap where the fourth card was
});

test("the Actions heading sits centred over the row's action icons", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p1", org_id: "o", name: "steady-pipe", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const head = (await page.getByRole("columnheader", { name: "Actions" }).locator("xpath=.").boundingBox())!;
  const icons = page.locator(".tbody .tr").first().locator(".acts .ai");
  const first = (await icons.first().boundingBox())!;
  const last = (await icons.last().boundingBox())!;
  const headMid = head.x + head.width / 2, iconsMid = (first.x + last.x + last.width) / 2;
  expect(Math.abs(headMid - iconsMid)).toBeLessThan(3);                              // heading and icon group share a centre
});
