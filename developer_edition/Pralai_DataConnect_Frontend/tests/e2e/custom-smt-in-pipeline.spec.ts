/*
 * The New pipeline form can use a Custom SMT (a policy made with "+ New SMT") instead of the ticked transformation boxes: an optional dropdown right after
 * the Transformations section. Either the boxes or a Custom SMT, never both; a Custom SMT with a step the connector can't run can't be chosen.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend, choose, openList } from "./fixtures";

const SPECS = [
  { type: "ValueToKey", description: "Set the record key", params: [] },
  { type: "MaskField", description: "Mask a field", params: [] },
];
const tpl = (id: string, name: string, specs: unknown[], supportsSMT = true, type = "source") => ({
  id, name, connector_type: type, category: "database", connector_class: "x.y.Z", runtime_pool_type: "kafka-connect", config_schema: {}, default_config: {},
  default_transforms: [], supported_transforms: specs, version: "v1", is_active: true, supports_dlq: false,
  capabilities: { cdc: true, snapshot: true, schemaDiscovery: true, supportsSMT, supportsDLQ: false },
});
const step = (order: number, name: string, type: string) => ({ order, name, type, scope: "value", config: {} });

async function openForm(page: Page, extra?: (s: ReturnType<typeof freshState>) => void) {
  const state = freshState();
  const withRouter = [...SPECS, { type: "RegexRouter", description: "Route", params: [] }];
  state.templates.splice(
    0, state.templates.length, tpl("t-main", "Main source", SPECS), tpl("t-narrow", "Narrow source", [SPECS[0]!]), tpl("t-none", "No-SMT source", [], false),
    tpl("t-router-src", "Router source", withRouter), tpl("t-router-sink", "Router sink", withRouter, true, "sink"),
  );
  state.connections.push({ id: "c1", org_id: "o", name: "seed-pg", source_type: "postgres", connection_config: {}, status: "active" });
  state.transforms.push(
    { id: "s-key", org_id: "o", name: "key-by-id", description: "mine", origin: "custom", transforms: [step(1, "createKey", "ValueToKey")] },
    { id: "s-both", org_id: "o", name: "key-and-mask", description: "mine", origin: "custom", transforms: [step(2, "maskPII", "MaskField"), step(1, "createKey", "ValueToKey")] },
    { id: "s-hdr", org_id: "o", name: "with-header", description: "mine", origin: "custom", transforms: [step(1, "hdr", "HeaderFrom")] },
    { id: "s-router", org_id: "o", name: "reroute-topic", description: "mine", origin: "custom", transforms: [step(1, "rr", "RegexRouter")] },
    { id: "s-auto", org_id: "o", name: "some-pipeline-transforms", description: "Auto-created from selected source transformations", origin: "auto", transforms: [step(1, "k", "ValueToKey")] },
  );
  state.pipelines.push({ id: "p-old", org_id: "o", name: "already-using", status: "running", topic_name: "t", connector_name: "c", mode: "batch", generated_config: {}, transform_policy_id: "s-key" });
  extra?.(state);
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  await page.getByRole("button", { name: "+ New pipeline" }).click();
  await expect(page.locator(".modal, .drawer").last()).toBeVisible();
  await choose(page.locator("#pipe-template"), "t-main");
  return state;
}

const smt = (page: Page) => page.locator("#pipe-custom-smt");

test("the Custom SMT dropdown sits after Transformations and before Data quality, optional like the quality policy", async ({ page }) => {
  await openForm(page);
  await expect(page.getByText("Custom SMT chain (optional)")).toBeVisible();
  const y = async (loc: ReturnType<Page["locator"]>) => (await loc.first().boundingBox())!.y;
  const boxes = await y(page.getByLabel("ValueToKey")), dropdown = await y(smt(page)), quality = await y(page.locator("#pipe-quality"));
  expect(boxes).toBeLessThan(dropdown);
  expect(dropdown).toBeLessThan(quality);
  await expect(smt(page)).toHaveAttribute("data-value", "");                         // nothing chosen by default
  await expect(smt(page)).toContainText("— none —");
});

test("it offers the Custom SMTs only — an Auto one is a pipeline's own copy — and a step the connector can't run is listed but can't be chosen", async ({ page }) => {
  await openForm(page);
  const list = await openList(smt(page));
  await expect(list.rows).toHaveText(["— none —", "key-by-id (1 SMTs)", "key-and-mask (2 SMTs)", /with-header \(1 SMTs\) — not supported by this connector: HeaderFrom/, /reroute-topic \(1 SMTs\) — not supported by this connector: RegexRouter/]);
  await expect(list.row("s-hdr")).toBeDisabled();
  await expect(list.row("s-key")).toBeEnabled();
  await expect(page.locator(".ss-menu [role=option]", { hasText: "some-pipeline-transforms" })).toHaveCount(0);
});

test("choosing one shows its steps in order, who else uses it, and turns the boxes off; the pipeline is created with it and no copy is made", async ({ page }) => {
  const state = await openForm(page);
  let policyPosts = 0;
  let body: Record<string, unknown> | null = null;
  page.on("request", (r) => {
    if (r.method() !== "POST") return;
    if (r.url().endsWith("/transform-policies")) policyPosts += 1;
    if (r.url().endsWith("/pipelines")) body = r.postDataJSON();
  });
  await choose(smt(page), "s-both");
  const steps = page.getByRole("list", { name: "Steps of the chosen Custom SMT chain" }).locator("li");
  await expect(steps).toHaveText(["createKey — ValueToKey (value)", "maskPII — MaskField (value)"]);   // by order, not as stored
  await expect(page.getByLabel("ValueToKey")).toBeDisabled();
  await expect(page.getByLabel("MaskField")).toBeDisabled();
  await expect(page.locator("#pipe-custom-smt-help")).toContainText("turned off");

  await choose(smt(page), "s-key");
  await expect(page.getByText("Also used by already-using — editing this SMT chain later changes all of them.")).toBeVisible();

  await page.locator(".fld", { hasText: "Name" }).locator("input").first().fill("uses-custom");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(body).not.toBeNull();
  expect((body as unknown as Record<string, unknown>).transform_policy_id).toBe("s-key");
  expect(policyPosts).toBe(0);                                                       // nothing was copied
  expect(state.transforms).toHaveLength(5);
});

test("either the boxes or a Custom SMT: a ticked box turns the dropdown off, and choosing none brings the boxes back", async ({ page }) => {
  await openForm(page);
  await page.getByLabel("ValueToKey").check();
  await expect(smt(page)).toBeDisabled();
  await expect(page.locator("#pipe-custom-smt-help")).toContainText("Untick the transformations above");
  await page.getByLabel("ValueToKey").uncheck();
  await expect(smt(page)).toBeEnabled();

  await choose(smt(page), "s-key");
  await expect(page.getByLabel("MaskField")).toBeDisabled();
  await choose(smt(page), "");
  await expect(page.getByLabel("MaskField")).toBeEnabled();
});

test("with no Custom SMT chosen the ticked boxes still make the pipeline's own Auto policy, as before", async ({ page }) => {
  const state = await openForm(page);
  await page.getByLabel("MaskField").check();
  await page.locator(".fld", { hasText: "Name" }).locator("input").first().fill("boxes");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("Pipeline created");
  expect(state.transforms).toHaveLength(6);
  expect((state.transforms[5] as { origin?: string }).origin).toBe("auto");
});

test("changing the connector after choosing a Custom SMT it can't run blocks Create and says which step", async ({ page }) => {
  const state = await openForm(page);
  await choose(smt(page), "s-both");                                                 // ValueToKey + MaskField: fine on Main source
  await choose(page.locator("#pipe-template"), "t-narrow");                          // Narrow source has ValueToKey only
  await expect(page.locator("#pipe-custom-smt-error")).toContainText("key-and-mask has MaskField, which Narrow source doesn't support");
  await expect(smt(page)).toHaveAttribute("aria-invalid", "true");
  await page.locator(".fld", { hasText: "Name" }).locator("input").first().fill("blocked");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator(".toast").last()).toContainText("doesn't support MaskField");
  expect(state.pipelines).toHaveLength(1);                                           // only the seeded one: nothing was created
  await choose(smt(page), "s-key");                                                  // a compatible one clears it
  await expect(page.locator("#pipe-custom-smt-error")).toHaveCount(0);
});

test("a connector that takes no transformations: the dropdown is off, says why, and an earlier pick is dropped", async ({ page }) => {
  await openForm(page);
  await choose(smt(page), "s-key");
  await choose(page.locator("#pipe-template"), "t-none");
  await expect(smt(page)).toBeDisabled();
  await expect(smt(page)).toHaveAttribute("data-value", "");
  await expect(page.locator("#pipe-custom-smt-help")).toContainText("doesn't support transformations, so no Custom SMT");
});

test("RegexRouter: not offered on a source (it always writes to its pipeline's topic) and a Custom SMT holding it can't be chosen there; a sink can use both", async ({ page }) => {
  await openForm(page);
  await choose(page.locator("#pipe-template"), "t-router-src");
  await expect(page.getByLabel("MaskField")).toBeVisible();
  await expect(page.getByLabel("RegexRouter")).toHaveCount(0);
  const onSource = await openList(smt(page));
  await expect(onSource.row("s-router")).toBeDisabled();
  await expect(onSource.row("s-router")).toContainText("not supported by this connector: RegexRouter");
  await page.keyboard.press("Escape");

  await choose(page.locator("#pipe-template"), "t-router-sink");
  await expect(page.getByLabel("RegexRouter")).toBeVisible();
  const onSink = await openList(smt(page));
  await expect(onSink.row("s-router")).toBeEnabled();
});
