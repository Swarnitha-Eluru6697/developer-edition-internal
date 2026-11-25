/*
 * A connector can be RUNNING with every task healthy and still fail every record (errors.tolerance=all skips them). Found in the
 * 2026-10-28 large-data test: a JDBC sink on a CDC topic read 101,500 records, wrote 0 rows, and the Monitoring list said RUNNING,
 * the Dashboard "all tasks healthy" and the Pipelines page counted all 101,500 as "written". The summary's error_rate (records/s
 * failed in the last 5 minutes) now puts an "errors" flag next to the state.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
const pipe = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, org_id: "org_test", name, status: "running", topic_name: `t-${id}`, connector_name: `conn-${id}`, mode: "api", generated_config: {},
  source_system: "postgres", entity: name, ...extra,
});
const row = (id: string, direction: string, error_rate: number | null) => ({
  connector_name: `conn-${id}`, pipeline_id: id, pipeline_name: id, direction, runtime_pool_id: "connect-sink-pool", rate: 0, lag: 0,
  tasks_running: 1, tasks_failed: 0, error_rate,
});

async function seed(page: Page) {
  const state = freshState();
  state.pipelines.push(pipe("bad", "skipping-sink", { direction: "SINK" }), pipe("ok", "healthy-sink", { direction: "SINK" }),
    pipe("off", "paused-sink", { direction: "SINK", status: "paused" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill(json({
    reachable: true, connect_url: "", bootstrap: "", message: "", topics: [],
    connectors: [
      { name: "conn-bad", pipeline_id: "bad", type: "sink", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
      { name: "conn-ok", pipeline_id: "ok", type: "sink", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
      { name: "conn-off", pipeline_id: "off", type: "sink", state: "PAUSED", tasks: ["PAUSED"], topics: [] },
    ],
    sinks: [{ connector: "conn-bad", consumed: 101500, lag: 0, topics: ["t"] }, { connector: "conn-ok", consumed: 500, lag: 0, topics: ["t"] }],
  })));
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill(json({
    prometheus_enabled: true, reachable: true, error: null, observed_at: new Date().toISOString(),
    connectors: [row("bad", "sink", 22.4), row("ok", "sink", 0), row("off", "sink", 5)],   // the paused one's last samples still say 5/s
  })));
  await page.goto("/");
}

test("Dashboard: a running connector that is failing records gets an 'errors' flag, and the card no longer says all is healthy", async ({ page }) => {
  await seed(page);
  const table = page.getByRole("table", { name: "Deployed pipelines" });
  await expect(table.locator(".tr", { hasText: "skipping-sink" })).toContainText("RUNNING");
  await expect(table.locator(".tr", { hasText: "skipping-sink" }).locator(".badge", { hasText: "errors" })).toBeVisible();
  await expect(table.locator(".tr", { hasText: "skipping-sink" }).locator(".badge", { hasText: "errors" })).toHaveAttribute("title", /22 records\/s failed in the last 5 minutes/);
  await expect(table.locator(".tr", { hasText: "healthy-sink" }).locator(".badge", { hasText: "errors" })).toHaveCount(0);
  await expect(table.locator(".tr", { hasText: "paused-sink" }).locator(".badge", { hasText: "errors" })).toHaveCount(0);   // paused: processes nothing
  const failedCard = page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: /^Failed connectors$/ }) });
  await expect(failedCard).toContainText("1 running but failing records");
  await expect(failedCard).not.toContainText("all tasks healthy");
});

test("Monitoring: the connector list flags the one failing records, and only that one", async ({ page }) => {
  await seed(page);
  await page.locator(".sn", { hasText: "Monitoring" }).click();
  const item = (connector: string) => page.locator(`.ri[data-connector="${connector}"]`);
  await expect(item("conn-bad")).toContainText("RUNNING");
  await expect(item("conn-bad").locator(".badge", { hasText: "errors" })).toBeVisible();
  await expect(item("conn-ok").locator(".badge", { hasText: "errors" })).toHaveCount(0);
  await expect(item("conn-off").locator(".badge", { hasText: "errors" })).toHaveCount(0);
});

test("Pipelines: the sink total says what it counts — records READ by sinks, not written", async ({ page }) => {
  await seed(page);
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const card = page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: /^Records read by sinks$/ }) });
  await expect(card).toContainText("102000");
  await expect(page.locator(".sc-l", { hasText: /^Records written$/ })).toHaveCount(0);
});

test("without any failing connector the card still says all tasks are healthy", async ({ page }) => {
  const state = freshState();
  state.pipelines.push(pipe("ok", "healthy-sink", { direction: "SINK" }));
  await mockBackend(page, state);
  await page.route("**/api/v1/dataconnect/runtime/flow", (route) => route.fulfill(json({
    reachable: true, connect_url: "", bootstrap: "", message: "", topics: [], sinks: [],
    connectors: [{ name: "conn-ok", pipeline_id: "ok", type: "sink", state: "RUNNING", tasks: ["RUNNING"], topics: [] }],
  })));
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => route.fulfill(json({
    prometheus_enabled: true, reachable: true, error: null, observed_at: new Date().toISOString(), connectors: [row("ok", "sink", 0)],
  })));
  await page.goto("/");
  await expect(page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: /^Failed connectors$/ }) })).toContainText("all tasks healthy");
});
