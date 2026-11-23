/*
 * Retry / backoff — end-to-end proof through the real RTK Query + api.ts
 * stack (not just the isolated unit tests in tests/unit/api.spec.ts): a
 * page hitting two transient 502/503s in a row should still load
 * successfully, and a page hitting a persistent failure should still
 * surface the error after retries are exhausted, not hang or retry forever.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

test("a read that fails twice with a transient 503 still loads once retries succeed", async ({ page }) => {
  const state = freshState();
  await mockBackend(page, state);

  // Two failures in a row, then fall through to mockBackend's real handler —
  // exactly the boundary of this app's retry budget (1 initial + 2 retries).
  let failuresLeft = 2;
  await page.route("**/api/v1/dataconnect/transform-policies", (route) => {
    if (route.request().method() !== "GET" || failuresLeft <= 0) return route.fallback();
    failuresLeft -= 1;
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "upstream unavailable" }) });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();

  // Never shows the error box — the retries absorbed both transient failures.
  await expect(page.locator(".empty")).toContainText("No SMT chains yet", { timeout: 10_000 });
  await expect(page.locator(".alert-w")).toHaveCount(0);
  expect(failuresLeft).toBe(0);
});

test("a persistently failing read exhausts retries and surfaces the error, without retrying forever", async ({ page }) => {
  await mockBackend(page);
  let requestCount = 0;
  await page.route("**/api/v1/dataconnect/transform-policies", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    requestCount += 1;
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "upstream unavailable" }) });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();

  await expect(page.locator(".alert-w")).toContainText("upstream unavailable", { timeout: 10_000 });
  // 1 initial attempt + 2 retries = 3 total — proves it gave up rather than
  // either failing immediately (no retry at all) or retrying indefinitely.
  expect(requestCount).toBe(3);
});

test("a non-idempotent write (create) is never retried on a transient failure", async ({ page }) => {
  await mockBackend(page);
  let requestCount = 0;
  await page.route("**/api/v1/dataconnect/transform-policies", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    requestCount += 1;
    return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "upstream unavailable" }) });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await page.getByRole("button", { name: "+ New SMT chain" }).click();
  await page.getByRole("button", { name: "Save SMT chain" }).click();

  await expect(page.locator(".toast").last()).toContainText("upstream unavailable");
  expect(requestCount).toBe(1);
});
