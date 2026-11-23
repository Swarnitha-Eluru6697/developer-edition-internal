/*
 * AppContext / state tests — permissions and tenant from GET /me, exposed by src/context/AppContext.tsx's useApp(). Driven through the
 * real app shell (src/DataConnectApp.tsx) rather than by importing the hook in isolation, since useApp() throws outside its provider tree
 * by design. The personas (Admin / Integrator / Viewer) are test data: the app only ever sees their permission lists.
 */
import { test, expect } from "@playwright/test";
import { freshState, meFor, mockBackend, switchRole, expectActiveRole } from "./fixtures";

test("with every permission, every page is in the nav and there is no role switcher or simulated user", async ({ page }) => {
  await mockBackend(page);
  await page.goto("/");
  await expectActiveRole(page, "Admin");
  for (const name of ["Dashboard", "Monitoring", "Connections", "Pipelines", "Schemas", "Quality", "Lineage", "Transforms", "Templates"]) {
    await expect(page.locator("aside.sb")).toContainText(name);
  }
  await expect(page.locator(".role-switch, .role-pill, .user-sim-btn")).toHaveCount(0);
  await expect(page.locator(".app")).not.toHaveAttribute("data-dc-role", /.*/);
});

test("the signed-in user and tenant come from /me", async ({ page }) => {
  const state = freshState();
  state.me = { ...meFor("Viewer", "Priya Nair"), tenant: "org_acme" };
  await mockBackend(page, state);
  await page.goto("/");
  await expect(page.locator(".app")).toHaveAttribute("data-dc-user", "Priya Nair");
  await expect(page.locator(".app")).toHaveAttribute("data-dc-org", "org_acme");
  await expect(page.locator(".tb-av")).toHaveText("P");
  await expect(page.locator("aside.sb")).not.toContainText("org_acme");
});

test("pages follow the permissions: a page whose view permission is missing is not in the nav", async ({ page }) => {
  const state = freshState();
  state.me = { ...meFor("Viewer"), permissions: ["dataconnect:access:view", "dataconnect:pipelines:view", "dataconnect:audit:view"] };
  await mockBackend(page, state);
  await page.goto("/");
  await expect(page.locator("aside.sb .sn")).toHaveText([/Pipelines/, /Audit/]);
  await expect(page.locator(".ph")).toContainText("Pipelines");               // the first page they can see, not the Dashboard
});

test("no DataConnect permission at all shows 'no access' and no page", async ({ page }) => {
  const state = freshState();
  state.me = { ...meFor("Viewer"), permissions: ["mlstudio:access:view"] };
  await mockBackend(page, state);
  await page.goto("/");
  await expect(page.getByTestId("dc-access-denied")).toContainText("No access to DataConnect");
  await expect(page.locator(".cont")).toHaveCount(0);
  await expect(page.locator("aside.sb .sn")).toHaveCount(0);
});

test("a failed /me shows an error and allows nothing", async ({ page }) => {
  const state = freshState();
  state.me = 401;
  await mockBackend(page, state);
  await page.goto("/");
  await expect(page.getByTestId("dc-access-denied")).toContainText("Could not check your access");
  await expect(page.locator(".cont")).toHaveCount(0);
});

test("no request carries an identity, tenant or role header", async ({ page }) => {
  const sent: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/v1/dataconnect/")) sent.push(...Object.keys(req.headers()).map((h) => h.toLowerCase()));
  });
  await mockBackend(page);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await expect(page.locator(".ph")).toContainText("Pipelines");
  for (const h of ["x-org-id", "x-actor", "x-user-role", "x-user-roles", "x-user-permissions", "x-tenant-id"]) expect(sent, h).not.toContain(h);
});
