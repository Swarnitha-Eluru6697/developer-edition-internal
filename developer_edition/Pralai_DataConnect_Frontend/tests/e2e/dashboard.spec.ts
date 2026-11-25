/*
 * Dashboard: real-time connector and pipeline health. State comes from Kafka Connect (/runtime/flow), rates and lag from
 * Prometheus (/runtime/connectors/metrics/summary), the failure counts from the quality and DLQ counters.
 *
 * FE-BUG-16: never a stale green — a state or number the page couldn't read is UNKNOWN or a dash, never RUNNING or 0.
 * FE-BUG-17: validation failures and ingestion failures are two separate counts, never one "Quarantine" number.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

function pipe(id: string, name: string, extra: Record<string, unknown> = {}) {
  return { id, org_id: "org_test", name, status: "running", topic_name: `t-${id}`, connector_name: `conn-${id}`, mode: "api", generated_config: {}, source_system: "postgres", entity: name, ...extra };
}
const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
const card = (page: Page, label: string) => page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: new RegExp(`^${label}$`) }) });
const deployedRow = (page: Page, name: string) => page.getByRole("table", { name: "Deployed pipelines" }).locator(".tr", { hasText: name });

const summary = (connectors: unknown[], overrides: Record<string, unknown> = {}) => ({
  prometheus_enabled: true, reachable: true, error: null, observed_at: new Date().toISOString(), connectors, ...overrides,
});

test("the connector cards count running, degraded and failed connectors and name the failed tasks", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("run", "running-pipe"), pipe("deg", "degraded-pipe"), pipe("bad", "failed-pipe", { status: "failed" }),
    pipe("paused", "paused-pipe", { status: "paused" }), pipe("draft", "draft-pipe", { status: "draft" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill(json({
    reachable: true, connect_url: "", bootstrap: "", message: "",
    connectors: [
      { name: "conn-run", pipeline_id: "run", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
      { name: "conn-deg", pipeline_id: "deg", type: "source", state: "RUNNING", tasks: ["RUNNING", "FAILED"], topics: [] },
      { name: "conn-bad", pipeline_id: "bad", type: "source", state: "FAILED", tasks: ["FAILED"], topics: [] },
      { name: "conn-paused", pipeline_id: "paused", type: "source", state: "PAUSED", tasks: ["PAUSED"], topics: [] },
    ],
    topics: [], sinks: [],
  })));

  await page.goto("/");

  // a draft was never deployed, so it is not one of the "deployed" connectors
  await expect(card(page, "Running connectors")).toContainText("2");
  await expect(card(page, "Running connectors")).toContainText("of 4 deployed");
  await expect(card(page, "Failed connectors")).toContainText("1");
  await expect(card(page, "Failed connectors")).toContainText("2 failed tasks");
  await expect(card(page, "Failed tasks")).toContainText("2");
  await expect(deployedRow(page, "draft-pipe")).toHaveCount(0);
  await expect(deployedRow(page, "degraded-pipe").locator(".badge").nth(1)).toContainText("DEGRADED");
  await expect(deployedRow(page, "paused-pipe").locator(".badge").nth(1)).toContainText("PAUSED");
});

test("rates, lag and per-pipeline read/written come from Prometheus and add up", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("src", "orders-cdc"), pipe("snk", "orders-sink", { direction: "SINK" }), pipe("snk2", "audit-sink", { direction: "SINK" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill(json(summary([
    { connector_name: "conn-src", pipeline_id: "src", pipeline_name: "orders-cdc", direction: "source", runtime_pool_id: "connect-cdc-pool", rate: 12.5, lag: null, tasks_running: 1, tasks_failed: 0 },
    { connector_name: "conn-snk", pipeline_id: "snk", pipeline_name: "orders-sink", direction: "sink", runtime_pool_id: "connect-sink-pool", rate: 40, lag: 9000, tasks_running: 1, tasks_failed: 0 },
    { connector_name: "conn-snk2", pipeline_id: "snk2", pipeline_name: "audit-sink", direction: "sink", runtime_pool_id: "connect-sink-pool", rate: 10, lag: 100, tasks_running: 1, tasks_failed: 0 },
  ]))));

  await page.goto("/");

  await expect(card(page, "Records read")).toContainText("12.5/s");
  await expect(card(page, "Records written")).toContainText("50/s");            // 40 + 10 across both sinks
  await expect(card(page, "Consumer lag")).toContainText("9,100");              // 9000 + 100
  await expect(deployedRow(page, "orders-cdc")).toContainText("12.5/s");
  await expect(deployedRow(page, "orders-cdc")).toContainText("read → Kafka");
  await expect(deployedRow(page, "orders-sink")).toContainText("40/s");
  await expect(deployedRow(page, "orders-sink")).toContainText("written");
  // only a lagging sink is flagged
  await expect(deployedRow(page, "orders-sink")).toContainText("lag 9,000");
  await expect(deployedRow(page, "audit-sink")).not.toContainText("lag");
});

test("without Prometheus the rates and lag are dashes with the reason — never 0", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("src", "orders-cdc"));
  await mockBackend(page, state);   // default mock: Prometheus not configured

  await page.goto("/");

  await expect(card(page, "Records read")).toContainText("—");
  await expect(card(page, "Records written")).toContainText("—");
  await expect(card(page, "Consumer lag")).toContainText("—");
  await expect(card(page, "Records read")).not.toContainText("0/s");
  await expect(deployedRow(page, "orders-cdc")).toContainText("—");
  await expect(page.locator(".alert-i")).toContainText("Prometheus metrics aren't configured");
  // connector state still comes from Kafka Connect
  await expect(card(page, "Running connectors")).toContainText("1");
});

test("Prometheus being unreachable shows a warning and dashes", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("src", "orders-cdc"));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill(json(summary(
    [{ connector_name: "conn-src", pipeline_id: "src", pipeline_name: "orders-cdc", direction: "source", runtime_pool_id: null, rate: null, lag: null, tasks_running: null, tasks_failed: null }],
    { reachable: false, error: "Prometheus could not be reached." },
  ))));

  await page.goto("/");

  await expect(page.locator(".alert-w")).toContainText("Connector metrics are currently unavailable");
  await expect(page.locator(".alert-w")).toContainText("Prometheus could not be reached.");
  await expect(card(page, "Records read")).toContainText("—");
});

test("Kafka Connect being unreachable shows UNKNOWN and dashes, never green (FE-BUG-16)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "some-pipe"));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill(json({
    reachable: false, connect_url: "", bootstrap: "", connectors: [], topics: [], sinks: [],
    message: "Kafka Connect not reachable at http://kafka-connect:8083: connection refused",
  })));

  await page.goto("/");

  await expect(page.locator(".alert-w").first()).toContainText("Connector state is currently unknown");
  await expect(page.locator(".alert-w").first()).toContainText("connection refused");
  await expect(card(page, "Running connectors")).toContainText("—");
  await expect(card(page, "Failed connectors")).toContainText("state unknown");
  await expect(page.locator(".card", { hasText: "Connector status" }).locator(".hrow", { hasText: "conn-1" })).toContainText("UNKNOWN");
});

test("the Connector status card never keeps showing RUNNING once polling fails — UNKNOWN with a last-observed time (FE-BUG-16)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "flaky-pipe"));
  await mockBackend(page, state);
  let calls = 0;
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => {
    calls += 1;
    if (calls === 1) {
      return route.fulfill(json({
        reachable: true, connect_url: "", bootstrap: "", message: "",
        connectors: [{ name: "conn-1", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }], topics: [], sinks: [],
      }));
    }
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "backend_unavailable", message: "boom", retryable: true }) });
  });

  await page.goto("/");
  const row = page.locator(".card", { hasText: "Connector status" }).locator(".hrow", { hasText: "conn-1" });
  await expect(row).toContainText("RUNNING");

  await expect(row).toContainText("UNKNOWN", { timeout: 9000 });
  await expect(row).toContainText("last observed");
  await expect(card(page, "Running connectors")).toContainText("—");
});

test("validation failures and ingestion failures render as two separate stat cards, never one Quarantine number (FE-BUG-17)", async ({ page }) => {
  await mockBackend(page);
  await page.route("**/api/v1/dataconnect/runtime/metrics/summary", (route) => route.fulfill(json({
    connector_running_count: 0, failed_connector_count: 0, failed_task_count: 0,
    records_read_per_second: 0, records_written_per_second: 0, error_rate: 0,
    validation_failure_count: 9, ingestion_failure_count: 4, consumer_lag: 0, extra: {},
  })));

  await page.goto("/");

  await expect(card(page, "Validation failures")).toContainText("9");
  await expect(card(page, "Ingestion failures")).toContainText("4");
  await expect(card(page, "Quarantine")).toHaveCount(0);
});

