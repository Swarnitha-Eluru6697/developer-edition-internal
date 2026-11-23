/*
 * The real UI, driven as each seeded role persona (a permission list from GET /me — tests/e2e/fixtures.ts), shows a gated control exactly
 * when the persona holds the permission behind it.
 */
import { test, expect } from "@playwright/test";
import { PERSONAS, freshState, meFor, mockBackend, type RoleName } from "./fixtures";

// A "+ New ..." control on a page and the permission that gates it.
const GATED: { page: string; button: string; permission: string }[] = [
  { page: "Pipelines", button: "+ New pipeline", permission: "dataconnect:pipelines:edit" },
  { page: "Transforms", button: "+ New SMT chain", permission: "dataconnect:transforms:edit" },
  { page: "Quality", button: "+ New Quality Check", permission: "dataconnect:quality:edit" },
];

for (const role of Object.keys(PERSONAS) as RoleName[]) {
  test(`${role}: gated controls appear exactly as the permissions say`, async ({ page }) => {
    const state = freshState();
    state.me = meFor(role);
    await mockBackend(page, state);
    await page.goto("/");
    await expect(page.locator(".app")).toHaveAttribute("data-dc-access", "ready");

    for (const { page: name, button, permission } of GATED) {
      await page.locator(".sn", { hasText: name }).first().click();
      const control = page.getByRole("button", { name: button, exact: true });
      if (PERSONAS[role].includes(permission)) await expect(control.first(), `${role} should see "${button}" on ${name}`).toBeVisible();      // Quality has two (the header's and the policy card's)
      else await expect(control, `${role} must not see "${button}" on ${name}`).toHaveCount(0);
    }
  });
}

test("the personas do not all see the same thing (the check above is not vacuous)", async () => {
  const sees = (role: RoleName) => GATED.map((g) => PERSONAS[role].includes(g.permission));
  expect(sees("Admin")).not.toEqual(sees("Viewer"));
});
