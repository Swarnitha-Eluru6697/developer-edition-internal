/*
 * FE-02: the Pipelines page keeps the last good runtime snapshot so a reload does not flash zeros — but it must never pass for current status.
 * These tests drive the real page against a mocked backend: disconnect the runtime, reload, switch organisation, and check what is shown.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, setTenant } from "./fixtures";

const FLOW_URL = "**/api/v1/dataconnect/runtime/flow";
const BASE = "http://localhost:8001/api/v1/dataconnect";                 // the standalone-dev defaults the page scopes its snapshot to
const KEY = (org: string) => `dc.pipelines.lastFlow:2:${encodeURIComponent(BASE)}:${encodeURIComponent(org)}`;
const ORG = "org_1001";
const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
const flowWith = (n: number, over: Record<string, unknown> = {}) => ({
  reachable: true, connect_url: "http://connect", bootstrap: "kafka:9092", message: "",
  connectors: Array.from({ length: n }, (_, i) => ({ name: `conn-${i}`, pipeline_id: `p${i}`, type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] })),
  topics: [], sinks: [], ...over,
});
const liveConnectors = (page: Page) => page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: /^Live connectors$/ }) });
const banner = (page: Page) => page.locator("[data-flow-freshness]");

async function openPipelines(page: Page) {
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(page.getByText("Manage data ingestion pipelines")).toBeVisible();
}
/** Load the page once with a healthy runtime so a snapshot is saved, then return. */
async function seedSnapshot(page: Page, connectors = 3) {
  await mockBackend(page, freshState());
  await page.route(FLOW_URL, (route) => route.fulfill(json(flowWith(connectors))));
  await openPipelines(page);
  await expect(liveConnectors(page)).toContainText(String(connectors));
  await expect.poll(() => page.evaluate((k) => localStorage.getItem(k), KEY(ORG))).not.toBeNull();
}
const stored = (page: Page, key: string) => page.evaluate((k) => { const raw = localStorage.getItem(k); return raw ? JSON.parse(raw) : null; }, key);
const backdate = (page: Page, key: string, minutes: number) => page.evaluate(([k, m]) => {
  const e = JSON.parse(localStorage.getItem(k as string)!); e.observed_at = new Date(Date.now() - (m as number) * 60_000).toISOString();
  localStorage.setItem(k as string, JSON.stringify(e));
}, [key, minutes] as const);

test("a live reading shows no stale banner, and the saved snapshot records when it was observed and for which backend and organisation", async ({ page }) => {
  await seedSnapshot(page);

  await expect(banner(page)).toHaveCount(0);
  const entry = await stored(page, KEY(ORG));
  expect(entry).toMatchObject({ v: 2, base_url: BASE, org_id: ORG });
  expect(Math.abs(Date.now() - Date.parse(entry.observed_at))).toBeLessThan(60_000);
  expect(entry.flow.connectors).toHaveLength(3);
});

test("while the live answer is slow after a reload, the saved numbers are shown without a banner, and the live ones replace them", async ({ page }) => {
  await seedSnapshot(page, 3);
  await backdate(page, KEY(ORG), 7);
  let release!: () => void;
  const gate = new Promise<void>((r) => { release = r; });
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, async (route) => { await gate; await route.fulfill(json(flowWith(5))); });

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(liveConnectors(page)).toContainText("3");                      // the saved value is shown while the live answer is on its way …
  await expect(banner(page)).toHaveCount(0);                                  // … without a "not live" banner (it flashed on every refresh)
  release();
  await expect(liveConnectors(page)).toContainText("5");                      // the live value replaces it
  await expect(banner(page)).toHaveCount(0);
});

test("runtime disconnected after a reload: the saved snapshot is shown as stale with how long ago it was observed", async ({ page }) => {
  await seedSnapshot(page, 3);
  await backdate(page, KEY(ORG), 12);
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.abort());

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "stale");
  await expect(banner(page)).toContainText("observed 12 min ago");
  await expect(banner(page)).toContainText("out of date");
  await expect(liveConnectors(page)).toContainText("3");
});

