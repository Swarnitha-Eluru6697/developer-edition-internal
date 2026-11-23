/*
 * DataConnect follows the platform UI pattern the Workspace app sets (shared/ui/IconAction.tsx, Glyph.tsx, HeaderSearch.tsx), so the two
 * can later ship as one application without a visible seam:
 *   - every row action is the same white 30px button: 1px #e5e7eb border, radius 8, gray #6b7280 glyph, red on hover for delete;
 *   - the glyphs are the shared drawings: 15px, 2px round stroke, drawn in the button's colour;
 *   - the search bar is 38px, gray fill, 1px #e5e7eb border, radius 9, with a lens icon;
 *   - "Run" is a play icon, not a text character.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend, rowMenuButton } from "./fixtures";

function seed() {
  const state = freshState();
  state.pipelines.push({ id: "p1", org_id: "org_test", name: "pattern-pipe", status: "running", topic_name: "t-1", connector_name: "c-1", mode: "api", generated_config: {} });
  state.qualityPolicies.push({ id: "q1", org_id: "org_test", name: "pattern-policy", rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  return state;
}

test("a pipeline row's three-dots button is the platform's icon-action, and its menu is a white card of icon + name lines, red for delete", async ({ page }) => {
  await mockBackend(page, seed());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  const dots = rowMenuButton(page.locator(".tr", { hasText: "pattern-pipe" }));
  await expect(dots).toBeVisible();
  const look = await dots.evaluate((e) => {
    const c = getComputedStyle(e), svg = e.querySelector("svg")!, s = getComputedStyle(svg);
    return { w: c.width, h: c.height, radius: c.borderTopLeftRadius, border: `${c.borderTopWidth} ${c.borderTopStyle} ${c.borderTopColor}`,
      bg: c.backgroundColor, color: c.color, iconSize: svg.getAttribute("width"), strokeWidth: s.strokeWidth, round: s.strokeLinecap };
  });
  expect(look).toEqual({
    w: "30px", h: "30px", radius: "8px", border: "1px solid rgb(229, 231, 235)", bg: "rgb(255, 255, 255)", color: "rgb(107, 114, 128)",
    iconSize: "15", strokeWidth: "2px", round: "round",
  });

  await dots.click();
  const menu = page.getByRole("menu");
  const card = await menu.evaluate((e) => { const c = getComputedStyle(e); return { bg: c.backgroundColor, radius: c.borderTopLeftRadius, border: `${c.borderTopWidth} ${c.borderTopStyle} ${c.borderTopColor}` }; });
  expect(card).toEqual({ bg: "rgb(255, 255, 255)", radius: "10px", border: "1px solid rgb(229, 231, 235)" });
  const line = page.getByRole("menuitem", { name: "Edit pattern-pipe" });
  await line.hover();
  await expect.poll(() => line.evaluate((e) => getComputedStyle(e).backgroundColor)).toBe("rgb(243, 244, 246)");   // a line lights up under the pointer
  const del = page.getByRole("menuitem", { name: "Delete pattern-pipe" });
  await del.hover();
  await expect.poll(() => del.evaluate((e) => getComputedStyle(e).backgroundColor)).toBe("rgb(254, 242, 242)");     // danger: red
  expect(await del.evaluate((e) => getComputedStyle(e).color)).toBe("rgb(185, 28, 28)");
});

test("the search bar is Workspace's: 38px, gray fill, 1px border, radius 9, with a lens", async ({ page }) => {
  const state = seed();
  state.connections.push({ id: "c-orders", org_id: "org_test", name: "orders-db", source_type: "postgresql", connection_config: { hostname: "orders.internal", database: "shop" }, status: "active" });
  await mockBackend(page, state);
  await page.goto("/");
  await page.getByRole("combobox", { name: "Search DataConnect" }).fill("orders-db");      // Connections' list is where a page search box lives now
  await page.locator(".hs-opt").first().click();
  const bar = page.locator(".srch").first();
  await expect(bar).toBeVisible();
  const look = await bar.evaluate((e) => {
    const c = getComputedStyle(e), input = getComputedStyle(e.querySelector("input")!);
    return { h: c.height, radius: c.borderTopLeftRadius, border: `${c.borderTopWidth} ${c.borderTopStyle} ${c.borderTopColor}`, bg: c.backgroundColor,
      font: input.fontSize, hasLens: !!e.querySelector("svg circle") };
  });
  expect(look).toEqual({ h: "38px", radius: "9px", border: "1px solid rgb(229, 231, 235)", bg: "rgb(249, 250, 251)", font: "13.5px", hasLens: true });
});

test("Quality's run action is a play icon, and the delete action matches the other pages", async ({ page }) => {
  await mockBackend(page, seed());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  const run = page.getByRole("button", { name: "Execute pattern-policy" });
  await expect(run).toBeVisible();
  expect(await run.innerText()).toBe("");                                     // no "▶" text character any more
  await expect(run.locator("svg path")).toHaveAttribute("d", "M7 4v16l13 -8z");   // the play triangle Workspace uses for Run
  const del = page.getByRole("button", { name: "Delete pattern-policy" });
  expect(await del.evaluate((e) => getComputedStyle(e).borderTopColor)).toBe("rgb(229, 231, 235)");
});

test("the Settings sidebar icon is Workspace's cog: a ring with eight rays", async ({ page }) => {
  await mockBackend(page, seed());
  await page.goto("/");
  const icon = page.locator(".sn", { hasText: "Settings" }).locator("svg");
  await expect(icon.locator("circle")).toHaveAttribute("r", "3");
  await expect(icon.locator("path")).toHaveAttribute("d", "M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1");
});

test("the open sidebar item is the Catalog's: tinted, bold, with a 2px orange rail curving along the rounded left edge", async ({ page }) => {
  await mockBackend(page, seed());
  await page.goto("/");
  const active = page.locator(".sn.active");
  await expect(active).toHaveCount(1);
  const look = await active.evaluate((e) => { const c = getComputedStyle(e); return { radius: c.borderTopLeftRadius, weight: c.fontWeight, shadow: c.boxShadow, bg: c.backgroundColor }; });
  expect(look).toEqual({ radius: "9px", weight: "600", shadow: "rgb(249, 115, 22) 2px 0px 0px 0px inset", bg: "rgb(255, 247, 237)" });
  await expect(page.locator(".sn:not(.active)").first()).toHaveCSS("box-shadow", "none");     // only the open one carries the rail
});
