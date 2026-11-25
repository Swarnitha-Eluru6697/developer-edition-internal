/*
 * Monitoring page: a searchable connector list (rail) next to the selected connector's Prometheus metrics.
 *
 * FE-BUG-16: the page must never substitute a previously-cached healthy status for UNKNOWN when runtime monitoring
 * itself fails or Kafka Connect can't be reached — and must render the full RUNNING/FAILED/DEGRADED/PAUSED/UNKNOWN
 * vocabulary, not a binary green/red.
 */
import { test, expect, type Locator, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const badge = (item: Locator) => item.locator(".badge");
const rail = (page: Page) => page.locator(".rail");
const item = (page: Page, name: string) => rail(page).locator(".ri", { hasText: name });

function pipe(id: string, name: string, extra: Record<string, unknown> = {}) {
  return { id, org_id: "org_test", name, status: "running", topic_name: `t-${id}`, connector_name: `conn-${id}`, mode: "api", generated_config: {}, ...extra };
}

async function openMonitoring(page: Page) {
  await page.goto("/");
  await page.locator(".sn", { hasText: "Monitoring" }).click();
}

test("the connector list renders RUNNING/PAUSED/DEGRADED/FAILED distinctly, not a binary green/red (FE-BUG-16)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("run", "running-pipe"), pipe("paused", "paused-pipe", { status: "paused" }),
    pipe("degraded", "degraded-pipe"), pipe("failed", "failed-pipe", { status: "failed" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      reachable: true, connect_url: "", bootstrap: "", message: "",
      connectors: [
        { name: "conn-run", pipeline_id: "run", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
        { name: "conn-paused", pipeline_id: "paused", type: "source", state: "PAUSED", tasks: ["PAUSED"], topics: [] },
        // Connector itself RUNNING but one task FAILED — DEGRADED, not a plain green RUNNING or a plain red FAILED.
        { name: "conn-degraded", pipeline_id: "degraded", type: "source", state: "RUNNING", tasks: ["RUNNING", "FAILED"], topics: [] },
        { name: "conn-failed", pipeline_id: "failed", type: "source", state: "FAILED", tasks: [], topics: [] },
      ],
      topics: [], sinks: [],
    }),
  }));

  await openMonitoring(page);

  await expect(badge(item(page, "running-pipe"))).toContainText("RUNNING");
  await expect(badge(item(page, "paused-pipe"))).toContainText("PAUSED");
  await expect(badge(item(page, "degraded-pipe"))).toContainText("DEGRADED");
  await expect(badge(item(page, "failed-pipe"))).toContainText("FAILED");
});

test("Kafka Connect being unreachable shows UNKNOWN for every connector, with the reason — never the last cached state (FE-BUG-16)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "some-pipe"));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      reachable: false, connect_url: "", bootstrap: "", connectors: [], topics: [], sinks: [],
      message: "Kafka Connect not reachable at http://kafka-connect:8083: connection refused",
    }),
  }));

  await openMonitoring(page);

  await expect(page.locator(".alert-w").first()).toContainText("Connector state is currently unknown");
  await expect(page.locator(".alert-w").first()).toContainText("connection refused");
  await expect(badge(item(page, "some-pipe"))).toContainText("UNKNOWN");
});

test("a connector that was RUNNING never keeps showing green once polling starts failing — it flips to UNKNOWN with a last-observed time (FE-BUG-16)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "flaky-pipe"));
  await mockBackend(page, state);

  let calls = 0;
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => {
    calls += 1;
    if (calls === 1) {
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          reachable: true, connect_url: "", bootstrap: "", message: "",
          connectors: [{ name: "conn-1", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }],
          topics: [], sinks: [],
        }),
      });
    }
    // Every poll after the first fails outright — RTK Query keeps the first successful response's `data` around;
    // the page must not trust it.
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "backend_unavailable", message: "boom", retryable: true }) });
  });

  await openMonitoring(page);
  await expect(badge(item(page, "flaky-pipe"))).toContainText("RUNNING");

  // Past the next 5s poll tick (MONITORING_POLL_MS), where the fetch itself now fails.
  await expect(badge(item(page, "flaky-pipe"))).toContainText("UNKNOWN", { timeout: 9000 });
  await expect(page.locator(".alert-w").first()).toContainText("Last observed");
});