test("a failed refresh does not overwrite the saved snapshot or refresh its timestamp", async ({ page }) => {
  await seedSnapshot(page, 3);
  await backdate(page, KEY(ORG), 12);
  const before = (await stored(page, KEY(ORG))).observed_at;
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.abort());

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "stale");

  expect((await stored(page, KEY(ORG))).observed_at).toBe(before);
});

test("switching organisation never shows the other organisation's snapshot", async ({ page }) => {
  await seedSnapshot(page, 3);                                                // org_1001 has a snapshot of 3 connectors
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.abort());
  setTenant(page, "org_2002");                                              // the next sign-in lands in another tenant (GET /me)

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "unavailable");     // nothing for THIS org: say so, show no borrowed numbers
  await expect(liveConnectors(page)).toContainText("0");
  expect(await stored(page, KEY("org_2002"))).toBeNull();                               // and nothing was written for it from the failure
  expect(await stored(page, KEY(ORG))).not.toBeNull();                                  // org_1001's snapshot is untouched, for when it comes back
});

test("each organisation keeps its own snapshot", async ({ page }) => {
  await seedSnapshot(page, 3);
  setTenant(page, "org_2002");                                              // the next sign-in lands in another tenant (GET /me)
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.fulfill(json(flowWith(8))));
  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(liveConnectors(page)).toContainText("8");
  await expect.poll(() => stored(page, KEY("org_2002")).then((e) => e?.flow.connectors.length)).toBe(8);

  expect((await stored(page, KEY(ORG))).flow.connectors).toHaveLength(3);
});

test("a snapshot older than the maximum age is not shown", async ({ page }) => {
  await seedSnapshot(page, 3);
  await backdate(page, KEY(ORG), 61);
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.abort());

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "unavailable");
  await expect(liveConnectors(page)).toContainText("0");
});

test("the old unscoped snapshot is never shown, and is deleted", async ({ page }) => {
  await mockBackend(page, freshState());
  await page.route(FLOW_URL, (route) => route.abort());
  await page.goto("/");
  await page.evaluate((flow) => localStorage.setItem("dc.pipelines.lastFlow", JSON.stringify(flow)), flowWith(9));

  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();

  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "unavailable");
  await expect(liveConnectors(page)).toContainText("0");
  expect(await page.evaluate(() => localStorage.getItem("dc.pipelines.lastFlow"))).toBeNull();
});

test("the backend saying Kafka Connect is unreachable is flagged, and is not saved as a good snapshot", async ({ page }) => {
  await mockBackend(page, freshState());
  await page.route(FLOW_URL, (route) => route.fulfill(json(flowWith(0, { reachable: false, message: "Kafka Connect not reachable at http://connect:8083" }))));

  await openPipelines(page);

  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "unreachable");
  await expect(banner(page)).toContainText("Kafka Connect not reachable");
  expect(await stored(page, KEY(ORG))).toBeNull();
});

test("an unreachable reply does not replace an earlier good snapshot", async ({ page }) => {
  await seedSnapshot(page, 3);
  await page.unroute(FLOW_URL);
  await page.route(FLOW_URL, (route) => route.fulfill(json(flowWith(0, { reachable: false, message: "down" }))));
  await page.reload();
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(banner(page)).toHaveAttribute("data-flow-freshness", "unreachable");

  expect((await stored(page, KEY(ORG))).flow.connectors).toHaveLength(3);
});

test("blocked storage does not break the page", async ({ page }) => {
  await page.addInitScript(() => {
    const blocked = () => { throw new Error("storage blocked"); };
    Storage.prototype.setItem = blocked; Storage.prototype.removeItem = blocked;
  });
  await mockBackend(page, freshState());
  await page.route(FLOW_URL, (route) => route.fulfill(json(flowWith(2))));

  await openPipelines(page);

  await expect(liveConnectors(page)).toContainText("2");
  await expect(banner(page)).toHaveCount(0);
});
