/*
 * Monitoring → Connector metrics: pick one of this org's connectors (from the connector list on the left) and see its
 * Prometheus-sourced Kafka Connect metrics (GET /runtime/connectors/{name}/metrics): tiles, enlargeable charts, the
 * per-task table, the real failure trace and the API response.
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { freshState, mockBackend, rowAction } from "./fixtures";

const NOW = Math.floor(Date.now() / 1000);
const series = (values: (number | null)[]) => values.map((v, i) => ({ t: NOW - (values.length - 1 - i) * 60, v }));

function metricsBody(name: string, range: string, overrides: Record<string, unknown> = {}) {
  const sink = name === "conn-sink";
  return {
    connector_name: name, pipeline_id: sink ? "p-sink" : "p-src", pipeline_name: sink ? "orders-sink" : "orders-cdc",
    direction: sink ? "sink" : "source", runtime_pool_id: sink ? "connect-sink-pool" : "connect-cdc-pool",
    range, step_seconds: 60, observed_at: new Date().toISOString(), prometheus_enabled: true, reachable: true, error: null,
    metrics: sink
      ? [
        { id: "sink_read_rate", label: "Records read from Kafka", unit: "records/s", description: "", current: 40, series: series([30, 35, 40]) },
        { id: "sink_consumer_lag", label: "Consumer lag", unit: "count", description: "", current: 1200, series: series([900, 1100, 1200]) },
      ]
      : [
        { id: "source_poll_rate", label: "Records polled", unit: "records/s", description: "", current: 12.5, series: series([10, 11, null, 12.5]) },
        { id: "running_ratio", label: "Running ratio", unit: "ratio", description: "", current: 1, series: series([1, 1, 1]) },
        { id: "source_write_rate", label: "Records written to Kafka", unit: "records/s", description: "", current: null, series: [] },
      ],
    task_columns: [{ id: sink ? "sink_read_rate" : "source_poll_rate", label: "Rate", unit: "records/s" }],
    tasks: [
      { task: "0", status: "running", worker: "connect-cdc-pool", values: { [sink ? "sink_read_rate" : "source_poll_rate"]: 7 } },
      { task: "1", status: "failed", worker: "connect-cdc-pool", values: {} },
    ],
    ...overrides,
  };
}

async function setup(page: Page, handler?: (route: Route, name: string, range: string) => unknown) {
  const state = freshState();
  state.pipelines.push(
    { id: "p-src", org_id: "org_test", name: "orders-cdc", status: "running", topic_name: "t1", connector_name: "conn-src", mode: "api", generated_config: {} },
    { id: "p-sink", org_id: "org_test", name: "orders-sink", status: "running", topic_name: "t2", connector_name: "conn-sink", mode: "api", generated_config: {}, connector_config: { topics: "t1" } },
  );
  await mockBackend(page, state);
  const calls: { name: string; range: string }[] = [];
  await page.route("**/api/v1/dataconnect/runtime/connectors/*/metrics*", (route) => {
    const url = new URL(route.request().url());
    const name = decodeURIComponent(url.pathname.split("/").slice(-2)[0]!);
    const range = url.searchParams.get("range") || "1h";
    calls.push({ name, range });
    if (handler) return handler(route, name, range);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(metricsBody(name, range)) });
  });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Monitoring" }).click();
  return calls;
}

const panel = (page: Page) => page.locator("#connector-metrics");

test("the first deployed connector is selected and its metrics, chart and per-task table render", async ({ page }) => {
  await setup(page);

  await expect(page.locator(".rail .ri.on")).toContainText("orders-cdc");
  await expect(panel(page).locator(".ct")).toContainText("orders-cdc");
  const poll = panel(page).locator('[data-metric="source_poll_rate"]');
  await expect(poll).toContainText("Records polled");
  await expect(poll).toContainText("12.5/s");
  await expect(poll.locator("svg[role=img]")).toHaveAttribute("aria-label", /latest 12.5\/s/);
  await expect(panel(page).locator('[data-metric="running_ratio"]')).toContainText("100%");
  // No samples -> a dash and an empty chart, never 0.
  const write = panel(page).locator('[data-metric="source_write_rate"]');
  await expect(write).toContainText("—");
  await expect(write).toContainText("No samples in this range");

  const tasks = panel(page).getByRole("table", { name: "Per-task metrics" });
  await expect(tasks.locator(".tr", { hasText: "Task 1" })).toContainText("RUNNING");
  await expect(tasks.locator(".tr", { hasText: "Task 2" })).toContainText("FAILED");
});

test("choosing another connector and range re-queries for exactly that connector", async ({ page }) => {
  const calls = await setup(page);

  await page.locator(".rail .ri", { hasText: "orders-sink" }).click();
  await expect(panel(page).locator('[data-metric="sink_consumer_lag"]')).toContainText("1,200");
  await expect(panel(page)).toContainText("sink");

  await panel(page).getByRole("tab", { name: "6h" }).click();
  await expect(panel(page).getByRole("tab", { name: "6h" })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => calls.some((c) => c.name === "conn-sink" && c.range === "6h")).toBe(true);
});

test("clicking a connector in the list selects it for metrics", async ({ page }) => {
  await setup(page);

  await page.locator(".rail .ri", { hasText: "orders-sink" }).click();
  await expect(page.locator(".rail .ri.on")).toContainText("orders-sink");
  await expect(panel(page).locator('[data-metric="sink_read_rate"]')).toContainText("40/s");
});

test("throughput and lag tiles are double width, and clicking a tile enlarges its chart with an exact value on hover", async ({ page }) => {
  await setup(page);

  const poll = panel(page).locator('[data-metric="source_poll_rate"]');
  const ratio = panel(page).locator('[data-metric="running_ratio"]');
  await expect(poll).toHaveClass(/\bwide\b/);
  await expect(ratio).not.toHaveClass(/\bwide\b/);
  await expect(poll).toHaveAttribute("aria-expanded", "false");

  await poll.click();
  await expect(poll).toHaveAttribute("aria-expanded", "true");
  await expect(poll).toHaveClass(/\bbig\b/);
  await expect(poll.locator(".mt-ylab").first()).toBeVisible();      // the axis grid appears when enlarged

  await poll.locator(".mt-spark").hover({ position: { x: 5, y: 60 } });
  await expect(poll.locator(".mt-tip")).toBeVisible();
  await expect(poll.locator(".mt-tip strong")).toContainText("/s");

  await poll.click();                                                 // click again to collapse
  await expect(poll).toHaveAttribute("aria-expanded", "false");
  await expect(poll.locator(".mt-ylab")).toHaveCount(0);
});

test("a failed task shows its real Kafka Connect trace, and Restart task asks for exactly that pipeline to restart", async ({ page }) => {
  const calls = await setup(page);
  void calls;
  await page.route("**/api/v1/dataconnect/runtime/connectors/conn-src/status", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      name: "conn-src", pipeline_id: "p-src", connector: { state: "RUNNING" },
      tasks: [{ id: 0, state: "RUNNING" }, { id: 1, state: "FAILED", trace: "org.apache.kafka.connect.errors.ConnectException: Source connection reset\n\tat io.debezium.Foo.bar(Foo.java:42)" }],
      observed_state: "degraded", observed_at: new Date().toISOString(), runtime_endpoint: null, error: null,
    }),
  }));
  const restarts: string[] = [];
  await page.route("**/api/v1/dataconnect/pipelines/p-src/restart", (route) => { restarts.push(route.request().method()); return route.fallback(); });
  await page.reload();
  await page.locator(".sn", { hasText: "Monitoring" }).click();

  const failure = panel(page).locator(".alert-w", { hasText: "Task 2 failed" });
  await expect(failure).toContainText("ConnectException: Source connection reset");
  await expect(failure).not.toContainText("io.debezium.Foo.bar");     // only the first line of the trace

  const tasks = panel(page).getByRole("table", { name: "Per-task metrics" });
  await expect(tasks.locator(".tr", { hasText: "Task 1" }).getByRole("button", { name: "Restart task" })).toHaveCount(0);
  await tasks.locator(".tr", { hasText: "Task 2" }).getByRole("button", { name: "Restart task" }).click();
  await expect.poll(() => restarts).toEqual(["POST"]);
  await expect(page.locator(".toast").last()).toContainText("(task 2)");   // Connect's task 1 is shown, and toasted, as task 2
  await expect(page.locator(".toast")).toContainText("Restart requested");
});