test("the list shows each connector's current rate from Prometheus, a dash when there is no sample, and filters by name and type", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("src", "orders-cdc"), pipe("snk", "orders-sink", { direction: "SINK" }), pipe("new", "just-deployed"));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      prometheus_enabled: true, reachable: true, error: null, observed_at: new Date().toISOString(),
      connectors: [
        { connector_name: "conn-src", pipeline_id: "src", pipeline_name: "orders-cdc", direction: "source", runtime_pool_id: "connect-cdc-pool", rate: 12.5, lag: null, tasks_running: 1, tasks_failed: 0 },
        { connector_name: "conn-snk", pipeline_id: "snk", pipeline_name: "orders-sink", direction: "sink", runtime_pool_id: "connect-sink-pool", rate: 40, lag: 1200, tasks_running: 1, tasks_failed: 0 },
        // deployed a moment ago — Prometheus has no series yet
        { connector_name: "conn-new", pipeline_id: "new", pipeline_name: "just-deployed", direction: "source", runtime_pool_id: "connect-jdbc-pool", rate: null, lag: null, tasks_running: null, tasks_failed: null },
      ],
    }),
  }));

  await openMonitoring(page);

  await expect(item(page, "orders-cdc")).toContainText("12.5/s");
  await expect(item(page, "orders-sink")).toContainText("40/s");
  await expect(item(page, "just-deployed")).toContainText("—");
  await expect(item(page, "just-deployed")).not.toContainText("0/s");

  await rail(page).getByRole("tab", { name: "Sinks" }).click();
  await expect(rail(page).locator(".ri")).toHaveCount(1);
  await expect(item(page, "orders-sink")).toBeVisible();
  await rail(page).getByRole("tab", { name: "All" }).click();
  await rail(page).getByRole("searchbox").fill("cdc");
  await expect(rail(page).locator(".ri")).toHaveCount(1);
  await expect(item(page, "orders-cdc")).toBeVisible();
  await rail(page).getByRole("searchbox").fill("zzz");
  await expect(rail(page)).toContainText("No connectors match");
});

test("the connector the Dashboard or Pipelines opens is the one Monitoring shows, and the panel is titled with its pipeline", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"), pipe("b", "beta-pipe"));
  await mockBackend(page, state);

  await openMonitoring(page);
  await item(page, "beta-pipe").click();

  await expect(item(page, "beta-pipe")).toHaveClass(/\bon\b/);
  await expect(page.locator("#connector-metrics .ct")).toContainText("beta-pipe");
  await expect(page.locator("#connector-metrics")).toContainText("conn-b");
});

test("no 'connector state unknown' warning while Kafka Connect's states are still loading (page refresh)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "slow-pipe"));
  await mockBackend(page, state);
  await openMonitoring(page);
  await expect(badge(item(page, "slow-pipe"))).toContainText("RUNNING");

  // Now refresh the browser on this page, with Connect's states arriving late.
  await page.route("**/api/v1/dataconnect/runtime/flow", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ reachable: true, connect_url: "", bootstrap: "", message: "",
        connectors: [{ name: "conn-1", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }], topics: [], sinks: [] }),
    });
  });
  await page.reload();
  await expect(item(page, "slow-pipe")).toBeVisible();                       // the list is back, states still loading
  expect(await page.locator(".alert-w").count()).toBe(0);                     // …and there is no warning about them yet (one-shot: an auto-retrying check would wait for it to go away)
  await expect(badge(item(page, "slow-pipe"))).toContainText("RUNNING", { timeout: 5000 });
  expect(await page.locator(".alert-w").count()).toBe(0);
});

