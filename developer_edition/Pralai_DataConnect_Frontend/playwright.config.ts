import { defineConfig, devices } from "@playwright/test";

/*
 * Single Playwright config for the whole frontend test suite:
 *  - "unit"  — plain Node-context specs (tests/unit/**) that import
 *              src/*.ts directly (e.g. the API client) and never touch a
 *              browser page. Playwright only launches a browser when a spec
 *              actually requests the `page`/`context`/`browser` fixture, so
 *              these run as fast, dependency-free logic tests.
 *  - "e2e"   — browser-driven specs (tests/e2e/**) against the real Vite dev
 *              server, with the backend replaced by page.route() mocks (see
 *              tests/e2e/fixtures.ts) so the suite never needs a live
 *              Postgres/Kafka stack to pass.
 *  - "live"  — FE-04: the REAL workflow, no mocks: the browser drives this UI against the real FastAPI backend, PostgreSQL, Kafka and Kafka
 *              Connect (the Docker Compose stack in Pralai_DataConnect_Backend/infra). Only present when DATACONNECT_LIVE_E2E=1 (see
 *              `npm run test:live` / scripts/live-e2e.sh), so a plain `npm test` stays hermetic and needs no stack.
 *  - "a11y"  — FE-BUG-21: @axe-core/playwright scans (tests/a11y/**) plus
 *              keyboard/focus-behavior assertions axe itself can't check
 *              (Escape-to-close, focus trap, focus restoration) — the
 *              actual "release gate" this ticket asks for. Same mocked-
 *              backend approach as "e2e" (reuses tests/e2e/fixtures.ts)
 *              so scans run against realistic populated pages, not an
 *              empty app.
 */
const LIVE = process.env.DATACONNECT_LIVE_E2E === "1";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    trace: "retain-on-failure",
  },
  projects: [
    { name: "unit", testDir: "./tests/unit" },
    {
      name: "e2e",
      testDir: "./tests/e2e",
      use: { ...devices["Desktop Chrome"], baseURL: "http://localhost:5001" },
    },
    ...(LIVE ? [{
      name: "live",
      testDir: "./tests/live",
      // One shared pipeline, run step by step against shared infrastructure: never parallel, and slow on purpose (real connectors, real Kafka).
      fullyParallel: false,
      workers: 1,
      timeout: 240_000,
      expect: { timeout: 30_000 },
      use: { ...devices["Desktop Chrome"], baseURL: "http://localhost:5001", actionTimeout: 20_000, trace: "retain-on-failure" as const, screenshot: "only-on-failure" as const },
    }] : []),
    {
      name: "a11y",
      testDir: "./tests/a11y",
      use: { ...devices["Desktop Chrome"], baseURL: "http://localhost:5001" },
    },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5001",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