test("Show API response reveals the real response, abridged, and hides it again", async ({ page }) => {
  await setup(page);

  const toggle = panel(page).getByRole("button", { name: /API response/ });
  await expect(panel(page).locator("pre.json")).toHaveCount(0);
  await toggle.click();
  const json = panel(page).locator("pre.json");
  await expect(json).toContainText("GET /api/v1/dataconnect/runtime/connectors/conn-src/metrics?range=1h");
  await expect(json).toContainText('"connector_name": "conn-src"');
  await expect(json).toContainText('"prometheus_enabled": true');
  await expect(json).toContainText("more points");                    // long series are abridged, not dumped
  await toggle.click();
  await expect(panel(page).locator("pre.json")).toHaveCount(0);
});

test("Prometheus being unreachable shows the reason and dashes, never zeros", async ({ page }) => {
  await setup(page, (route, name, range) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify(metricsBody(name, range, {
      reachable: false, error: "Prometheus could not be reached.",
      metrics: [{ id: "source_poll_rate", label: "Records polled", unit: "records/s", description: "", current: null, series: [] }],
      tasks: [],
    })),
  }));

  await expect(panel(page).locator(".alert-w")).toContainText("Connector metrics are currently unavailable");
  await expect(panel(page).locator(".alert-w")).toContainText("Prometheus could not be reached.");
  await expect(panel(page).locator('[data-metric="source_poll_rate"]')).toContainText("—");
});

