/*
 * The side drawer (New pipeline, edit forms, ...) appears in place and goes away at once. It used to slide in from the right edge over ~0.3 s, which read as the
 * page "moving" every time a form was opened. These tests check where it is the instant after the click, and that nothing animates.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

async function open(page: Page) {
  const state = freshState();
  state.connections.push({ id: "c1", org_id: "org_test", name: "seed", source_type: "postgres", connection_config: {}, status: "active" });
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1500, height: 900 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).first().click();
}

test("the form is at its final position in the first frame after the click, with no transition", async ({ page }) => {
  await open(page);
  // Record the form's x position on every frame from the moment of the click.
  await page.evaluate(() => {
    const xs: number[] = []; (window as unknown as { __xs: number[] }).__xs = xs;
    const tick = () => { const m = document.querySelector(".modal"); if (m) xs.push(Math.round(m.getBoundingClientRect().x)); if (xs.length < 30) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  });
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();
  await page.waitForTimeout(700);

  const xs = await page.evaluate(() => (window as unknown as { __xs: number[] }).__xs);
  expect(xs.length).toBeGreaterThan(5);
  expect(new Set(xs).size).toBe(1);                                                          // it never moved: one position on every frame
  expect(xs[0]).toBe(1500 - 520);                                                            // and that is its place against the right edge
  const style = await page.locator(".modal").evaluate((e) => { const s = getComputedStyle(e); return { transform: s.transform, transition: s.transitionDuration }; });
  expect(style.transform).toBe("none");
  expect(style.transition).toBe("0s");
  expect(await page.locator(".ov").evaluate((e) => getComputedStyle(e).transitionDuration)).toBe("0s");   // the backdrop does not fade either
});

test("the page behind the form does not move when it opens or closes", async ({ page }) => {
  await open(page);
  const where = () => page.evaluate(() => ["#root .ph", ".tbl-fill", ".sb"].map((s) => { const b = document.querySelector(s)?.getBoundingClientRect(); return b ? [Math.round(b.x), Math.round(b.y), Math.round(b.width)] : null; }));
  const before = await where();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal")).toBeVisible();
  expect(await where()).toEqual(before);
  await page.locator(".modal .ff").getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".modal")).toHaveCount(0, { timeout: 500 });                      // gone at once, not after a slide-out
  expect(await where()).toEqual(before);
});

test("the dim backdrop is on as soon as the form is, and closing still works from the X, Escape and the backdrop", async ({ page }) => {
  await open(page);
  for (const close of [
    () => page.locator(".modal .mx").click(),
    () => page.keyboard.press("Escape"),
    () => page.locator(".ov").click({ position: { x: 20, y: 400 } }),
  ]) {
    await page.getByRole("button", { name: "+ New pipeline" }).click();
    await expect(page.locator(".ov.on")).toBeVisible();
    expect(await page.locator(".ov").evaluate((e) => getComputedStyle(e).backgroundColor)).toBe("rgba(0, 0, 0, 0.4)");
    await close();
    await expect(page.locator(".modal")).toHaveCount(0, { timeout: 500 });
  }
});