test("the Connect worker pools card shows each pool's workload, how many connectors run in it, and idle pools as neutral", async ({ page }) => {
  await mockBackend(page, freshState());
  const pool = (id: string, name: string, type: string, workload: string, status: string, count: number) => ({
    id, name, pool_type: type, workload, status, connector_count: count, shared_cluster: false,
    runtime_endpoint: `http://connect-${type}:8083`, observed_at: new Date().toISOString(),
  });
  await page.route("**/api/v1/dataconnect/runtime/connect-clusters", (route) => route.fulfill(json([
    pool("connect-cdc-pool", "CDC Worker Pool", "cdc", "Log-based change data capture (Debezium)", "idle", 0),
    pool("connect-jdbc-pool", "JDBC Worker Pool", "jdbc", "Polling and batch database sources", "healthy", 3),
    pool("connect-file-pool", "File Worker Pool", "file", "File and object-store sources", "degraded", 1),
    pool("connect-api-pool", "API Worker Pool", "api", "REST and webhook API sources", "idle", 0),
    pool("connect-stream-pool", "Stream Worker Pool", "stream", "Queue and message-stream sources", "idle", 0),
    pool("connect-sink-pool", "Sink Worker Pool", "sink", "Sink connectors", "unknown", 9),
  ])));

  await page.goto("/");

  const pools = page.locator(".card", { hasText: "Connect worker pools" });
  await expect(pools.locator(".hrow")).toHaveCount(6);
  await expect(pools).toContainText("Log-based change data capture (Debezium)");
  await expect(pools.locator(".hrow", { hasText: "JDBC Worker Pool" })).toContainText("3 connectors");
  await expect(pools.locator(".hrow", { hasText: "File Worker Pool" })).toContainText("1 connector");
  await expect(pools.locator(".hrow", { hasText: "File Worker Pool" })).not.toContainText("1 connectors");
  await expect(pools.locator(".hrow", { hasText: "CDC Worker Pool" })).not.toContainText("0 connector");
  // Only a healthy pool is green; an idle pool is a neutral badge, not a warning.
  await expect(pools.locator(".hrow", { hasText: "JDBC Worker Pool" }).locator(".badge")).toHaveClass(/\bbg\b/);
  await expect(pools.locator(".hrow", { hasText: "CDC Worker Pool" }).locator(".badge")).toHaveClass(/\bbgr\b/);
  await expect(pools.locator(".hrow", { hasText: "Sink Worker Pool" }).locator(".badge")).toHaveClass(/\bba\b/);
});

