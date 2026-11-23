/*
 * FE-BUG-09: CodeBlock (src/components/ui.tsx) — the shared renderer behind
 * the transform preview's "Generated Kafka Connect config" view — now
 * redacts secret-shaped keys itself, independent of whatever the backend
 * already redacted. Exercised through the real preview flow rather than
 * unit-testing redactSecrets alone (see tests/unit/secretRedaction.spec.ts
 * for that): this proves the redaction actually reaches the DOM, not just
 * that the underlying function is correct in isolation.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend, switchRole, rowAction, rowMenuButton } from "./fixtures";

test("the transform preview's generated-config view redacts secret-shaped fields even though the backend response didn't", async ({ page }) => {
  const state = freshState();
  state.transforms.push({
    id: "tf-preview", org_id: "org_test", name: "Customer basic transform",
    description: "key + mask PII", transforms: [],
  });
  await mockBackend(page, state);

  // Simulates a backend response that — for whatever reason (a bug, a new
  // connector type the backend's own allowlist doesn't know about yet) —
  // still carries a real secret-shaped field unredacted. The frontend must
  // never trust that the backend always gets this right.
  await page.route("**/api/v1/dataconnect/transform-policies/tf-preview/preview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        generated_config: {
          "database.hostname": "db.internal.example.com",
          "database.password": "hunter2-not-redacted-by-backend",
          "transforms.maskPII.type": "org.apache.kafka.connect.transforms.MaskField$Value",
        },
      }),
    });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await rowAction(page.locator(".tr", { hasText: "Customer basic transform" }), "Preview Customer basic transform");
  await page.getByRole("button", { name: "Generate config" }).click();

  const codeBlock = page.locator(".codeblk");
  await expect(codeBlock).toContainText("db.internal.example.com");
  await expect(codeBlock).toContainText("[REDACTED]");
  await expect(codeBlock).not.toContainText("hunter2-not-redacted-by-backend");
});

test("the SMT chain view redacts secret-shaped keys inside a step's config (FE-BUG-09)", async ({ page }) => {
  const state = freshState();
  state.transforms.push({
    id: "tf-secret", org_id: "org_test", name: "Leaky transform", description: "",
    transforms: [{ order: 1, name: "custom", type: "CustomSMT", config: { field: "email", api_token: "tok-not-redacted-by-backend" } }],
  });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  await rowAction(page.locator(".tr", { hasText: "Leaky transform" }), "View Leaky transform");

  const modal = page.locator(".modal");
  await expect(modal).toContainText("CustomSMT");
  await expect(modal).toContainText("[REDACTED]");
  await expect(modal).not.toContainText("tok-not-redacted-by-backend");
});

test("Viewer sees a genuine read-only SMT chain view, not a hidden or disabled form (FE-BUG-19)", async ({ page }) => {
  const state = freshState();
  state.transforms.push({
    id: "tf-view", org_id: "org_test", name: "Customer basic transform",
    description: "key + mask PII",
    transforms: [
      { order: 1, name: "createKey", type: "ValueToKey", scope: "value", config: { fields: "customer_id" } },
      { order: 2, name: "maskPII", type: "MaskField", scope: "value", config: { fields: "email,phone" } },
    ],
  });
  await mockBackend(page, state);

  await page.goto("/");
  await switchRole(page, "Viewer");
  await page.locator(".sn", { hasText: "Transforms" }).click();

  const row = page.locator(".tr", { hasText: "Customer basic transform" });
  await rowMenuButton(row).click();
  await expect(page.getByRole("menuitem")).toHaveText(["View details"]);        // manage-only actions are not in a Viewer's menu
  await page.keyboard.press("Escape");

  await rowAction(row, "View Customer basic transform");
  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Customer basic transform");
  await expect(dialog).toContainText("createKey");
  await expect(dialog).toContainText("ValueToKey");
  await expect(dialog).toContainText("maskPII");
  // Read-only presentation, not a form — no editable input/textarea inside.
  await expect(dialog.locator("input, textarea")).toHaveCount(0);
});

test("each SMT chain row has one three-dots button under Actions; its menu lists View details, Validate, Preview config and Delete, icon + name, Delete in red", async ({ page }) => {
  const state = freshState();
  state.transforms.push({ id: "tf-m", org_id: "org_test", name: "menu-chain", description: "d", transforms: [], origin: "custom" });
  await mockBackend(page, state);
  const calls: string[] = [];
  await page.route("**/api/v1/dataconnect/transform-policies/tf-m/validate", (route) => { calls.push("validate"); return route.fallback(); });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Transforms" }).click();
  const row = page.locator(".tr", { hasText: "menu-chain" });
  await expect(row.locator(".acts .ai")).toHaveCount(1);
  const dots = rowMenuButton(row);
  const head = (await page.locator(".th", { hasText: "Actions" }).boundingBox())!;
  const box = (await dots.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - (head.x + head.width / 2))).toBeLessThan(6);          // centred under the heading
  await dots.click();
  const items = page.getByRole("menuitem");
  await expect(items).toHaveText(["View details", "Validate", "Preview config", "Delete"]);
  for (const item of await items.all()) await expect(item.locator("svg")).toHaveCount(1);
  await expect(page.getByRole("menuitem", { name: "Delete menu-chain" })).toHaveCSS("color", "rgb(185, 28, 28)");
  await page.getByRole("menuitem", { name: "Validate menu-chain" }).click();                    // a pick runs the action and closes the menu
  await expect.poll(() => calls).toEqual(["validate"]);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(dots).toBeFocused();
});