test("without Prometheus configured the panel says so", async ({ page }) => {
  await setup(page, (route, name, range) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify(metricsBody(name, range, { prometheus_enabled: false, reachable: false, metrics: [], tasks: [] })),
  }));

  await expect(panel(page)).toContainText("Prometheus metrics aren't configured");
});

test("hovering a small tile shows the sample's time on its own line without squeezing the label or the value", async ({ page }) => {
  await setup(page);
  const tile = panel(page).locator('[data-metric="running_ratio"]');
  const labelBefore = await tile.locator(".mt-lt").evaluate((e) => e.getBoundingClientRect().width);
  const valueBox = await tile.locator(".mt-v").boundingBox();

  await tile.locator(".mt-spark").hover({ position: { x: 20, y: 20 } });
  const when = tile.locator(".mt-when");
  await expect(when).toBeVisible();
  await expect(when).toHaveText(/\d{1,2}:\d{2}/);                         // a real time, not truncated to "15:…"

  const labelDuring = await tile.locator(".mt-lt").evaluate((e) => e.getBoundingClientRect().width);
  expect(labelDuring).toBe(labelBefore);                                  // hovering never narrows the label
  const whenBox = (await when.boundingBox())!, valueNow = (await tile.locator(".mt-v").boundingBox())!;
  expect(whenBox.y).toBeGreaterThanOrEqual(valueNow.y + valueNow.height - 1);   // it sits below the value, not beside it
  expect(valueNow.width).toBeGreaterThanOrEqual(valueBox!.width - 1);           // …and the value keeps its width
  expect(await when.evaluate((e) => e.scrollWidth <= e.clientWidth)).toBe(true);
});

test("the task table's Worker pool names the connector's pool, not Prometheus' 'shared' scrape label", async ({ page }) => {
  await setup(page, (route, name, range) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify(metricsBody(name, range, { tasks: [
      { task: "0", status: "running", worker: "shared", values: {} },          // a local stack: every connector on one worker
      { task: "1", status: "running", worker: "connect-jdbc-pool", values: {} },
    ] })),
  }));
  const rows = panel(page).locator('[aria-label="Per-task metrics"] .tbody .tr');
  await expect(rows.nth(0)).toContainText("connect-cdc-pool (shared worker)");   // the connector's own pool, on the one shared worker
  await expect(rows.nth(0)).not.toHaveText(/^\s*Task 1\s*running\s*shared\s/i);
  await expect(rows.nth(1)).toContainText("connect-jdbc-pool");                   // a real worker label stays as it is…
  await expect(rows.nth(1)).not.toContainText("shared worker");                   // …without the shared-worker note
});

