/*
 * Module Federation mounting test.
 *
 * This repo is the federation *remote* (see vite.config.ts: it exposes
 * "./DataConnectApp" as remoteEntry.js) — the actual cross-app mount only
 * happens inside the Control Portal host, which doesn't exist in this repo
 * and can't be driven from here. What this repo owns, and what these tests
 * verify, is its half of that contract:
 *   1. The exposed component (src/DataConnectApp.tsx) mounts and renders a
 *      working shell on its own, the way the host will use it — proven via
 *      the standalone dev harness (index.html -> src/main.tsx), which
 *      renders the same <DataConnectApp> the host imports.
 *   2. A production build actually emits a loadable remoteEntry.js bundle
 *      with the federation manifest the host's dynamic import depends on.
 */
import { execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { mockBackend } from "./fixtures";

const dirname = path.dirname(fileURLToPath(import.meta.url));

test("the exposed DataConnectApp component mounts and renders the shell", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");

  await expect(page.locator(".dc-root")).toBeVisible();
  await expect(page.locator("aside.sb")).toContainText("InaiEra");
  await expect(page.locator(".app")).toHaveAttribute("data-dc-access", "ready");
  await expect(page.locator(".tb-av")).toBeVisible();                     // the signed-in user from /me (no host account menu here)
  const pageErrors: string[] = [];
  page.on("pageerror", (err) => pageErrors.push(String(err)));
  await page.reload();
  await expect(page.locator(".dc-root")).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("a production build emits a loadable remoteEntry.js exposing ./DataConnectApp", async () => {
  const root = path.resolve(dirname, "../..");
  execSync("npm run build", { cwd: root, stdio: "pipe" });

  const remoteEntryPath = path.join(root, "dist/assets/remoteEntry.js");
  expect(existsSync(remoteEntryPath)).toBe(true);

  const contents = readFileSync(remoteEntryPath, "utf-8");
  expect(contents).toContain("dataconnect_mfe");
  expect(contents).toContain("./DataConnectApp");
});
