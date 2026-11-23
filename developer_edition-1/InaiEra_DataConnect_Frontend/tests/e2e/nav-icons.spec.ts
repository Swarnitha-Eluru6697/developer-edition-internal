/*
 * FE-01: the sidebar icons are real React SVG components, not HTML strings pushed through dangerouslySetInnerHTML. These tests pin what is rendered
 * (every nav button has an icon with the expected shape elements) and fail if raw-HTML injection comes back anywhere in src/.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src");

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sourceFiles(path) : /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

// Shape elements each icon is drawn from, in order (what the old string markup contained).
const EXPECTED: Record<string, string[]> = {
  Dashboard: ["rect", "rect", "rect", "rect"],
  Monitoring: ["path"],
  Connections: ["path"],
  Pipelines: ["circle", "circle", "path"],
  Schemas: ["path"],
  Quality: ["path", "path"],
  Lineage: ["circle", "circle", "circle", "path"],
  Transforms: ["path"],
  Templates: ["rect", "path"],
  Settings: ["circle", "path"],
  "Audit / History": ["path", "circle"],
};

test("every sidebar item has a decorative SVG icon drawn from the expected shapes (FE-01)", async ({ page }) => {
  await mockBackend(page, freshState());
  await page.goto("/");
  for (const [label, shapes] of Object.entries(EXPECTED)) {
    const svg = page.locator(".sn", { hasText: label }).first().locator("svg");
    await expect(svg, label).toHaveCount(1);
    await expect(svg).toHaveAttribute("viewBox", "0 0 24 24");
    await expect(svg).toHaveAttribute("aria-hidden", "true");
    await expect(svg).toHaveAttribute("stroke", "currentColor");
    const tags = await svg.locator("> *").evaluateAll((els) => els.map((e) => e.tagName.toLowerCase()));
    expect(tags, label).toEqual(shapes);
  }
});

test("no source file renders raw HTML (dangerouslySetInnerHTML / innerHTML) (FE-01)", () => {
  const offenders = sourceFiles(SRC)
    .filter((f) => !f.endsWith(".generated.ts"))
    .filter((f) => /\.(dangerouslySetInnerHTML|innerHTML|outerHTML)\b|dangerouslySetInnerHTML\s*=|dangerouslySetInnerHTML\s*:/.test(readFileSync(f, "utf8")))
    .map((f) => f.slice(SRC.length + 1));
  expect(offenders).toEqual([]);
});
