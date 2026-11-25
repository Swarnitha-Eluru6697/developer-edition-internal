/*
 * Scroll areas scroll but show no scrollbar. Headless Chromium hides every scrollbar by default, which would make this check
 * pass for the wrong reason, so real scrollbars are switched back on for this file.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, choose } from "./fixtures";

test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });

function pipe(id: string, name: string) {
  return { id, org_id: "org_test", name, status: "running", topic_name: `t-${id}`, connector_name: `conn-${id}`, mode: "api", generated_config: {} };
}
const rail = (page: Page) => page.locator(".rail");
const openMonitoring = async (page: Page) => { await page.goto("/"); await page.locator(".sn", { hasText: "Monitoring" }).click(); };

test("scroll areas scroll but show no scrollbar (Monitoring list and metrics, Dashboard and Pipelines tables)", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 30; i++) state.pipelines.push(pipe(String(i), `pipe-${String(i).padStart(2, "0")}`));
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1512, height: 640 });
  await openMonitoring(page);
  await expect(rail(page).locator(".ri")).toHaveCount(30);
  await page.getByRole("button", { name: /show api response/i }).click();

  // On macOS Chromium draws overlay scrollbars that take no layout width, so measure what the page asks for instead:
  // the bar is hidden when `scrollbar-width` is none (Firefox) and ::-webkit-scrollbar is display:none (Chromium / Safari).
  const barWidth = (sel: string) => page.locator(sel).first().evaluate((e) => {
    const hidden = getComputedStyle(e).scrollbarWidth === "none" && getComputedStyle(e, "::-webkit-scrollbar").display === "none";
    return hidden ? 0 : 1;
  });
  expect(await page.locator(".rail-list").evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);   // it does scroll…
  expect(await barWidth(".rail-list")).toBe(0);                                                            // …without a bar
  expect(await page.locator("#connector-metrics").evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);
  expect(await barWidth("#connector-metrics")).toBe(0);

  await page.locator(".sn", { hasText: "Dashboard" }).click();
  await expect(page.getByRole("table", { name: "Deployed pipelines" })).toBeVisible();
  expect(await page.locator(".page-scroll").evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);
  expect(await scrollbarWidth(page, ".page-scroll")).toBe(0);
  expect(await barWidth('[aria-label="Deployed pipelines"] .tbody')).toBe(0);
  // The Connector status card on the Dashboard: more connectors than its 300px, so it scrolls — and draws no bar.
  expect(await page.locator(".conn-status-list").evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);
  expect(await barWidth(".conn-status-list")).toBe(0);

  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(page.locator(".tbl-fill .tbody")).toBeVisible();
  expect(await barWidth(".tbl-fill .tbody")).toBe(0);
});

/** 0 when the element draws no scrollbar (it can still scroll with the wheel, trackpad and keyboard). */
const scrollbarWidth = (page: import("@playwright/test").Page, sel: string) => page.locator(sel).first().evaluate((e) => {
  const hidden = getComputedStyle(e).scrollbarWidth === "none" && getComputedStyle(e, "::-webkit-scrollbar").display === "none";
  return hidden ? 0 : 1;
});

// Quality and Transforms work like Pipelines: the title and the stat cards stay put, the table's column header stays put, and the
// wheel over the rows scrolls the rows (on Quality, the sections below the policies table scroll too) — with no scrollbar drawn.
test("Quality and Transforms keep the title, cards and column header in place while the rows scroll", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 25; i++) {
    state.qualityPolicies.push({ id: `q-${i}`, org_id: "org_test", name: `policy-${i}`, rules: [{ type: "required", field: "id", name: "r" }], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
    state.transforms.push({ id: `t-${i}`, org_id: "org_test", name: `transform-${i}`, transforms: [] });
  }
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 560 });
  await page.goto("/");

  for (const [nav, rowText, lastRow] of [["Quality", "policy-1", "policy-25"], ["Transforms", "transform-1", "transform-25"]] as const) {
    await page.locator(".sn", { hasText: nav }).click();
    const row = page.locator(".tbody .tr", { hasText: rowText }).first();
    await expect(row).toBeVisible();
    const fixed = async () => ({
      title: (await page.locator(".ph").boundingBox())!.y,
      cards: (await page.locator(".stats").boundingBox())!.y,
      header: (await page.locator(".tbl .thead").first().boundingBox())!.y,
    });
    const before = await fixed();
    const body = page.locator(".tbl .tbody").first();
    expect(await body.evaluate((e) => e.scrollHeight > e.clientHeight), `${nav}: rows should scroll inside the table`).toBe(true);
    expect(await scrollbarWidth(page, ".tbl .tbody")).toBe(0);

    const box = (await row.boundingBox())!;
    await page.mouse.move(box.x + 200, box.y + box.height / 2);   // cursor right over a table row
    await page.mouse.wheel(0, 400);
    await expect.poll(() => body.evaluate((e) => e.scrollTop), { message: `${nav}: rows did not scroll` }).toBeGreaterThan(0);
    expect(await fixed(), `${nav}: title, cards and column header must not move`).toEqual(before);
    expect(await page.locator(".cont").evaluate((e) => e.scrollTop)).toBe(0);

    await body.evaluate((e) => { e.scrollTop = e.scrollHeight; });
    await expect(page.locator(".tbody .tr", { hasText: lastRow })).toBeVisible();   // the last row is reachable
  }
});

