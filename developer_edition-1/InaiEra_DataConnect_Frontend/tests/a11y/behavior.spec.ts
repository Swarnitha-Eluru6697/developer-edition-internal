/*
 * FE-BUG-21: the behaviours axe can't see — keyboard navigation, the modal
 * focus trap, Escape, focus restoration, the unsaved-changes guard, toast
 * announcements, form-error associations and status indicators that don't rely
 * on colour. axe (pages.spec.ts) checks how the DOM is marked up; these check
 * what actually happens when someone uses only a keyboard or a screen reader.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, choose, rowAction, rowMenuButton } from "../e2e/fixtures";

function seed() {
  const state = freshState();
  state.connections.push({
    id: "conn-b", org_id: "org_test", name: "erp-prod-db", source_type: "postgres",
    connection_config: { hostname: "erp-db.internal", port: 5432, database: "erp" }, status: "active", verification_status: "ok",
  });
  state.pipelines.push({
    id: "pipe-b", org_id: "org_test", name: "ERP Customer CDC", status: "running",
    topic_name: "org_test-raw-crm-erp-customers-cdc-v1", connector_name: "erp-customer-cdc", zone: "raw", mode: "cdc",
    generated_config: {}, desired_state: "running", observed_state: "running", last_observed_at: new Date().toISOString(),
  });
  state.qualityPolicies.push({ id: "qp-b", org_id: "org_test", name: "PII validation", on_failure: "route_to_quarantine", rules: [{ name: "email_format", type: "format", field: "email" }] });
  return state;
}

const activeIsInside = (page: Page, selector: string) =>
  page.evaluate((sel) => !!document.activeElement?.closest(sel), selector);

async function openPipelineDetail(page: Page) {
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "ERP Customer CDC" });
  const trigger = rowMenuButton(row);                     // the three dots: the panel hands focus back to it when it closes
  await rowAction(row, "View pipeline detail");
  await expect(page.getByRole("dialog")).toBeVisible();
  return trigger;
}

test.describe("modal focus handling", () => {
  test("focus moves into the dialog and Tab / Shift+Tab never leave it", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await openPipelineDetail(page);

    expect(await activeIsInside(page, '[role="dialog"]')).toBe(true);
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      expect(await activeIsInside(page, '[role="dialog"]'), `Tab #${i + 1} escaped the dialog`).toBe(true);
    }
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Shift+Tab");
      expect(await activeIsInside(page, '[role="dialog"]'), `Shift+Tab #${i + 1} escaped the dialog`).toBe(true);
    }
  });

  test("the dialog is labelled by its title", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await openPipelineDetail(page);

    const dialog = page.getByRole("dialog", { name: "Pipeline detail" });
    await expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  test("Escape closes the dialog and focus returns to the control that opened it", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    const trigger = await openPipelineDetail(page);

    await page.keyboard.press("Escape");

    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("Escape on a form with unsaved changes asks first instead of discarding, and its own focus is trapped", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await page.locator(".sn", { hasText: "Pipelines" }).click();
    const newPipeline = page.getByRole("button", { name: "+ New pipeline" });
    await newPipeline.click();
    await expect(page.getByRole("dialog", { name: /new pipeline/i })).toBeVisible();

    // Make the form dirty.
    await choose(page.locator("#pipe-err-policy"), { index: 1 });

    await page.keyboard.press("Escape");
    const guard = page.getByRole("alertdialog", { name: "Unsaved changes" });
    await expect(guard).toBeVisible();
    expect(await activeIsInside(page, '[role="alertdialog"]')).toBe(true);
    for (let i = 0; i < 6; i += 1) {
      await page.keyboard.press("Tab");
      expect(await activeIsInside(page, '[role="alertdialog"]'), `Tab #${i + 1} left the guard`).toBe(true);
    }

    // Escape on the guard backs out (keeps editing) rather than discarding.
    await page.keyboard.press("Escape");
    await expect(guard).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: /new pipeline/i })).toBeVisible();

    // Discard really closes it, and focus goes back to where the user started.
    await page.keyboard.press("Escape");
    await guard.getByRole("button", { name: "Discard" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(newPipeline).toBeFocused();
  });
});

test.describe("keyboard navigation", () => {
  test("every sidebar item is reachable with Tab in order and Enter opens its page", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");

    const items = page.locator(".sb .sn");
    const labels = (await items.allTextContents()).map((t) => t.trim());
    expect(labels.length).toBeGreaterThan(5);

    // Walk the keyboard from the first item; each Tab must land on the next one.
    await items.first().focus();
    await expect(items.first()).toBeFocused();
    for (let i = 1; i < labels.length; i += 1) {
      await page.keyboard.press("Tab");
      await expect(items.nth(i), `Tab should reach "${labels[i]}"`).toBeFocused();
    }

    const quality = page.locator(".sb .sn", { hasText: "Quality" });
    await quality.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".sn.active")).toContainText("Quality");
  });

  test("the current page is exposed to assistive tech, not only by colour", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await page.locator(".sn", { hasText: "Connections" }).click();

    await expect(page.locator(".sb .sn.active")).toHaveAttribute("aria-current", "page");
    await expect(page.locator(".sb [aria-current]")).toHaveCount(1);
  });

  test("a clickable failure row on Quality can be focused and opened from the keyboard", async ({ page }) => {
    await mockBackend(page, seed());
    await page.route("**/api/v1/dataconnect/quality-stream-stats", (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([{
        id: "qs-1", org_id: "org_test", pipeline_id: "pipe-b", quality_policy_id: "qp-b", total_count: 10, failed_count: 2,
        sample_failures: [{
          original_topic: "t", pipeline_id: "pipe-b", schema_id: null, quality_policy_id: "qp-b", failure_type: "quality_rule_failed",
          failed_rules: ["email_format"], received_at: new Date().toISOString(), original_record: { email: "nope" },
        }],
      }]),
    }));
    await page.goto("/");
    await page.locator(".sn", { hasText: "Quality" }).click();
    await page.getByRole("tab", { name: "Results" }).click();
    await page.getByRole("tab", { name: "History" }).click();

    const row = page.getByTitle("View failed records");
    await row.focus();
    await expect(row).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
  });
});

