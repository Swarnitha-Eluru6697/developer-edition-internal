/*
 * Auto-refresh polling — the React/DOM-dependent half of
 * src/hooks/usePolling.ts (the pure backoff/jitter math is covered by
 * tests/unit/usePolling.spec.ts instead): pausing while the tab is hidden,
 * and refetching immediately the moment it's visible again rather than
 * waiting out the rest of a tick. Driven through Monitoring's connector summary call (5s — the
 * fastest tier of the polling cadence matrix, see ../../src/pollingPolicy.ts)
 * rather than Pipelines' own list view (20s): usePolling() is the same
 * shared mechanism regardless of which page's tier it's configured with, so
 * proving it here once, at the faster tier, is both sufficient and keeps
 * this test from needing a real ~20s wait to stay meaningful.
 *
 * Simulating "hidden" by overriding `document.visibilityState` and firing a
 * real `visibilitychange` event is the standard way to drive the Page
 * Visibility API in a test — it's the exact event usePolling() subscribes
 * to, not a synthetic stand-in for it.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

async function setTabVisibility(page: import("@playwright/test").Page, state: "visible" | "hidden") {
  await page.evaluate((s) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => s });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}

test("pauses polling while the tab is hidden and refetches immediately when it's visible again", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-poll", org_id: "org_test", name: "poll-pipe", status: "running",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "poll-pipe-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  let metricsRequests = 0;
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => {
    metricsRequests += 1;
    return route.fallback();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Monitoring" }).click();
  await expect.poll(() => metricsRequests).toBeGreaterThanOrEqual(1);

  await setTabVisibility(page, "hidden");
  const countWhenHidden = metricsRequests;

  // Longer than the 5s base interval — if polling weren't actually paused,
  // at least one more tick would have fired in this window.
  await page.waitForTimeout(6500);
  expect(metricsRequests).toBe(countWhenHidden);

  await setTabVisibility(page, "visible");
  // Immediate refetch on regaining visibility, not "wait out the rest of the interval".
  await expect.poll(() => metricsRequests, { timeout: 2000 }).toBeGreaterThan(countWhenHidden);
});

test("Monitoring auto-refreshes on its own — a connector's state can change on Kafka Connect's side with no browser action at all (FE-BUG-06)", async ({ page }) => {
  const state = freshState();
  state.pipelines.push({
    id: "pipe-mon", org_id: "org_test", name: "mon-pipe", status: "running",
    topic_name: "org_test-raw-crm-salesforce-accounts-batch-v1", connector_name: "mon-pipe-connector",
    mode: "api", generated_config: {},
  });
  await mockBackend(page, state);

  let metricsRequests = 0;
  await page.route("**/api/v1/dataconnect/runtime/connectors/metrics/summary", (route) => {
    metricsRequests += 1;
    return route.fallback();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Monitoring" }).click();
  await expect.poll(() => metricsRequests).toBeGreaterThanOrEqual(1);
  const firstFetch = metricsRequests;

  // No click, no manual "Refresh" — just time passing at this page's own
  // 5s tier (see the test above). Before FE-BUG-06 this page never
  // refetched anything on its own at all, so this would never pass.
  await expect.poll(() => metricsRequests, { timeout: 8000 }).toBeGreaterThan(firstFetch);
});