test("Metrics → opens Monitoring on exactly that connector", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"), pipe("b", "beta-pipe"));
  await mockBackend(page, state);
  const asked: string[] = [];
  await page.route("**/api/v1/dataconnect/runtime/connectors/*/metrics*", (route) => {
    asked.push(decodeURIComponent(new URL(route.request().url()).pathname.split("/").slice(-2)[0]!));
    return route.fallback();
  });

  await page.goto("/");
  await deployedRow(page, "beta-pipe").getByRole("button", { name: "Metrics →" }).click();

  await expect(page.locator(".sn.active")).toContainText("Monitoring");
  await expect(page.locator(".rail .ri.on")).toContainText("beta-pipe");
  await expect(page.locator("#connector-metrics .ct")).toContainText("beta-pipe");
  await expect.poll(() => asked).toContain("conn-b");
  expect(asked).not.toContain("conn-a");

  // the Connector status card's own link does the same for another connector
  await page.locator(".sn", { hasText: "Dashboard" }).click();
  await page.locator(".card", { hasText: "Connector status" }).getByRole("button", { name: "conn-a" }).click();
  await expect(page.locator(".rail .ri.on")).toContainText("alpha-pipe");
});

test("no 'connector state unknown' warning on the Dashboard while Kafka Connect's states are still loading (page refresh)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("p1", "slow-pipe"));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ reachable: true, connect_url: "", bootstrap: "", message: "",
        connectors: [{ name: "conn-p1", pipeline_id: "p1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] }], topics: [], sinks: [] }),
    });
  });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Dashboard" }).click();
  await page.reload();                                                         // the Dashboard is the remembered page
  await expect(deployedRow(page, "slow-pipe")).toBeVisible();                  // the list is there, states still loading
  const during = await deployedRow(page, "slow-pipe").innerText();             // one-shot: must not wait for it to change
  expect(during).not.toMatch(/UNKNOWN/i);                                      // a neutral "…", not a claim that Connect couldn't tell us
  expect(during).toContain("…");
  expect(await page.locator(".alert-w").count()).toBe(0);                      // …and no warning about them yet (one-shot: an auto-retrying check would wait for it to go away)
  await expect(deployedRow(page, "slow-pipe")).toContainText("RUNNING", { timeout: 5000 });
  expect(await page.locator(".alert-w").count()).toBe(0);
});

