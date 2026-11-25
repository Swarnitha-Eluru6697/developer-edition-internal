/*
 * The CDC "Snapshot mode" choices are the ones the pipeline's own Debezium connector accepts (2.5.4, installed on Kafka Connect).
 * Found in the 2026-10-28 large-data test: one list was offered for every Debezium connector, and PostgreSQL + "no_data" deployed
 * only to be rejected by Kafka Connect. New and Edit pipeline share the list (snapConfig in pages/Pipelines.tsx).
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, rowAction } from "./fixtures";

const template = (id: string, klass: string) => ({
  id, name: id, connector_type: "source", category: "database_cdc", connector_class: klass, runtime_pool_type: "kafka-connect",
  config_schema: {}, default_config: {}, default_transforms: [], supported_transforms: [], version: "v1", is_active: true, supports_dlq: false,
  capabilities: { cdc: true, snapshot: true, schemaDiscovery: true, supportsSMT: true, supportsDLQ: false },
});
const pipe = (id: string, name: string, tpl: string, mode?: string) => ({
  id, org_id: "o", name, status: "draft", topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "cdc", zone: "raw", template_id: tpl,
  connection_profile_id: "c1", generated_config: {}, revision: 1, connector_config: { "tasks.max": "1", tables: ["orders"], ...(mode ? { snapshot_mode: mode } : {}) },
});

async function editFor(page: Page, name: string) {
  const state = freshState();
  state.templates.splice(0, state.templates.length,
    template("tpl-pg", "io.debezium.connector.postgresql.PostgresConnector"),
    template("tpl-mongo", "io.debezium.connector.mongodb.MongoDbConnector"),
    template("tpl-mssql", "io.debezium.connector.sqlserver.SqlServerConnector"));
  state.connections.push({ id: "c1", org_id: "o", name: "db", source_type: "postgresql", connection_config: {}, status: "active" });
  state.pipelines.push(pipe("p-pg", "pg-cdc", "tpl-pg"), pipe("p-legacy", "pg-legacy-no-data", "tpl-pg", "no_data"),
    pipe("p-mongo", "mongo-cdc", "tpl-mongo"), pipe("p-mssql", "mssql-legacy-no-data", "tpl-mssql", "no_data"));
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await rowAction(page.locator(".tr", { hasText: name }), "Edit");
  await expect(page.locator(".modal")).toContainText("Edit pipeline");
  const select = page.locator("#pipe-initload");
  const options = async () => (JSON.parse((await select.getAttribute("data-options")) ?? "[]") as { value: string }[]).map((o) => o.value);
  return { select, options };
}

test("PostgreSQL CDC offers only the modes its connector accepts — no no_data, when_needed, configuration_based or custom", async ({ page }) => {
  const { select, options } = await editFor(page, "pg-cdc");
  await expect.poll(options).toEqual(["initial", "initial_only", "always", "never"]);
  await expect(select).toHaveAttribute("data-value", "initial");                                  // the default
  await expect(page.locator(".modal")).toContainText("never streams new changes only");
});

test("each Debezium connector gets its own list (MongoDB: initial or never)", async ({ page }) => {
  const { options } = await editFor(page, "mongo-cdc");
  await expect.poll(options).toEqual(["initial", "never"]);
});

test("a pipeline saved with the old no_data shows the stream-only mode it deploys as", async ({ page }) => {
  const pg = await editFor(page, "pg-legacy-no-data");
  await expect(pg.select).toHaveAttribute("data-value", "never");
});

test("…and for SQL Server, whose stream-only mode is schema_only", async ({ page }) => {
  const mssql = await editFor(page, "mssql-legacy-no-data");
  await expect.poll(mssql.options).toEqual(["initial", "initial_only", "schema_only"]);
  await expect(mssql.select).toHaveAttribute("data-value", "schema_only");
});