test("a refresh keeps the connector you were looking at, instead of jumping back to the first one", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"), pipe("b", "beta-pipe"), pipe("c", "gamma-pipe"));
  await mockBackend(page, state);
  await openMonitoring(page);
  await item(page, "gamma-pipe").click();
  await expect(item(page, "gamma-pipe")).toHaveClass(/\bon\b/);

  await page.reload();
  await expect(item(page, "gamma-pipe")).toHaveClass(/\bon\b/);
  await expect(page.locator("#connector-metrics .ct")).toContainText("gamma-pipe");
  await expect(item(page, "alpha-pipe")).not.toHaveClass(/\bon\b/);
});

test("a remembered connector that no longer exists falls back to the first running one", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"), pipe("b", "beta-pipe"));
  await mockBackend(page, state);
  await page.addInitScript(() => { localStorage.setItem("dc_selected_connector", "conn-deleted-long-ago"); });
  await openMonitoring(page);
  await expect(item(page, "alpha-pipe")).toHaveClass(/\bon\b/);
});

test("connector states read '…' (not UNKNOWN) while Kafka Connect's answer is still on its way after a refresh", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "slow-pipe"));
  await mockBackend(page, state);
  await openMonitoring(page);
  await page.route("**/api/v1/dataconnect/runtime/flow", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ reachable: true, connect_url: "", bootstrap: "", message: "",
        connectors: [{ name: "conn-1", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }], topics: [], sinks: [] }),
    });
  });
  await page.reload();
  await expect(item(page, "slow-pipe")).toBeVisible();
  const during = await badge(item(page, "slow-pipe")).innerText();                 // one-shot: must not wait for it to change
  expect(during).not.toMatch(/UNKNOWN/i);
  expect(during).toContain("…");
  await expect(badge(item(page, "slow-pipe"))).toContainText("RUNNING", { timeout: 5000 });
});

test("a paused connector shows a dash in the list, not the last rate Prometheus still remembers", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("run", "running-pipe"), pipe("paused", "paused-pipe", { status: "paused" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      prometheus_enabled: true, reachable: true, error: null, observed_at: new Date().toISOString(),
      connectors: [
        { connector_name: "conn-run", pipeline_id: "run", pipeline_name: "running-pipe", direction: "source", runtime_pool_id: "connect-cdc-pool", rate: 12.5, lag: null, tasks_running: 1, tasks_failed: 0 },
        { connector_name: "conn-paused", pipeline_id: "paused", pipeline_name: "paused-pipe", direction: "source", runtime_pool_id: "connect-cdc-pool", rate: 9.9, lag: null, tasks_running: 0, tasks_failed: 0 },
      ],
    }),
  }));
  await openMonitoring(page);
  await expect(item(page, "running-pipe")).toContainText("12.5/s");
  await expect(item(page, "paused-pipe")).toContainText("PAUSED");
  await expect(item(page, "paused-pipe")).not.toContainText("9.9/s");
  await expect(item(page, "paused-pipe")).toContainText("—");
});

test("the page never scrolls: the metrics card scrolls only when its own content is taller (e.g. the API response is open)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"));
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1512, height: 900 });
  await openMonitoring(page);
  await expect(page.locator("#connector-metrics .ct")).toContainText("alpha-pipe");
  await page.waitForTimeout(500);
  const pageScrolls = () => page.locator(".page-scroll").evaluate((e) => e.scrollHeight > e.clientHeight + 1);
  const cardScrolls = () => page.locator("#connector-metrics").evaluate((e) => e.scrollHeight > e.clientHeight + 1);
  expect(await pageScrolls()).toBe(false);
  expect(await cardScrolls()).toBe(false);                                        // everything fits: nothing scrolls

  await page.getByRole("button", { name: /show api response/i }).click();
  await page.setViewportSize({ width: 1512, height: 640 });
  await expect.poll(cardScrolls).toBe(true);                                      // the card is what scrolls…
  expect(await pageScrolls()).toBe(false);                                        // …never the page behind it
});

