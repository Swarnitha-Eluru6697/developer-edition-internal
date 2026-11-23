/*
 * Error-state tests — RTK Query's error path rendered via src/components/ui.tsx's
 * ErrBox on a failed GET, the offline health badge, and a failed write
 * surfacing as an error toast rather than silently doing nothing.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend, mockNextCallFailure } from "./fixtures";

test("a failed list load renders the error box with the backend's message", async ({ page }) => {
  // Transforms, not Connections: Dashboard (the landing page) also queries
  // getConnections, and RTK Query's shared cache means whichever of the two
  // requests fires first "uses up" a one-shot route mock — Transforms isn't
  // fetched anywhere before this page, so it's an unambiguous first call.
  await mockBackend(page);
  await mockNextCallFailure(page, "/transform-policies", 500, "Database connection lost", "GET");
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();

  await expect(page.locator(".alert-w")).toContainText("Failed to load");
  await expect(page.locator(".alert-w")).toContainText("Database connection lost");
});

test("an unreachable backend shows the offline health badge, not a crash", async ({ page }) => {
  // Route priority is last-registered-wins, so the broad "everything else
  // succeeds" handler must be registered first — the specific /health abort
  // registered after it is what actually takes precedence for that one path.
  await page.route("**/api/v1/dataconnect/**", (route) => route.fulfill({ status: 200, body: "[]" }));
  await page.route("**/health", (route) => route.abort());
  await page.goto("/");

  await expect(page.locator(".tb-conn")).toHaveCount(0);   // the top bar no longer carries a backend status badge
});

test("a failed create surfaces an error toast instead of failing silently", async ({ page }) => {
  const state = freshState();
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await page.getByRole("button", { name: "+ New SMT chain" }).click();

  await mockNextCallFailure(
    page, "/transform-policies", 403,
    "Role 'DataConnect Viewer' is not permitted to manage transforms.", "POST",
  );
  await page.getByRole("button", { name: "Save SMT chain" }).click();

  await expect(page.locator(".toast").last()).toContainText("not permitted");
  // The failed write must not have been applied client-side.
  await expect(page.locator(".tbody")).toContainText("No SMT chains yet");
});
