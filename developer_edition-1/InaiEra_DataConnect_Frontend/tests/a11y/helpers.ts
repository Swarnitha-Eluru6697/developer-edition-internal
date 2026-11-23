/*
 * FE-BUG-21: shared axe-core scan helper for tests/a11y/**. Scoped to
 * `.dc-root` (this app's own mounted subtree — see src/styles.css's header
 * comment on why every rule is scoped that way) so a scan never flags
 * anything in the Playwright/host page chrome outside this app's control,
 * and restricted to WCAG 2.0/2.1 A+AA rules — the same bar most real
 * release gates hold, not axe's full "best practice" rule set (which
 * includes opinionated checks this ticket didn't ask for).
 */
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

export async function scan(page: Page) {
  return new AxeBuilder({ page })
    .include(".dc-root")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
}

// Modal.tsx's drawer slides in over ~260ms while its dim backdrop fades in over 200ms (.ov -> .ov.on, see styles.css).
// Scanning mid-way makes axe see the dialog shifted or behind a half-transparent backdrop and flag color-contrast
// failures — a race between the test's click() and the CSS transitions, not a real defect. Waiting for the panel to
// reach its resting position and the backdrop to reach its final colour (a no-op when there's no open dialog) makes
// the scan deterministic.
async function waitForOverlaySettled(page: Page) {
  await page.waitForFunction(() => {
    const ov = document.querySelector(".ov");
    if (!ov) return true;
    const modal = ov.querySelector(".modal");
    const transform = modal ? getComputedStyle(modal).transform : "none";
    const resting = transform === "none" || transform === "matrix(1, 0, 0, 1, 0, 0)";
    return ov.classList.contains("on") && resting && /rgba\(0, 0, 0, 0?\.(4|6)\)/.test(getComputedStyle(ov).backgroundColor);
  });
}

export async function expectNoViolations(page: Page) {
  await waitForOverlaySettled(page);
  const results = await scan(page);
  if (results.violations.length) {
    const summary = results.violations
      .map((v) => `\n[${v.impact}] ${v.id}: ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).join("\n  ")}`)
      .join("\n");
    expect(results.violations, `axe-core found ${results.violations.length} violation(s):${summary}`).toEqual([]);
  }
}