test("scrolling over the connector list never moves the metrics card, and the other way round", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 30; i++) state.pipelines.push(pipe(String(i), `pipe-${String(i).padStart(2, "0")}`));
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1512, height: 600 });
  await openMonitoring(page);
  await expect(rail(page).locator(".ri")).toHaveCount(30);
  await expect(page.locator("#connector-metrics .ct")).toBeVisible();

  const tops = () => page.evaluate(() => ({
    list: document.querySelector(".rail-list")!.scrollTop,
    card: document.querySelector("#connector-metrics")!.scrollTop,
    page: document.querySelector(".page-scroll")!.scrollTop,
  }));
  const r = (await rail(page).boundingBox())!;
  await page.mouse.move(r.x + 100, r.y + 200);
  await page.mouse.wheel(0, 500);
  await expect.poll(async () => (await tops()).list).toBeGreaterThan(0);          // the list scrolled…
  expect((await tops()).card).toBe(0);                                            // …the metrics did not
  expect((await tops()).page).toBe(0);

  await page.getByRole("button", { name: /show api response/i }).click();         // taller metrics, so that card overflows too
  await page.setViewportSize({ width: 1512, height: 640 });
  await expect.poll(() => page.locator("#connector-metrics").evaluate((e) => e.scrollHeight > e.clientHeight + 1)).toBe(true);
  const c = (await page.locator("#connector-metrics").boundingBox())!;
  await page.mouse.move(c.x + 300, c.y + 100);
  const listBefore = (await tops()).list;
  await page.mouse.wheel(0, 300);
  await expect.poll(async () => (await tops()).card).toBeGreaterThan(0);          // the metrics scrolled…
  expect((await tops()).list).toBe(listBefore);                                   // …the list stayed where it was
});

test("the connector list keeps its search and filter in place and scrolls only the rows", async ({ page }) => {
  const state = freshState();
  for (let i = 1; i <= 30; i++) state.pipelines.push(pipe(String(i), `pipe-${String(i).padStart(2, "0")}`));
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1512, height: 800 });
  await openMonitoring(page);
  await expect(rail(page).locator(".ri")).toHaveCount(30);

  const list = rail(page).locator(".rail-list");
  expect(await list.evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);          // the rows scroll…
  const searchTop = (await rail(page).getByRole("searchbox").boundingBox())!.y;
  await list.evaluate((e) => { e.scrollTop = e.scrollHeight; });
  expect((await rail(page).getByRole("searchbox").boundingBox())!.y).toBe(searchTop);      // …the search box does not move
  await expect(item(page, "pipe-30")).toBeVisible();
});

test("the header has no Live chip or Pause button, only a refresh icon that reloads the page at once, like the browser's own refresh", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "steady-pipe"), pipe("2", "second-pipe"));
  await mockBackend(page, state);
  await openMonitoring(page);
  await item(page, "second-pipe").click();
  await expect(item(page, "second-pipe")).toHaveClass(/\bon\b/);

  await expect(page.locator(".live")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toHaveCount(0);
  const refresh = page.getByRole("button", { name: "Refresh page" });
  await expect(refresh).toBeVisible();
  await expect(refresh).toHaveText("");                                            // an icon, no word

  await page.evaluate(() => { (window as unknown as { __marker: string }).__marker = "same page"; });
  const reloaded = page.waitForEvent("load");
  await refresh.click();
  await reloaded;                                                                  // the page really reloads, with no animation first
  expect(await page.evaluate(() => (window as unknown as { __marker?: string }).__marker)).toBeUndefined();   // a fresh document, not a refetch

  await expect(item(page, "second-pipe")).toHaveClass(/\bon\b/);                  // you land back on the same connector
  await expect(page.locator(".ph")).toContainText("Monitoring");                   // …on the same page
  await expect(page.locator(".toast")).toHaveCount(0);                             // and nothing says "Refreshed."
});

test("Monitoring keeps polling on its own, with nothing to turn it off", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "steady-pipe"));
  await mockBackend(page, state);
  let flowCalls = 0;
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => { flowCalls += 1; return route.fallback(); });
  await openMonitoring(page);
  await expect.poll(() => flowCalls).toBeGreaterThan(0);
  const first = flowCalls;
  await expect.poll(() => flowCalls, { timeout: 9000 }).toBeGreaterThan(first);
});