test("on Quality the results sections scroll into view while the title, cards and tabs stay put", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 25; i++) state.qualityPolicies.push({ id: `q-${i}`, org_id: "org_test", name: `policy-${i}`, rules: [], on_failure: "route_to_quarantine", quarantine_sink: "kafka" });
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 430 });                    // short: the History card cannot fit under the title, cards and tabs
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();
  await page.getByRole("tab", { name: "History" }).click();
  const cardsY = (await page.locator(".stats").boundingBox())!.y;
  const recent = page.locator(".card", { hasText: "Recent quality results" });
  await recent.scrollIntoViewIfNeeded();
  await expect(recent).toBeInViewport();
  expect((await page.locator(".stats").boundingBox())!.y).toBe(cardsY);
  expect(await page.locator(".page-scroll").evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  expect(await scrollbarWidth(page, ".page-scroll")).toBe(0);
});

// Quality's "Recent quality results" has one row per pipeline, so it can grow without bound. It fills the page down to the bottom and scrolls
// inside its card — the card's header stays put — with no scrollbar drawn.
test("Recent quality results fills the page, scrolls inside its card and draws no scrollbar", async ({ page }) => {
  const state = freshState();
  const stats = Array.from({ length: 20 }, (_, i) => ({
    id: `s${i + 1}`, org_id: "org_test", pipeline_id: `pipe-${i + 1}`, quality_policy_id: "qp-1", total_count: 10, failed_count: i % 3, passed_count: 10 - (i % 3),
    updated_at: new Date(Date.now() - 600_000).toISOString(), sample_failures: [],
  }));
  for (let i = 1; i <= 20; i++) state.pipelines.push({ id: `pipe-${i}`, org_id: "org_test", name: `pipeline-${String(i).padStart(2, "0")}`, status: "running", mode: "batch", generated_config: {}, revision: 1 });
  await mockBackend(page, state);
  await page.route("**/quality-stream-stats", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stats) }));
  await page.setViewportSize({ width: 1500, height: 1000 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();
  await page.getByRole("tab", { name: "History" }).click();

  const card = page.locator(".card", { hasText: "Recent quality results" });
  const body = card.locator(".cb");
  await card.scrollIntoViewIfNeeded();
  await expect(card.locator(".hrow")).toHaveCount(20);
  const m = await body.evaluate((e) => ({ h: e.clientHeight, sh: e.scrollHeight, bar: getComputedStyle(e).scrollbarWidth === "none" && getComputedStyle(e, "::-webkit-scrollbar").display === "none" }));
  const area = (await page.locator(".cont").boundingBox())!;
  const cardBox = (await card.boundingBox())!;
  expect(cardBox.y + cardBox.height).toBeGreaterThan(area.y + area.height - 40);   // the card reaches the bottom of the page
  expect(m.h).toBeGreaterThan(330);                  // taller than the old five-and-a-half-row cap
  expect(m.h).toBeLessThan(m.sh);                    // yet not all 20 rows at once
  expect(m.sh).toBeGreaterThan(m.h);                 // the rest scrolls inside
  expect(m.bar).toBe(true);                          // and no bar is drawn

  const headerBefore = (await card.locator(".ch").boundingBox())!.y;
  const first = card.locator(".hrow", { hasText: "pipeline-01" });
  const box = (await first.boundingBox())!;
  await page.mouse.move(box.x + 200, box.y + box.height / 2);
  await page.mouse.wheel(0, 300);
  await expect.poll(() => body.evaluate((e) => e.scrollTop), { message: "the rows did not scroll" }).toBeGreaterThan(0);
  expect((await card.locator(".ch").boundingBox())!.y).toBe(headerBefore);   // the card header does not move
  await body.evaluate((e) => { e.scrollTop = e.scrollHeight; });
  await expect(card.locator(".hrow", { hasText: "pipeline-20" })).toBeInViewport();   // the last pipeline is reachable
});

