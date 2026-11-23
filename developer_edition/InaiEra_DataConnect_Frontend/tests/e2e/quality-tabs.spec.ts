/*
 * The Quality page has two tabs under its title and cards: "Quality Checks" (the policy table, with its search and filter; "+ New Quality Check" is in the page header) and
 * "Results" (the failure breakdown for a chosen range, and the recent results).
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, choose, meFor } from "./fixtures";

const policy = (id: string, name: string, on_failure: string) => ({ id, org_id: "o", name, rules: [], on_failure, quarantine_sink: "kafka", created_at: "2026-10-20T10:00:00Z" });

async function openQuality(page: Page, role?: "Viewer") {
  const state = freshState();
  state.qualityPolicies.push(policy("q1", "orders-rules", "route_to_quarantine"), policy("q2", "audit-log-only", "log"), policy("q3", "customer-pii", "route_to_quarantine"));
  if (role) state.me = meFor(role);
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  return state;
}
const tab = (page: Page, name: string) => page.getByRole("tab", { name });
/** Results' two views: they sit on the right of the same row as the sections, and only while Results is open. */
const views = (page: Page) => page.getByRole("tablist", { name: "Results views" });

test("the page is titled Quality, keeps its four cards, and opens on Quality Checks", async ({ page }) => {
  await openQuality(page);
  await expect(page.locator(".ph .ptitle")).toHaveText("Quality");
  await expect(page.locator(".stats .sc")).toHaveCount(4);
  await expect(page.locator(".stats .sc-l").first()).toHaveText("Quality Checks");               // the first card is named the same
  await expect(page.getByRole("tab")).toHaveText(["Quality Checks", "Results"]);
  await expect(tab(page, "Quality Checks")).toHaveAttribute("aria-selected", "true");
  const panel = page.getByRole("tabpanel");
  await expect(panel).toContainText("Manage your data quality checks, define rules and configure validations.");
  await expect(panel.getByRole("columnheader")).toHaveText(["Quality Check Name", "Created at", "Rules", "On failure", "Actions"]);
  await expect(page.getByRole("button", { name: "+ New Quality Check" })).toHaveCount(1);          // one, in the page header — none in the card
  await expect(panel.getByRole("button", { name: "+ New Quality Check" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ New policy" })).toHaveCount(0);
  await expect(panel.getByText("Quality Checks", { exact: true })).toBeVisible();                  // the card's own heading
  await expect(panel.getByText("Policy details")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Validation failures" })).toHaveCount(0);         // the results are on the other tab
  await expect(views(page)).toHaveCount(0);                                                          // Quality Checks has no views: nothing on the right of its row
});

test("Results' views (Validation / History) sit on the right of the same row as Quality Checks and Results, and only under Results", async ({ page }) => {
  await openQuality(page);
  await expect(views(page)).toHaveCount(0);
  await tab(page, "Results").click();
  await expect(views(page)).toBeVisible();
  const box = async (loc: ReturnType<Page["locator"]>) => (await loc.boundingBox())!;
  const results = await box(tab(page, "Results")), validation = await box(tab(page, "Validation")), history = await box(tab(page, "History"));
  expect(Math.abs((validation.y + validation.height / 2) - (results.y + results.height / 2)), "one row").toBeLessThan(3);
  expect(validation.x, "to the right of the sections").toBeGreaterThan(results.x + results.width + 100);
  expect(history.x + history.width, "at the right edge of the page content").toBeGreaterThan(validation.x + validation.width);
  await tab(page, "Quality Checks").click();
  await expect(views(page)).toHaveCount(0);
});

test("Results has two views: Validation (the failure groups and the range) opens first, History (the recent results) is the other", async ({ page }) => {
  await openQuality(page);
  await tab(page, "Results").click();
  await expect(tab(page, "Results")).toHaveAttribute("aria-selected", "true");
  const results = page.locator("#quality-panel-results");
  await expect(views(page).getByRole("tab")).toHaveText(["Validation", "History"]);
  await expect(views(page).getByRole("tab", { name: "Validation" })).toHaveAttribute("aria-selected", "true");
  await expect(results).toContainText("View and analyze recent quality validation results.");
  await expect(results.getByRole("region", { name: "Validation failures" })).toBeVisible();
  await expect(results.getByRole("region", { name: "Ingestion failures" })).toBeVisible();
  await expect(results.locator("#quality-range")).toBeVisible();
  await expect(results.getByText("Recent quality results")).toHaveCount(0);                      // that is the History view
  await expect(results.getByRole("table")).toHaveCount(0);                                       // and no checks table here
  await expect(page.locator(".stats .sc")).toHaveCount(4);                                       // the cards stay above all the tabs

  await views(page).getByRole("tab", { name: "History" }).click();
  await expect(views(page).getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  await expect(results.getByText("Recent quality results")).toBeVisible();
  await expect(results.getByRole("region", { name: "Validation failures" })).toHaveCount(0);
  await expect(results.locator("#quality-range")).toHaveCount(0);                                // the range belongs to Validation

  await views(page).getByRole("tab", { name: "Validation" }).click();
  await expect(results.getByRole("region", { name: "Validation failures" })).toBeVisible();
  await tab(page, "Quality Checks").click();
  await expect(page.locator("#quality-panel-policies").getByRole("table")).toBeVisible();
});

test("the view inside Results survives a trip to Quality Checks and back, and the arrow keys move between its two tabs", async ({ page }) => {
  await openQuality(page);
  await tab(page, "Results").click();
  const results = page.locator("#quality-panel-results");
  await views(page).getByRole("tab", { name: "Validation" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(views(page).getByRole("tab", { name: "History" })).toBeFocused();
  await expect(results.getByText("Recent quality results")).toBeVisible();
  await page.keyboard.press("Home");
  await expect(views(page).getByRole("tab", { name: "Validation" })).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await tab(page, "Quality Checks").click();
  await tab(page, "Results").click();
  await expect(views(page).getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
});

test("the arrow keys, Home and End move between the tabs", async ({ page }) => {
  await openQuality(page);
  await tab(page, "Quality Checks").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Results")).toBeFocused();
  await expect(tab(page, "Results")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(tab(page, "Quality Checks")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tab(page, "Results")).toHaveAttribute("aria-selected", "true");
});

test("Quality Checks: search narrows the list by name; the filter narrows it to one on-failure action; both clear", async ({ page }) => {
  await openQuality(page);
  const rows = page.getByRole("tabpanel").locator(".tbody .tr");
  await expect(rows).toHaveCount(3);
  await page.getByLabel("Search quality checks").fill("pii");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("customer-pii");
  await page.getByLabel("Search quality checks").fill("zzz");
  await expect(page.getByText("No quality checks match")).toBeVisible();
  await page.getByLabel("Search quality checks").fill("");

  const filter = page.getByRole("button", { name: "Filter quality checks" });
  await filter.click();
  await expect(page.getByRole("radio")).toHaveCount(3);                                          // All + the two actions in use
  await page.getByRole("radio", { name: "log" }).click();                                    // a pick closes the panel
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("audit-log-only");
  await expect(filter).toHaveClass(/\bon\b/);                                                    // lit while a filter is on
  await filter.click();
  await page.getByRole("radio", { name: "All" }).click();
  await expect(rows).toHaveCount(3);
  await expect(filter).not.toHaveClass(/\bon\b/);
});

test("the range under Quality results asks the backend for exactly that window, and the default is the last hour", async ({ page }) => {
  const windows: (string | null)[] = [];
  page.on("request", (r) => { if (r.url().includes("/runtime/metrics/failures")) windows.push(new URL(r.url()).searchParams.get("window_seconds")); });
  await openQuality(page);
  await tab(page, "Results").click();
  await expect.poll(() => windows).toContain("3600");
  await choose(page.locator("#quality-range"), "6h");
  await expect.poll(() => windows).toContain("21600");
  await choose(page.locator("#quality-range"), "24h");
  await expect.poll(() => windows).toContain("86400");
  await choose(page.locator("#quality-range"), "15m");
  await expect.poll(() => windows).toContain("900");
  await expect(page.locator("#quality-range")).toContainText("Last 15m");
  await tab(page, "Quality Checks").click();                                                      // the choice survives a trip to the other tab
  await tab(page, "Results").click();
  await expect(page.locator("#quality-range")).toContainText("Last 15m");
});

test("a Viewer sees both tabs but no + New Quality Check", async ({ page }) => {
  await openQuality(page, "Viewer");
  await expect(page.getByRole("tab")).toHaveText(["Quality Checks", "Results"]);
  await expect(page.getByRole("button", { name: "+ New Quality Check" })).toHaveCount(0);
  await expect(page.getByRole("tabpanel").locator(".tbody .tr")).toHaveCount(3);
});

test("a policy picked in the top-bar search opens even if the results tab was showing", async ({ page }) => {
  await openQuality(page);
  await tab(page, "Results").click();
  await page.getByRole("combobox", { name: "Search DataConnect" }).fill("orders-rules");
  await page.locator(".hs-opt").first().click();
  await expect(tab(page, "Quality Checks")).toHaveAttribute("aria-selected", "true");
});

// ── Validation → click a quality check → the failed records of every pipeline that runs it ──────────────────────────────────

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const failure = (pipeline: string, topic: string, rules: string[], at: string, record: Record<string, unknown>) => ({
  original_topic: topic, original_partition: 0, original_offset: 1, pipeline_id: pipeline, schema_id: null, quality_policy_id: "qp-pii", failure_type: "quality_rule_failed",
  failed_rules: rules, received_at: at, original_record: record,
});

async function sharedCheck(page: Page) {
  const state = freshState();
  state.qualityPolicies.push({
    id: "qp-pii", org_id: "o", name: "customer-pii-check", on_failure: "log", quarantine_sink: "kafka",
    rules: [{ name: "email_format", type: "regex", field: "email", pattern: "^.+@.+$" }, { name: "name_required", type: "required", field: "name" }],
  });
  for (const [id, name] of [["pa", "orders-a"], ["pb", "orders-b"]] as const) {
    state.pipelines.push({ id, org_id: "o", name, status: "running", topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "batch", generated_config: {}, quality_policy_id: "qp-pii" });
  }
  await mockBackend(page, state);
  await page.route("**/runtime/metrics/failures*", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      window_seconds: 3600, ingestion: { total: 0, classified: 0, unclassified: 0, by_category: [], recent: [] },
      validation: { total: 3, by_rule: [], by_check: [{ quality_policy_id: "qp-pii", name: "customer-pii-check", count: 3, pipelines: ["orders-a", "orders-b"] }] },
    }),
  }));
  await page.route("**/quality-stream-stats", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify([
      { id: "s1", org_id: "o", pipeline_id: "pa", quality_policy_id: "qp-pii", total_count: 10, failed_count: 2, passed_count: 8, sample_failures: [
        failure("pa", "topic-a", ["email_format", "name_required"], minutesAgo(30), { name: null, email: "nope" }),
        failure("pa", "topic-a", ["email_format"], minutesAgo(5), { name: "ann", email: "bad" }),
        failure("pa", "topic-a", ["email_format"], minutesAgo(60 * 30), { name: "old", email: "old" }),                 // 30 hours ago: outside every range
      ] },
      { id: "s2", org_id: "o", pipeline_id: "pb", quality_policy_id: "qp-pii", total_count: 5, failed_count: 1, passed_count: 4, sample_failures: [
        failure("pb", "topic-b", ["name_required"], minutesAgo(12), { name: "", email: "b@b.com" }),
      ] },
    ]),
  }));
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await tab(page, "Results").click();
}

test("a quality check row opens the failed records of every pipeline that runs it, each labelled with its pipeline, newest first", async ({ page }) => {
  await sharedCheck(page);
  const row = page.getByRole("region", { name: "Validation failures" }).getByRole("button", { name: /customer-pii-check/ });
  await expect(row).toHaveAttribute("title", "View failed records");
  await row.click();
  const drawer = page.locator(".modal");
  await expect(drawer.locator("#modal-title")).toHaveText("Failed records — customer-pii-check");
  await expect(drawer).toContainText("from the 2 pipelines that run this quality check (orders-a, orders-b)");   // which pipelines the records come from
  const cards = drawer.locator(".click");
  await expect(cards).toHaveCount(3);                                                               // 2 from orders-a + 1 from orders-b; the 30-hour-old one is outside "Last 1h"
  await expect(cards.nth(0)).toContainText("orders-a");                                             // 5 min ago
  await expect(cards.nth(1)).toContainText("orders-b");                                             // 12 min ago
  await expect(cards.nth(2)).toContainText("orders-a");                                             // 30 min ago
  await expect(cards.nth(2)).toContainText("2 rules broken");
  await cards.nth(1).click();                                                                       // open one: what each rule expected vs got, using the CHECK's rules
  await expect(drawer).toContainText("name — ");
  await expect(drawer).toContainText("actual:");
  await expect(drawer.getByText("old", { exact: true })).toHaveCount(0);
});

test("the records follow the chosen range: a wider range brings in older ones, and a row with nothing to show is not a button", async ({ page }) => {
  await sharedCheck(page);
  await page.locator("#quality-range").click();
  await page.locator(".ss-menu [role=option]", { hasText: "Last 15m" }).click();
  const row = page.getByRole("region", { name: "Validation failures" }).getByRole("button", { name: /customer-pii-check/ });
  await row.click();
  await expect(page.locator(".modal .click")).toHaveCount(2);                                      // the ones from 5 and 12 minutes ago, not the one from 30
  await page.keyboard.press("Escape");
  await page.locator("#quality-range").click();
  await page.locator(".ss-menu [role=option]", { hasText: "Last 24h" }).click();
  await row.click();
  await expect(page.locator(".modal .click")).toHaveCount(3);                                      // 30 hours ago is still outside a day
});

test("the row opens from the keyboard too, and a check with no failed records on hand is plain text", async ({ page }) => {
  await sharedCheck(page);
  const row = page.getByRole("region", { name: "Validation failures" }).getByRole("button", { name: /customer-pii-check/ });
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".modal")).toContainText("Failed records — customer-pii-check");
  await page.keyboard.press("Escape");
  // the same check, but the worker holds no samples (they rolled off): nothing to open
  await page.route("**/quality-stream-stats", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.reload();
  await page.locator(".sn", { hasText: "Quality" }).click();
  await tab(page, "Results").click();
  const plain = page.getByRole("region", { name: "Validation failures" }).locator(".hrow", { hasText: "customer-pii-check" });
  await expect(plain).toBeVisible();
  await expect(plain).not.toHaveAttribute("role", "button");
});

// ── A refresh keeps you where you were ──────────────────────────────────────────────────────────────────────────────────────

test("refreshing the page keeps the open tab, the view inside Results and the range", async ({ page }) => {
  await openQuality(page);
  await tab(page, "Results").click();
  await views(page).getByRole("tab", { name: "History" }).click();
  await page.reload();
  await expect(page.locator(".ph .ptitle")).toHaveText("Quality");
  await expect(tab(page, "Results")).toHaveAttribute("aria-selected", "true");                       // not thrown back to Quality Checks
  await expect(views(page).getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("Recent quality results")).toBeVisible();

  await views(page).getByRole("tab", { name: "Validation" }).click();
  await choose(page.locator("#quality-range"), "6h");
  await page.reload();
  await expect(views(page).getByRole("tab", { name: "Validation" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#quality-range")).toContainText("Last 6h");

  await tab(page, "Quality Checks").click();
  await page.reload();
  await expect(tab(page, "Quality Checks")).toHaveAttribute("aria-selected", "true");                // and the first tab is remembered too
});

test("a stored choice that is not one of the real tabs is ignored: the page opens on Quality Checks", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("dc_quality_tab", "nonsense");
    localStorage.setItem("dc_quality_view", "<script>");
    localStorage.setItem("dc_quality_range", "99y");
  });
  await openQuality(page);
  await expect(tab(page, "Quality Checks")).toHaveAttribute("aria-selected", "true");
  await tab(page, "Results").click();
  await expect(views(page).getByRole("tab", { name: "Validation" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#quality-range")).toContainText("Last 1h");
});

test("the Pipelines status tab survives a refresh too", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({ id: "p1", org_id: "o", name: "running-one", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {} },
    { id: "p2", org_id: "o", name: "paused-one", status: "paused", topic_name: "t2", connector_name: "c2", mode: "batch", generated_config: {} });
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("tab", { name: /^Paused/ }).click();
  await page.reload();
  await expect(page.getByRole("tab", { name: /^Paused/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".tbody .tr")).toHaveCount(1);
  await expect(page.locator(".tbody .tr").first()).toContainText("paused-one");
});

// ── Validation fills the page; each panel scrolls its own list, with no scrollbar ────────────────────────────────────────────

async function manyFailures(page: Page, checks: number, errors: number) {
  const state = freshState();
  await mockBackend(page, state);
  await page.route("**/runtime/metrics/failures*", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      window_seconds: 3600,
      validation: {
        total: checks * 2, by_rule: [],
        by_check: Array.from({ length: checks }, (_, i) => ({ quality_policy_id: `q${i}`, name: `quality-check-${String(i + 1).padStart(2, "0")}`, count: 2, pipelines: [`pipe-${i}`] })),
      },
      ingestion: {
        total: errors, classified: errors, unclassified: 0, by_category: [{ category: "connector", label: "Connector errors", count: errors }],
        recent: Array.from({ length: errors }, (_, i) => ({ pipeline_name: `sink-${i}`, category: "connector", stage: "TASK_PUT", exception_class: "java.sql.SQLException", message: `bad row ${i}`, at: new Date().toISOString() })),
      },
    }),
  }));
  await page.setViewportSize({ width: 1300, height: 760 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Quality" }).click();
  await tab(page, "Results").click();
}

test("Validation fills the page down to the bottom, and the page itself does not scroll", async ({ page }) => {
  await manyFailures(page, 3, 1);
  const card = (await page.locator("#quality-subpanel-validation").boundingBox())!;
  const area = (await page.locator(".cont").boundingBox())!;
  expect(card.y + card.height).toBeGreaterThan(area.y + area.height - 40);                          // the card reaches the bottom of the page, however few rows there are
  expect(await page.locator(".page-scroll").evaluate((e) => e.scrollHeight - e.clientHeight)).toBeLessThanOrEqual(1);   // so nothing scrolls except the lists
  const panels = await page.locator(".qpanel").evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  expect(new Set(panels).size).toBe(1);                                                              // the two panels are as tall as each other
});

test("with many failures each panel scrolls its own list with no scrollbar, and scrolling one never moves the other", async ({ page }) => {
  await manyFailures(page, 40, 25);
  await page.getByText("Recent ingestion errors (25)").click();                                     // open the ingestion detail: a long list too
  const validation = page.locator("[aria-label='Validation failures'] .qpanel-body");
  const ingestion = page.locator("[aria-label='Ingestion failures'] .qpanel-body");
  for (const body of [validation, ingestion]) {
    const m = await body.evaluate((e) => ({ scrolls: e.scrollHeight > e.clientHeight + 20, bar: e.offsetWidth - e.clientWidth, css: getComputedStyle(e).scrollbarWidth, overscroll: getComputedStyle(e).overscrollBehaviorY }));
    expect(m).toEqual({ scrolls: true, bar: 0, css: "none", overscroll: "none" });                   // it scrolls inside itself, no bar, and neither bounces nor chains to the page
  }
  await validation.hover();
  await page.mouse.wheel(0, 400);
  await expect.poll(() => validation.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  expect(await ingestion.evaluate((e) => e.scrollTop)).toBe(0);                                      // the other panel did not move
  expect(await page.locator(".page-scroll").evaluate((e) => e.scrollTop)).toBe(0);                   // nor did the page
  await ingestion.hover();
  await page.mouse.wheel(0, 300);
  await expect.poll(() => ingestion.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
  const before = await validation.evaluate((e) => e.scrollTop);
  expect(before).toBeGreaterThan(0);
  await validation.evaluate((e) => { e.scrollTop = 0; });
  expect(await ingestion.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);                           // and the other way round
});

test("a panel's title and description stay put while its list scrolls", async ({ page }) => {
  await manyFailures(page, 40, 1);
  const title = page.locator("[aria-label='Validation failures'] .fsec");
  const y = (await title.boundingBox())!.y;
  await page.locator("[aria-label='Validation failures'] .qpanel-body").evaluate((e) => { e.scrollTop = 500; });
  expect((await title.boundingBox())!.y).toBe(y);
  await expect(page.getByText("quality-check-40")).toBeVisible();                                    // the last of the 40 is reachable
});

test("forcing the wheel up, down, left and right moves nothing but each list's own vertical scroll", async ({ page }) => {
  await manyFailures(page, 40, 25);
  await page.getByText("Recent ingestion errors (25)").click();
  const offsets = () => page.evaluate(() => {
    const at = (sel: string) => { const e = document.querySelector(sel) as HTMLElement | null; return e ? [e.scrollLeft, e.scrollTop] : null; };
    return { doc: [document.documentElement.scrollLeft, document.documentElement.scrollTop], cont: at(".cont"), page: at(".page-scroll"), card: at("#quality-subpanel-validation"), sb: at(".sb") };
  });
  const still = await offsets();
  for (const body of ["Validation failures", "Ingestion failures"]) {
    const list = page.locator(`[aria-label='${body}'] .qpanel-body`);
    await list.hover();
    for (const [dx, dy] of [[0, 3000], [0, -3000], [400, 0], [-400, 0], [500, 500], [-500, -500]]) await page.mouse.wheel(dx, dy);
    await page.waitForTimeout(150);
    expect(await list.evaluate((e) => e.scrollLeft)).toBe(0);                                        // never sideways
    expect(await list.evaluate((e) => getComputedStyle(e).overflowX)).toBe("hidden");
  }
  expect(await offsets()).toEqual(still);                                                            // the page, the card and the sidebar did not move at all, not even past the list's end
});