test("the two rows of four stat cards sit together at the top, above the pipelines table", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "orders-cdc"));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Dashboard" }).click();

  const labels = await page.locator(".sc .sc-l").allInnerTexts();
  expect(labels.map((l) => l.toLowerCase())).toEqual([
    "running connectors", "failed connectors", "records read", "records written",
    "failed tasks", "consumer lag", "validation failures", "ingestion failures",
  ]);
  const lastCard = (await page.locator(".sc").last().boundingBox())!;
  const table = (await page.getByRole("table", { name: "Deployed pipelines" }).boundingBox())!;
  expect(lastCard.y + lastCard.height).toBeLessThanOrEqual(table.y);       // every card is above the table
});

test("the Dashboard has no search box of its own: the table lists every pipeline, and searching is the top bar's", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("1", "orders-cdc"), pipe("2", "customers-jdbc"), pipe("3", "orders-sink", { direction: "SINK" }));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Dashboard" }).click();
  await expect(page.getByRole("table", { name: "Deployed pipelines" }).locator(".tbody .tr")).toHaveCount(3);
  await expect(page.getByRole("searchbox", { name: "Search pipelines" })).toHaveCount(0);       // the old "Search pipelines..." box is gone
  await expect(page.locator(".ph .srch")).toHaveCount(0);                                         // nothing in the page header either
  await expect(page.getByRole("combobox", { name: "Search DataConnect" })).toBeVisible();        // the one search lives in the top bar
});

test("a paused sink's backlog is counted in Consumer lag and shown on its row — the same number as the Pipelines page, before and after the states load", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(
    pipe("run", "busy-sink", { direction: "SINK" }),
    pipe("paused", "waiting-sink", { direction: "SINK", status: "paused" }),
  );
  await mockBackend(page, state);
  // Hold the connector states back for a moment, as on a real refresh, to catch the total moving when they arrive.
  let release!: () => void;
  const statesArrive = new Promise<void>((r) => { release = r; });
  await page.route("**/api/v1/dataconnect/runtime/flow", async (route) => {
    await statesArrive;
    await route.fulfill(json({
      reachable: true, connect_url: "", bootstrap: "", message: "", topics: [], sinks: [],
      connectors: [
        { name: "conn-run", pipeline_id: "run", type: "sink", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
        { name: "conn-paused", pipeline_id: "paused", type: "sink", state: "PAUSED", tasks: ["PAUSED"], topics: [] },
      ],
    }));
  });
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill(json(summary([
    { connector_name: "conn-run", pipeline_id: "run", pipeline_name: "busy-sink", direction: "sink", runtime_pool_id: "connect-sink-pool", rate: 3, lag: 0, tasks_running: 1, tasks_failed: 0 },
    { connector_name: "conn-paused", pipeline_id: "paused", pipeline_name: "waiting-sink", direction: "sink", runtime_pool_id: "connect-sink-pool", rate: 0, lag: 14080, tasks_running: 0, tasks_failed: 0 },
  ]))));

  await page.goto("/");
  await expect(card(page, "Consumer lag")).toContainText("14.1k");                   // before the states arrive …
  release();
  await expect(deployedRow(page, "waiting-sink")).toContainText("PAUSED");
  await expect(card(page, "Consumer lag")).toContainText("14.1k");                   // … and after: it no longer drops to 0
  await expect(deployedRow(page, "waiting-sink")).toContainText("lag 14.1k");         // the paused sink's row says why
  await expect(deployedRow(page, "busy-sink")).not.toContainText("lag");
});

// Like a Pipelines row, a clickable thing on the Dashboard only shows the pointer on hover: no underline, no colour change, no tooltip.
test("hovering Metrics → or a connector name shows only the pointer — no underline, no colour change, no tooltip", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("a", "alpha-pipe"));
  await mockBackend(page, state);
  await page.goto("/");
  const look = (loc: ReturnType<Page["locator"]>) => loc.evaluate((e) => { const c = getComputedStyle(e); return { decoration: c.textDecorationLine, color: c.color, cursor: c.cursor, title: e.getAttribute("title") }; });
  for (const target of [deployedRow(page, "alpha-pipe").getByRole("button", { name: "Metrics →" }), page.locator(".conn-link").first()]) {
    await expect(target).toBeVisible();
    const before = await look(target);
    await target.hover();
    await page.waitForTimeout(600);                                                 // longer than the tooltip's delay
    expect(await look(target)).toEqual(before);                                     // nothing about it changes on hover
    expect(before.decoration).toBe("none");
    expect(before.cursor).toBe("pointer");
    expect(before.title).toBeNull();
    await expect(page.locator(".fast-tip")).toHaveCount(0);                         // and no tooltip appears
  }
});
