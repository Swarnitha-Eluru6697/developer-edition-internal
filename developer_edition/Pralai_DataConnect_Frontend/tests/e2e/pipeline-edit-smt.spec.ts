/*
 * Edit pipeline shows the same Transformations, Custom SMT chain and Initial load sections as New pipeline, loaded with what the pipeline has now.
 * Nothing about them is sent unless the user changes one of them; a change links another chain, makes a fresh Standard chain from ticked boxes, or removes the link.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, choose, rowAction } from "./fixtures";

const SPECS = [
  { type: "ValueToKey", description: "Set the record key", params: [{ key: "fields", label: "Field(s)", type: "text", required: true, placeholder: "id" }] },
  { type: "MaskField", description: "Mask a field", params: [{ key: "fields", label: "Fields to mask", type: "text", required: true, placeholder: "email" }] },
];
const TEMPLATE = {
  id: "tpl-jdbc", name: "JDBC Source", connector_type: "source", category: "database_jdbc", connector_class: "io.confluent.connect.jdbc.JdbcSourceConnector",
  runtime_pool_type: "kafka-connect", config_schema: {}, default_config: {}, default_transforms: [], supported_transforms: SPECS, version: "v1", is_active: true, supports_dlq: false,
  capabilities: { cdc: false, snapshot: true, schemaDiscovery: true, supportsSMT: true, supportsDLQ: false },
};
const step = (order: number, name: string, type: string, config: Record<string, string>) => ({ order, name, type, scope: "value", config });
const pipe = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, org_id: "o", name, status: "draft", topic_name: `t-${id}`, connector_name: `c-${id}`, mode: "batch", zone: "raw", template_id: "tpl-jdbc",
  connection_profile_id: "c1", generated_config: {}, revision: 1, connector_config: { "tasks.max": "1", tables: ["products"] }, ...extra,
});

async function seeded(page: Page) {
  const state = freshState();
  state.templates.splice(0, state.templates.length, TEMPLATE);
  state.connections.push({ id: "c1", org_id: "o", name: "db", source_type: "postgresql", connection_config: {}, status: "active" });
  state.transforms.push(
    { id: "std-1", org_id: "o", name: "has-auto-transforms", description: "Auto-created from selected source transformations", origin: "auto", transforms: [step(1, "ValueToKey", "ValueToKey", { fields: "id" })] },
    { id: "cus-1", org_id: "o", name: "mask-pii", description: "mine", origin: "custom", transforms: [step(1, "m", "MaskField", { fields: "email" })] },
    { id: "cus-2", org_id: "o", name: "other-chain", description: "mine", origin: "custom", transforms: [step(1, "k", "ValueToKey", { fields: "sku" })] },
    { id: "cus-bad", org_id: "o", name: "needs-header", description: "mine", origin: "custom", transforms: [step(1, "h", "HeaderFrom", { fields: "a" })] },
  );
  state.pipelines.push(
    pipe("p-none", "no-chain"),
    pipe("p-auto", "has-auto", { transform_policy_id: "std-1", connector_config: { "tasks.max": "1", tables: ["products"], load_mode: "incrementing" } }),
    pipe("p-custom", "has-custom", { transform_policy_id: "cus-1" }),
    pipe("p-live", "deployed-one", { status: "running" }),
  );
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  return state;
}

async function edit(page: Page, name: string) {
  await rowAction(page.locator(".tr", { hasText: name }), "Edit");
  await expect(page.locator(".modal")).toContainText("Edit pipeline");
}
const patches = (page: Page) => {
  const bodies: Record<string, unknown>[] = [];
  page.on("request", (r) => { if (r.method() === "PATCH" && /\/pipelines\//.test(r.url())) bodies.push(r.postDataJSON()); });
  return bodies;
};
const box = (page: Page, type: string) => page.locator("label", { hasText: type }).locator("input[type=checkbox]");
const chain = (page: Page) => page.locator("#pipe-custom-smt");

test("the edit form has the same three sections as the new form, and nothing is loaded into them for a pipeline with no chain", async ({ page }) => {
  await seeded(page);
  await edit(page, "no-chain");
  const form = page.locator(".modal");
  await expect(form.getByText("Transformations (supported by this source)")).toBeVisible();
  await expect(form.getByText("Custom SMT chain (optional)")).toBeVisible();
  await expect(form.getByText("Initial load / snapshot")).toBeVisible();
  await expect(box(page, "ValueToKey")).not.toBeChecked();
  await expect(box(page, "MaskField")).not.toBeChecked();
  await expect(chain(page)).toHaveAttribute("data-value", "");
  // the same order as New pipeline: transformations, chain, data quality, error handling, initial load
  const y = async (text: string) => (await form.getByText(text, { exact: true }).first().boundingBox())!.y;
  expect(await y("Transformations (supported by this source)")).toBeLessThan(await y("Custom SMT chain (optional)"));
  expect(await y("Custom SMT chain (optional)")).toBeLessThan(await y("Data quality"));
  expect(await y("Data quality")).toBeLessThan(await y("Error handling"));
  expect(await y("Error handling")).toBeLessThan(await y("Initial load / snapshot"));
});

test("a pipeline's own Standard chain comes up as ticked boxes with its settings, and its stored load mode as the load mode", async ({ page }) => {
  await seeded(page);
  await edit(page, "has-auto");
  await expect(box(page, "ValueToKey")).toBeChecked();
  await expect(page.locator("#pipe-tf-ValueToKey-fields")).toHaveValue("id");
  await expect(box(page, "MaskField")).not.toBeChecked();
  await expect(chain(page)).toBeDisabled();                                         // either the boxes or a chain
  await expect(page.locator("#pipe-initload")).toHaveAttribute("data-value", "incrementing");
});

test("a pipeline on a Custom chain comes up with the chain chosen, its steps shown and the boxes off", async ({ page }) => {
  await seeded(page);
  await edit(page, "has-custom");
  await expect(chain(page)).toHaveAttribute("data-value", "cus-1");
  await expect(chain(page)).toContainText("mask-pii");
  await expect(page.getByRole("list", { name: "Steps of the chosen Custom SMT chain" }).locator("li")).toHaveText(["m — MaskField (value)"]);
  await expect(box(page, "ValueToKey")).toBeDisabled();
  await expect(page.getByText(/Also used by/)).toHaveCount(0);                      // the pipeline itself isn't "another user" of its own chain
});

test("saving without touching those sections sends nothing about them", async ({ page }) => {
  await seeded(page);
  const sent = patches(page);
  await edit(page, "has-auto");
  await page.locator("#pipe-edit-name").fill("has-auto-renamed");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent).toHaveLength(1);
  expect(Object.keys(sent[0]!)).not.toContain("transform_policy_id");
  expect(Object.keys(sent[0]!)).not.toContain("connector_config");
});

test("ticking another box makes a fresh Standard chain, links it, and removes the pipeline's old Standard chain", async ({ page }) => {
  const state = await seeded(page);
  const sent = patches(page);
  await edit(page, "has-auto");
  await box(page, "MaskField").check();
  await page.locator("#pipe-tf-MaskField-fields").fill("email,phone");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  const made = state.transforms.find((t) => t.name === "has-auto-transforms" && t.id !== "std-1") as { id: string; origin: string; transforms: { type: string; config: Record<string, string> }[] };
  expect(made.origin).toBe("auto");
  expect(made.transforms.map((t) => t.type)).toEqual(["ValueToKey", "MaskField"]);
  expect(made.transforms[1]!.config.fields).toBe("email,phone");
  expect(sent[0]!.transform_policy_id).toBe(made.id);
  expect(state.transforms.some((t) => t.id === "std-1")).toBe(false);                // the old copy is gone
  expect(state.transforms.some((t) => t.id === "cus-1")).toBe(true);                 // a Custom chain is never touched
});

test("choosing a Custom chain links it and removes the old Standard chain; the Custom chain it replaces is kept", async ({ page }) => {
  const state = await seeded(page);
  const sent = patches(page);
  await edit(page, "has-auto");
  await box(page, "ValueToKey").uncheck();
  await choose(chain(page), "cus-2");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent[0]!.transform_policy_id).toBe("cus-2");
  expect(state.transforms.some((t) => t.id === "std-1")).toBe(false);

  await edit(page, "has-custom");
  await choose(chain(page), "cus-2");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent[1]!.transform_policy_id).toBe("cus-2");
  expect(state.transforms.some((t) => t.id === "cus-1")).toBe(true);                 // unlinking a Custom chain never deletes it
});

test("unticking everything, or choosing none, removes the link (null) — and deletes only the pipeline's own Standard chain", async ({ page }) => {
  const state = await seeded(page);
  const sent = patches(page);
  await edit(page, "has-auto");
  await box(page, "ValueToKey").uncheck();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent[0]!.transform_policy_id).toBeNull();
  expect(state.transforms.some((t) => t.id === "std-1")).toBe(false);

  await edit(page, "has-custom");
  await choose(chain(page), "");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent[1]!.transform_policy_id).toBeNull();
  expect(state.transforms.some((t) => t.id === "cus-1")).toBe(true);
});

test("a ticked transformation with a required setting left blank is refused, and nothing is created or sent", async ({ page }) => {
  const state = await seeded(page);
  const sent = patches(page);
  const before = state.transforms.length;
  await edit(page, "no-chain");
  await box(page, "MaskField").check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator("#pipe-tf-MaskField-fields-error")).toContainText("MaskField: Fields to mask is required.");
  expect(sent).toHaveLength(0);
  expect(state.transforms).toHaveLength(before);
});

test("a chain with a step this connector lacks can be seen but not chosen", async ({ page }) => {
  await seeded(page);
  await edit(page, "no-chain");
  await chain(page).click();
  const row = page.locator(".ss-menu [role=option]", { hasText: "needs-header" });
  await expect(row).toContainText("not supported by this connector: HeaderFrom");
  await expect(row).toHaveAttribute("aria-disabled", "true");
});

test("changing the initial load keeps the rest of the connector settings and sends the merged config", async ({ page }) => {
  await seeded(page);
  const sent = patches(page);
  await edit(page, "has-auto");
  await choose(page.locator("#pipe-initload"), "bulk");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("updated");
  expect(sent[0]!.connector_config).toEqual({ "tasks.max": "1", tables: ["products"], load_mode: "bulk" });
  expect(Object.keys(sent[0]!)).not.toContain("transform_policy_id");
});

test("a deployed pipeline's edit says the change reaches its connector; a draft's does not", async ({ page }) => {
  await seeded(page);
  await edit(page, "deployed-one");
  await expect(page.getByText(/already deployed: a change to its transformations or initial load/)).toBeVisible();
  await page.keyboard.press("Escape");
  await edit(page, "no-chain");
  await expect(page.getByText(/already deployed: a change/)).toHaveCount(0);
});