test("a connector Kafka Connect reports as paused shows dashes, not Prometheus' lingering last sample", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/dataconnect/runtime/connectors/conn-src/status*", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ name: "conn-src", pipeline_id: "p-src", observed_state: "paused", connector: { state: "PAUSED" }, tasks: [] }),
  }));
  await page.reload();
  const poll = panel(page).locator('[data-metric="source_poll_rate"]');
  await expect(poll.locator(".mt-v")).toHaveText("—");
  await expect(poll.locator(".mt-lt")).toContainText("Records polled");
  const row = panel(page).locator('[aria-label="Per-task metrics"] .tbody .tr').first();
  await expect(row).not.toContainText("/s");
});

// ── Consumer lag read live from Kafka (the monitoring test run's finding #2) ─────────────────────────────────────────────────

function liveLagBody(name: string, range: string) {
  const body = metricsBody(name, range) as ReturnType<typeof metricsBody> & { metrics: Record<string, unknown>[] };
  body.metrics = body.metrics.map((m) => (m.id === "sink_consumer_lag" ? { ...m, current: 250, series: [], history: false } : m));
  body.task_columns = [{ id: "sink_read_rate", label: "Rate", unit: "records/s" }, { id: "sink_consumer_lag", label: "Consumer lag", unit: "count" }];
  body.tasks = [{ task: "0", status: "paused", worker: "connect-sink-pool", values: { sink_read_rate: 7, sink_consumer_lag: 250 } }];
  return body;
}

async function openSink(page: Page, { paused = false } = {}) {
  await setup(page, (route, name, range) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(liveLagBody(name, range)) }));
  if (paused) {
    // Registered after the mocked backend so it wins (the newest route is tried first), then reload, as the existing paused test does.
    await page.route("**/api/v1/dataconnect/runtime/connectors/conn-sink/status*", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ name: "conn-sink", pipeline_id: "p-sink", observed_state: "paused", connector: { state: "PAUSED" }, tasks: [] }),
    }));
    await page.reload();
  }
  await page.locator(".rail .ri", { hasText: "orders-sink" }).click();
  await expect(panel(page).locator(".ct")).toContainText("orders-sink");
}

test("the sink's consumer lag shows the real backlog, and says it is live with no history instead of 'no samples'", async ({ page }) => {
  await openSink(page);

  const lag = panel(page).locator('[data-metric="sink_consumer_lag"]');
  await expect(lag.locator(".mt-v")).toHaveText("250");
  await expect(lag).toContainText("Live from Kafka offsets · no history");
  await expect(lag).not.toContainText("No samples in this range");
});

test("while the sink is paused its backlog still shows (it is exactly what to look at), and only the Prometheus values are dashed", async ({ page }) => {
  await openSink(page, { paused: true });

  await expect(panel(page).locator('[data-metric="sink_consumer_lag"] .mt-v')).toHaveText("250");
  await expect(panel(page).locator('[data-metric="sink_read_rate"] .mt-v')).toHaveText("—");          // Prometheus' lingering last rate is still hidden
  const row = panel(page).locator('[aria-label="Per-task metrics"] .tbody .tr').first();
  await expect(row).toContainText("250");                                                            // the task's backlog too
  await expect(row).not.toContainText("/s");
});

