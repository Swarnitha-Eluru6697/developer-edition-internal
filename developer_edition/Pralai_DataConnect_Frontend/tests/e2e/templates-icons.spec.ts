/*
 * Templates page: each card (and the details drawer) shows a round icon, one colour for all (white on the platform orange), like the Connections page's shapes — the product's own logo when one exists (simple-icons, or a mark traced from the product's logo — templateBrandLogos.ts), a category glyph otherwise —
 * and no longer the red initials tile ("AS" for Ably Sink).
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

function template(id: string, name: string, category: string, type = "sink") {
  return {
    id, name, connector_type: type, category, connector_class: "x.y.Z", runtime_pool_type: "kafka-connect", config_schema: {}, default_config: {},
    default_transforms: [], supported_transforms: [], version: "v1", is_active: true, supports_dlq: type === "sink", description: `${name} description`,
  };
}

async function open(page: import("@playwright/test").Page) {
  const state = freshState();
  state.templates = [
    template("t1", "Ably Sink", "messaging"), template("t2", "PostgreSQL Sink", "database"), template("t3", "Syslog Source", "messaging", "source"), template("t5", "Salesforce CDC Source", "api", "source"),
    template("t4", "Brand New Thing", "some_future_category", "source"),
  ];
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Templates" }).click();
}

test("no card shows initials any more: every card has a round icon with a drawn mark or glyph", async ({ page }) => {
  await open(page);
  await expect(page.locator(".tplcard")).toHaveCount(5);
  await expect(page.locator(".tpl-ico")).toHaveCount(0);
  for (const card of await page.locator(".tplcard").all()) {
    await expect(card.locator(".tpl-logo svg, .tpl-logo .tpl-mark")).toHaveCount(1);
    await expect(card.locator(".tpl-logo")).not.toContainText(/^[A-Z]{2}$/);
  }
});

test("the icon is round and the same size on every card", async ({ page }) => {
  await open(page);
  await expect(page.locator(".tplcard")).toHaveCount(5);                 // wait for the cards before measuring them
  const boxes = await page.locator(".tpl-logo").evaluateAll((els) => els.map((e) => {
    const r = e.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), radius: parseFloat(getComputedStyle(e).borderTopLeftRadius) };
  }));
  expect(new Set(boxes.map((b) => `${b.w}x${b.h}`)).size).toBe(1);
  expect(boxes[0].w).toBe(boxes[0].h);                                   // square box ...
  expect(boxes[0].radius).toBeGreaterThanOrEqual(boxes[0].w / 2);        // ... with a full radius: a circle
});

test("a template for a technology the Connections page knows draws that technology's logo, not the category glyph", async ({ page }) => {
  await open(page);
  const svgOf = (name: string) => page.locator(".tplcard", { hasText: name }).locator(".tpl-logo svg").innerHTML();
  // PostgreSQL ships as a filled brand path (drawn in the shared colour, not its own blue), the generic category glyph is stroked lines
  expect(await svgOf("PostgreSQL Sink")).toContain("fill=\"currentColor\"");
  expect(await svgOf("Salesforce CDC Source")).toContain("fill=\"currentColor\"");   // a product mark simple-icons dropped: still filled, still one colour
  expect(await svgOf("Syslog Source")).not.toContain("fill=");        // a protocol nobody owns: the category glyph
});

test("a product whose mark is traced from its own logo shows it, in the tile's colour", async ({ page }) => {
  await open(page);
  const mark = page.locator(".tplcard", { hasText: "Ably Sink" }).locator(".tpl-logo .tpl-mark");
  await expect(mark).toHaveCount(1);
  const look = await mark.evaluate((e) => { const s = getComputedStyle(e); return { mask: s.webkitMaskImage || s.maskImage, bg: s.backgroundColor, w: e.getBoundingClientRect().width }; });
  expect(look.mask).toContain("data:image/png;base64,");
  expect(look.bg).toBe("rgb(255, 255, 255)");
  expect(look.w).toBe(24);
});

test("a category the catalog gains later still gets an icon, never initials", async ({ page }) => {
  await open(page);
  await expect(page.locator(".tplcard", { hasText: "Brand New Thing" }).locator(".tpl-logo svg")).toHaveCount(1);
});

test("the details drawer shows the same icon", async ({ page }) => {
  await open(page);
  await page.locator(".tplcard", { hasText: "Ably Sink" }).click();
  await expect(page.locator(".tpl-logo-lg .tpl-mark")).toHaveCount(1);
});

// Every template icon is drawn in ONE colour — a white glyph on the platform's orange (the Catalog's card icons) — whatever the technology's own brand
// colour; the shapes are unchanged. The Connections page, which shares the logos, keeps their brand colours.
test("every template icon is the same colour: a white glyph on the platform orange, brand logos and category glyphs alike", async ({ page }) => {
  await open(page);
  await expect(page.locator(".tplcard")).toHaveCount(5);
  const looks = await page.locator(".tpl-logo").evaluateAll((els) => els.map((e) => {
    const mark = e.querySelector<HTMLElement>(".tpl-mark");
    if (mark) return { bg: getComputedStyle(e).backgroundColor, ink: getComputedStyle(mark).backgroundColor };
    const shape = e.querySelector("svg *")!, s = getComputedStyle(shape);
    return { bg: getComputedStyle(e).backgroundColor, ink: s.fill !== "none" ? s.fill : s.stroke };
  }));
  expect(looks).toHaveLength(5);
  for (const look of looks) expect(look).toEqual({ bg: "rgb(249, 115, 22)", ink: "rgb(255, 255, 255)" });

  await page.locator(".tplcard", { hasText: "Syslog Source" }).click();
  const large = await page.locator(".tpl-logo-lg").evaluate((e) => ({ bg: getComputedStyle(e).backgroundColor, ink: getComputedStyle(e.querySelector("svg *")!).stroke }));
  expect(large).toEqual({ bg: "rgb(249, 115, 22)", ink: "rgb(255, 255, 255)" });          // the details drawer's icon too
});

test("the Connections page keeps the logos' own brand colours", async ({ page }) => {
  await mockBackend(page, freshState());
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await expect(page.locator("svg path[fill^='#']").first()).toBeVisible();                  // a brand mark filled in its own hex colour
});
