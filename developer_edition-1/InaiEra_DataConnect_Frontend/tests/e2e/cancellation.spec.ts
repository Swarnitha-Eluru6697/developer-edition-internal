/*
 * Request cancellation — proves the AbortSignal wiring (api.ts + apiSlice.ts)
 * actually cancels the real, in-flight browser request when a page unmounts
 * mid-fetch, not just that the plumbing type-checks. Playwright/Chromium
 * reports a client-aborted request's failure as net::ERR_ABORTED via the
 * `requestfailed` event — that's the signal this test watches for.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

test("navigating away mid-request aborts the underlying fetch instead of letting it run to completion", async ({ page }) => {
  const state = freshState();
  await mockBackend(page);

  // Hold the Transforms list response open until the test releases it —
  // long enough to navigate away from the page that's waiting on it.
  let releaseResponse!: () => void;
  const held = new Promise<void>((resolve) => { releaseResponse = resolve; });
  let requestReachedServer = false;
  await page.route("**/api/v1/dataconnect/transform-policies", async (route) => {
    requestReachedServer = true;
    await held;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state.transforms) });
  });

  const failedUrls: string[] = [];
  const failureReasons: string[] = [];
  page.on("requestfailed", (req) => {
    failedUrls.push(req.url());
    failureReasons.push(req.failure()?.errorText ?? "");
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await expect.poll(() => requestReachedServer).toBe(true);

  // Navigate away before the held response is ever released — RTK Query
  // should unsubscribe getTransforms and abort its fetch.
  await page.locator(".sn", { hasText: "Quality" }).click();
  await expect(page.locator(".ph .ptitle")).toHaveText("Quality");

  await expect.poll(() => failedUrls.some((u) => u.includes("/transform-policies"))).toBe(true);
  const reason = failureReasons[failedUrls.findIndex((u) => u.includes("/transform-policies"))];
  expect(reason).toContain("ABORTED");

  releaseResponse();
});