// Kafka Connect numbers a connector's tasks from 0; the page counts from 1 ("Task 1" is Connect's task 0). A JDBC sink or source can now run
// several tasks (the pipeline option tasks_max), so the table lists every one, in order, and the failure notice names them the same way.
test("a connector with several tasks lists Task 1, Task 2, Task 3 — counted from 1, in order", async ({ page }) => {
  await setup(page, (route, name, range) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify(metricsBody(name, range, { tasks: [
      { task: "0", status: "running", worker: "connect-jdbc-pool", values: {} },
      { task: "1", status: "running", worker: "connect-jdbc-pool", values: {} },
      { task: "2", status: "paused", worker: "connect-jdbc-pool", values: {} },
    ] })),
  }));
  const rows = panel(page).locator('[aria-label="Per-task metrics"] .tbody .tr');
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("Task 1");
  await expect(rows.nth(1)).toContainText("Task 2");
  await expect(rows.nth(2)).toContainText("Task 3");
  await expect(rows.nth(2)).toContainText("PAUSED");
  await expect(panel(page).locator('[aria-label="Per-task metrics"]')).not.toContainText("#");   // no "#0" style numbering anywhere
});

test("two failed tasks are named together in the notice, counted from 1", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/dataconnect/runtime/connectors/conn-src/status", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      name: "conn-src", pipeline_id: "p-src", connector: { state: "RUNNING" },
      tasks: [
        { id: 0, state: "FAILED", trace: "org.apache.kafka.connect.errors.ConnectException: first" },
        { id: 1, state: "RUNNING" },
        { id: 2, state: "FAILED", trace: "org.apache.kafka.connect.errors.ConnectException: third" },
      ],
      observed_state: "degraded", observed_at: new Date().toISOString(), runtime_endpoint: null, error: null,
    }),
  }));
  await page.reload();
  await page.locator(".sn", { hasText: "Monitoring" }).click();
  await expect(panel(page).locator(".alert-w", { hasText: "failed" }).locator(".at")).toHaveText("Tasks 1, 3 failed");
});

// ── The same metrics in the side panel opened from a Pipelines row ───────────────────────────────────────────────────────────

test("the side panel shows the tiles and tabs but no per-task table and no API-response footer, at the width of the other pipeline panel", async ({ page }) => {
  await setup(page);
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "orders-cdc" }), "View metrics");
  const drawer = page.locator(".modal");
  await expect(drawer.locator("#connector-metrics")).toContainText("Records polled");
  await expect(drawer.getByRole("tab", { name: "1h" })).toHaveAttribute("aria-selected", "true");
  await expect(drawer.getByRole("table", { name: "Per-task metrics" })).toHaveCount(0);
  await expect(drawer.getByText("Tasks", { exact: true })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: /API response/ })).toHaveCount(0);
  await expect(drawer.getByText(/Click a tile to enlarge/)).toHaveCount(0);
  expect((await drawer.boundingBox())!.width).toBeLessThanOrEqual(520);              // the same width as the Pipeline detail panel
  await page.getByRole("button", { name: "Close", exact: true }).first().click();

  await page.locator(".sn", { hasText: "Monitoring" }).click();                       // the Monitoring page keeps both
  await expect(panel(page).getByRole("table", { name: "Per-task metrics" })).toBeVisible();
  await expect(panel(page).getByRole("button", { name: /API response/ })).toBeVisible();
});

test("a failed task in the side panel can still be restarted, from its notice", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/dataconnect/runtime/connectors/conn-src/status", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      name: "conn-src", pipeline_id: "p-src", connector: { state: "RUNNING" },
      tasks: [{ id: 0, state: "RUNNING" }, { id: 1, state: "FAILED", trace: "org.apache.kafka.connect.errors.ConnectException: Source connection reset" }],
      observed_state: "degraded", observed_at: new Date().toISOString(), runtime_endpoint: null, error: null,
    }),
  }));
  const restarts: string[] = [];
  await page.route("**/api/v1/dataconnect/pipelines/p-src/restart", (route) => { restarts.push(route.request().method()); return route.fallback(); });
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: "orders-cdc" }), "View metrics");
  const failure = page.locator(".modal .alert-w", { hasText: "Task 2 failed" });
  await expect(failure).toContainText("ConnectException: Source connection reset");
  await failure.getByRole("button", { name: "Restart task" }).click();
  await expect.poll(() => restarts).toEqual(["POST"]);
});
