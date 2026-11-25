/*
 * A connection's detail drawer lists the pipelines that use it (name and status), or says none does.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const conn = (id: string, name: string) => ({ id, org_id: "o", name, source_type: "postgresql", connection_config: { hostname: "db", database: name }, status: "active" });
const pipe = (id: string, name: string, connectionId: string, status: string) => ({
  id, org_id: "o", name, status, topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "batch", generated_config: {}, connection_profile_id: connectionId,
});

async function openConnection(page: Page, name: string) {
  const state = freshState();
  state.connections.push(conn("c-shop", "shop-db"), conn("c-idle", "idle-db"));
  state.pipelines.push(pipe("p1", "orders-cdc", "c-shop", "running"), pipe("p2", "orders-sink", "c-shop", "paused"), pipe("p3", "other", "c-elsewhere", "running"));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Connections" }).click();
  await page.locator(".conn-grid >> text=postgresql").first().click();
  await page.getByRole("button", { name: `View ${name}` }).click();
  await expect(page.locator(".drawer, .modal").last()).toContainText("Source");
}

test("the drawer lists the pipelines using the connection, with their status, and only those", async ({ page }) => {
  await openConnection(page, "shop-db");
  const drawer = page.locator(".drawer, .modal").last();
  await expect(drawer.getByText("Pipelines using this connection (2)")).toBeVisible();
  const items = drawer.getByRole("list", { name: "Pipelines using this connection" }).getByRole("listitem");
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText("orders-cdc");
  await expect(items.nth(0)).toContainText("running");
  await expect(items.nth(1)).toContainText("orders-sink");
  await expect(items.nth(1)).toContainText("paused");
  await expect(drawer).not.toContainText("other");                                      // another connection's pipeline is not listed
});

test("a connection no pipeline uses says so", async ({ page }) => {
  await openConnection(page, "idle-db");
  const drawer = page.locator(".drawer, .modal").last();
  await expect(drawer.getByText("Pipelines using this connection (0)")).toBeVisible();
  await expect(drawer.getByText("No pipeline uses this connection yet")).toBeVisible();
});
