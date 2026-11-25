/*
 * Every dropdown in DataConnect is the platform's SearchSelect (the Catalog's, components/SearchSelect.tsx behind components/Select.tsx): a trigger
 * with a chevron, and a panel with a filter box, rows with a check on the chosen one, and a "N options · ↑↓ move · ↵ select · esc close" footer.
 * No browser <select> is left in a form.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

async function open(page: Page, nav: string, button: string) {
  const state = freshState();
  state.connections.push({ id: "c1", org_id: "o", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  state.pipelines.push({ id: "p1", org_id: "o", name: "steady-pipe", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: nav }).click();
  await page.getByRole("button", { name: button }).first().click();
  await expect(page.locator(".modal, .drawer").last()).toBeVisible();
}

test("no browser <select> is left in the forms that had one", async ({ page }) => {
  for (const [nav, button] of [["Pipelines", "+ New pipeline"], ["Schemas", "+ Register schema"], ["Quality", "+ New Quality Check"]] as const) {
    await open(page, nav, button);
    await expect(page.locator("select")).toHaveCount(0);
    await expect(page.locator(".ss-trigger").first()).toBeVisible();
    await page.keyboard.press("Escape");
  }
});

test("a dropdown looks like the Catalog's: bordered trigger, orange when open; panel with a filter, a check and a footer", async ({ page }) => {
  await open(page, "Pipelines", "+ New pipeline");
  const zone = page.locator("#pipe-zone");
  await expect(zone).toHaveCSS("border-top-color", "rgb(229, 231, 235)");
  await expect(zone).toHaveCSS("border-top-left-radius", "8px");
  await zone.click();
  await page.mouse.move(5, 5);                                                                  // off the trigger: hover would grey its border
  await expect(zone).toHaveCSS("border-top-color", "rgb(249, 115, 22)");                     // open: the orange border
  const menu = page.locator(".ss-menu");
  await expect(menu.getByPlaceholder("Type to filter…")).toBeFocused();
  await expect(menu).toHaveCSS("border-top-left-radius", "10px");
  const chosen = menu.getByRole("option", { name: "raw" });
  await expect(chosen).toHaveAttribute("aria-selected", "true");
  await expect(chosen).toHaveCSS("color", "rgb(194, 65, 12)");                                // the chosen row: orange text …
  await expect(chosen).toHaveCSS("background-color", "rgb(255, 237, 213)");                   // … on the light orange fill (a shade deeper while it is the highlighted row) …
  await expect(chosen.locator(".ss-check")).toBeVisible();                                    // … and a check
  await expect(menu.locator(".ss-foot")).toContainText("5 options");
  await expect(menu.locator(".ss-foot")).toContainText("↑↓ move · ↵ select · esc close");
});

test("typing filters, Enter picks, and what was picked is shown and used", async ({ page }) => {
  await open(page, "Pipelines", "+ New pipeline");
  const zone = page.locator("#pipe-zone");
  await zone.click();
  await page.keyboard.type("cur");
  await expect(page.locator(".ss-menu [role=option]")).toHaveText(["curated"]);
  await expect(page.locator(".ss-foot")).toContainText("1 of 5");
  await page.keyboard.press("Enter");
  await expect(zone).toContainText("curated");
  await expect(page.locator(".ss-menu")).toHaveCount(0);
  await expect(page.getByText(/^Topic: .*-curated-/)).toBeVisible();                          // the form really took it
});

test("inside a drawer the panel is above it, and Escape closes only the panel", async ({ page }) => {
  await open(page, "Pipelines", "+ New pipeline");
  await page.locator("#pipe-zone").click();
  const row = page.locator(".ss-menu [role=option]").first();
  const covered = await row.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !(hit === el || el.contains(hit));
  });
  expect(covered).toBe(false);                                                                  // not hidden behind the drawer
  await page.keyboard.press("Escape");
  await expect(page.locator(".ss-menu")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create", exact: true })).toBeVisible();        // the drawer is still there
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Create", exact: true })).toHaveCount(0);       // a second Escape closes it
});

test("a disabled dropdown cannot be opened", async ({ page }) => {
  await open(page, "Pipelines", "+ New pipeline");
  const mode = page.locator("#pipe-quality-mode");                                              // disabled until a quality policy is chosen
  await expect(mode).toBeDisabled();
  await mode.click({ force: true });
  await expect(page.locator(".ss-menu")).toHaveCount(0);
});

test("a dropdown near the right edge of the window opens fully inside it (Quality → Results range filter)", async ({ page }) => {
  const state = freshState();
  await mockBackend(page, state);
  for (const width of [1100, 1500]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/");
    await page.locator(".sn", { hasText: "Quality" }).click();
    await page.getByRole("tab", { name: "Results" }).click();
    await page.getByRole("tab", { name: "Validation" }).click();
    await page.locator("#quality-range").click();
    const menu = (await page.locator(".ss-menu").boundingBox())!;
    const trigger = (await page.locator("#quality-range").boundingBox())!;
    expect(menu.x, `${width}px: left edge on screen`).toBeGreaterThanOrEqual(0);
    expect(menu.x + menu.width, `${width}px: right edge on screen`).toBeLessThanOrEqual(width);
    expect(menu.x + menu.width, "lines up with the button's right edge, not beyond it").toBeLessThanOrEqual(trigger.x + trigger.width + 8);
  }
});