test("Recent quality results lists the most recently updated pipelines first", async ({ page }) => {
  const state = freshState();
  const at = (minutesAgo: string) => new Date(Date.now() - Number(minutesAgo) * 60_000).toISOString();
  const stats = [["old-one", "50"], ["newest", "2"], ["middle", "20"]].map(([name, day], i) => ({
    id: `s${i}`, org_id: "org_test", pipeline_id: name, quality_policy_id: "qp-1", total_count: 5, failed_count: 1, passed_count: 4, updated_at: at(day), sample_failures: [],
  }));
  for (const [name] of [["old-one"], ["newest"], ["middle"]]) state.pipelines.push({ id: name, org_id: "org_test", name, status: "running", mode: "batch", generated_config: {}, revision: 1 });
  await mockBackend(page, state);
  await page.route("**/quality-stream-stats", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stats) }));
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();
  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.locator("#quality-subpanel-history .hrow")).toHaveText([/newest/, /middle/, /old-one/]);
});

// Recent quality results has the same range filter as Validation: only the pipelines checked inside it are listed, and its failed records are
// those of that range. The choice is the one Validation uses (and remembers), so the two views always speak about the same window.
test("Recent quality results has the range filter: only pipelines checked in the range are listed, and it is Validation's range", async ({ page }) => {
  const state = freshState();
  const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
  const rows = [["just-now", 5], ["two-hours", 120], ["ten-hours", 600], ["three-days", 4320]] as const;
  const failure = (id: string, minutes: number) => ({
    original_topic: "t", pipeline_id: id, schema_id: null, quality_policy_id: "qp-1", failure_type: "quality_rule_failed", failed_rules: ["r"],
    received_at: ago(minutes), original_record: { id },
  });
  const stats = rows.map(([name, minutes], i) => ({
    id: `s${i}`, org_id: "org_test", pipeline_id: name, quality_policy_id: "qp-1", total_count: 9, failed_count: 2, passed_count: 7, updated_at: ago(minutes),
    sample_failures: [failure(name, minutes), failure(name, minutes + 400)],     // one inside the pipeline's last check, one much older
  }));
  for (const [name] of rows) state.pipelines.push({ id: name, org_id: "org_test", name, status: "running", mode: "batch", generated_config: {}, revision: 1 });
  await mockBackend(page, state);
  await page.route("**/quality-stream-stats", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stats) }));
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await page.getByRole("tab", { name: "Results" }).click();
  await page.getByRole("tab", { name: "History" }).click();
  const list = page.locator("#quality-subpanel-history .hrow");
  const range = page.locator("#quality-history-range");
  await choose(range, "15m");
  await expect(list).toHaveText([/just-now/]);
  await choose(range, "6h");
  await expect(list).toHaveText([/just-now/, /two-hours/]);
  await choose(range, "24h");
  await expect(list).toHaveText([/just-now/, /two-hours/, /ten-hours/]);     // the 3-day-old one is outside every range

  await choose(range, "6h");                                                 // the drawer holds only the range's failed records
  await list.filter({ hasText: "two-hours" }).click();
  await expect(page.locator(".drawer, .modal").last().locator(".click")).toHaveCount(1);   // of its two samples, the 2h-old one; the 8h-old one is outside 6h
});

// A hard scroll past the end of a list must not bounce the whole browser page (macOS shows a blank strip while it springs back): every layer
// the wheel can chain to — the lists, the page regions and the document itself — has overscroll-behavior none, where the page is one that scrolls.
test("no layer of the page can rubber-band: the document and every scroll region have overscroll-behavior none", async ({ page }) => {
  const state = freshState();
  await mockBackend(page, state);
  await page.goto("/");
  // The standalone page also sets this itself (index.html); take that away so what is checked is the rule DataConnect brings along, which is
  // what counts when a host loads it as a remote.
  await page.evaluate(() => { for (const el of Array.from(document.querySelectorAll("style"))) if (el.textContent?.includes("html,body,#root{height:100%")) el.remove(); });
  for (const nav of ["Pipelines", "Quality", "Connections", "Dashboard"]) {
    await page.locator(".sn", { hasText: nav }).click();
    const result = await page.evaluate(() => ({
      html: getComputedStyle(document.documentElement).overscrollBehaviorY, body: getComputedStyle(document.body).overscrollBehaviorY,
      cont: getComputedStyle(document.querySelector(".cont")!).overscrollBehaviorY, sidebar: getComputedStyle(document.querySelector(".sb")!).overscrollBehaviorY,
    }));
    expect(result, nav).toEqual({ html: "none", body: "none", cont: "none", sidebar: "none" });
  }
});