test.describe("toast announcements", () => {
  test("toasts live in a polite live region, and a failure is announced as an alert", async ({ page }) => {
    await mockBackend(page, seed());
    await page.route("**/api/v1/dataconnect/quality-policies/qp-b/validate", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify({ valid: false, errors: ["rule 1 is broken"] }),
    }));
    await page.goto("/");
    await page.locator(".sn", { hasText: "Quality" }).click();

    const region = page.locator(".toasts");
    await expect(region).toHaveAttribute("role", "status");
    await expect(region).toHaveAttribute("aria-live", "polite");

    await page.locator(".tr", { hasText: "PII validation" }).locator('[title="Validate"]').click();
    await expect(page.getByRole("alert").filter({ hasText: "rule 1 is broken" })).toBeVisible();
  });

  test("a success toast is announced politely, not as an interrupting alert", async ({ page }) => {
    await mockBackend(page, seed());
    await page.route("**/api/v1/dataconnect/quality-policies/qp-b/validate", (route) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify({ valid: true, errors: [] }),
    }));
    await page.goto("/");
    await page.locator(".sn", { hasText: "Quality" }).click();

    await page.locator(".tr", { hasText: "PII validation" }).locator('[title="Validate"]').click();
    const toast = page.locator(".toasts .toast", { hasText: "Quality check valid" });
    await expect(toast).toBeVisible();
    await expect(toast).not.toHaveAttribute("role", "alert");
  });
});

test.describe("form errors", () => {
  test("a field error is tied to its input and announced", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await page.locator(".sn", { hasText: "Connections" }).click();
    await page.locator(".ctile").filter({ has: page.locator(".clbl", { hasText: /^postgres$/ }) }).click();
    await page.locator('button[title^="New"]').click();
    await page.getByRole("button", { name: "Create connection" }).click();

    // Whichever field is reported first: invalid, described by a visible alert with real text.
    const firstInvalid = page.locator('[role="dialog"] [aria-invalid="true"]').first();
    await expect(firstInvalid).toBeVisible();
    const inputId = await firstInvalid.getAttribute("id");
    expect(inputId).toBeTruthy();
    const describedBy = await firstInvalid.getAttribute("aria-describedby");
    expect(describedBy, "an invalid input must point at its error message").toBeTruthy();
    const message = page.locator(`[id="${describedBy}"]`);
    await expect(message).toHaveAttribute("role", "alert");
    await expect(message).not.toBeEmpty();

    // Fixing the field clears the error and the association (re-find it by id: the locator above stops matching).
    const input = page.locator(`[id="${inputId}"]`);
    await input.fill("fixed-value");
    await expect(message).toHaveCount(0);
    await expect(input).not.toHaveAttribute("aria-invalid", "true");
    await expect(input).not.toHaveAttribute("aria-describedby", describedBy as string);
  });
});

test.describe("status is never colour-only", () => {
  const PAGES = ["Dashboard", "Connections", "Pipelines", "Quality", "Transforms", "Schemas", "Lineage", "Monitoring", "Audit / History"];

  for (const label of PAGES) {
    test(`${label}: every status badge carries readable text`, async ({ page }) => {
      await mockBackend(page, seed());
      await page.goto("/");
      if (label !== "Dashboard") await page.locator(".sn", { hasText: label }).click();
      await page.locator(".cont").waitFor();

      const texts = await page.locator(".dc-root .badge").evaluateAll((els) => els.map((el) => (el as HTMLElement).innerText.trim()));
      for (const text of texts) expect(text, "a .badge with no text conveys status by colour alone").not.toBe("");
    });
  }

  test("a pipeline row states its state in text — the pill is a word, not just a coloured dot", async ({ page }) => {
    await mockBackend(page, seed());
    await page.goto("/");
    await page.locator(".sn", { hasText: "Pipelines" }).click();

    const row = page.locator(".tr", { hasText: "ERP Customer CDC" });
    await expect(row).toContainText(/running|paused|failed|draft|deployed|unknown/i);   // the state, as a word
    await expect(rowMenuButton(row)).toBeVisible();                                      // the row's actions are one button (they list the state-independent views)
  });
});
