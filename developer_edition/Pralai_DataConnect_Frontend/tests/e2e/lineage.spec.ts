/*
 * FE-BUG-20: the lineage graph defaults to the bounded per-pipeline chain
 * (never the org's whole lineage graph) and grows only through explicit,
 * bounded "expand upstream/downstream" requests — never a client-side
 * fetch-everything-then-hide-nodes-in-React shortcut.
 */
import { test, expect } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

function seedChain(state: ReturnType<typeof freshState>) {
  state.pipelines.push({
    id: "pipe-1", org_id: "org_test", name: "erp-pipe", status: "running",
    topic_name: "org_test-raw-crm-erp-customers-cdc-v1", connector_name: "erp-connector",
    mode: "cdc", generated_config: {},
  });
  const source = { id: "a-source", org_id: "org_test", asset_type: "source", name: "ERP System", qualified_name: "org_test:source:erp" };
  const connector = { id: "a-connector", org_id: "org_test", asset_type: "connector", name: "erp-connector", qualified_name: "org_test:connector:erp-connector" };
  const rawTopic = { id: "a-raw", org_id: "org_test", asset_type: "kafka_topic", name: "raw.customers", qualified_name: "org_test:topic:raw" };
  const quality = { id: "a-quality", org_id: "org_test", asset_type: "processor", name: "Quality Processor", qualified_name: "org_test:processor:q" };
  const preparedTopic = { id: "a-prepared", org_id: "org_test", asset_type: "kafka_topic", name: "prepared.customers", qualified_name: "org_test:topic:prepared" };
  const sink = { id: "a-sink", org_id: "org_test", asset_type: "sink", name: "Analytics Sink", qualified_name: "org_test:sink:analytics" };
  state.lineageAssets.push(source, connector, rawTopic, quality, preparedTopic, sink);
  state.lineageEdges.push(
    { id: "e1", org_id: "org_test", from_asset_id: source.id, to_asset_id: connector.id, edge_type: "CONNECTS_TO", pipeline_id: "pipe-1" },
    { id: "e2", org_id: "org_test", from_asset_id: connector.id, to_asset_id: rawTopic.id, edge_type: "INGESTS_TO", pipeline_id: "pipe-1" },
    { id: "e3", org_id: "org_test", from_asset_id: rawTopic.id, to_asset_id: quality.id, edge_type: "VALIDATED_BY", pipeline_id: "pipe-1" },
    { id: "e4", org_id: "org_test", from_asset_id: quality.id, to_asset_id: preparedTopic.id, edge_type: "EMITS_TO", pipeline_id: "pipe-1" },
    // Not part of the pipeline's own edges (no pipeline_id) — only reachable
    // by explicitly expanding downstream from the prepared topic, never
    // part of the default bounded view.
    { id: "e5", org_id: "org_test", from_asset_id: preparedTopic.id, to_asset_id: sink.id, edge_type: "WRITTEN_TO", pipeline_id: null },
  );
  return { source, connector, rawTopic, quality, preparedTopic, sink };
}

test("the default view is the bounded 5-node chain, in order — never the sink beyond it (FE-BUG-20)", async ({ page }) => {
  const state = freshState();
  seedChain(state);
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();

  await expect(page.locator(".lgraph .lnode")).toHaveCount(5);
  const names = await page.locator(".lgraph .ln-n").allTextContents();
  expect(names).toEqual(["ERP System", "erp-connector", "raw.customers", "Quality Processor", "prepared.customers"]);
  await expect(page.locator(".lgraph")).not.toContainText("Analytics Sink");
});

test("expanding downstream fetches exactly one bounded hop and merges it in, without refetching the whole graph (FE-BUG-20)", async ({ page }) => {
  const state = freshState();
  seedChain(state);
  await mockBackend(page, state);

  const expandRequests: string[] = [];
  await page.route("**/api/v1/dataconnect/lineage/a-prepared*", (route) => {
    expandRequests.push(route.request().url());
    return route.fallback();
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(5);

  const preparedNode = page.locator(".lnode", { hasText: "prepared.customers" });
  await preparedNode.getByRole("button", { name: /downstream/i }).click();

  await expect(page.locator(".lgraph")).toContainText("Analytics Sink");
  await expect(page.locator(".lgraph .lnode")).toHaveCount(6);

  expect(expandRequests).toHaveLength(1);
  const url = new URL(expandRequests[0]);
  expect(url.searchParams.get("depth")).toBe("1");
  expect(url.searchParams.get("direction")).toBe("downstream");
});

test("a hub with more links than one page shows '+N more' and pages through the rest (FE-BUG-20)", async ({ page }) => {
  const state = freshState();
  seedChain(state);
  // The prepared topic already feeds "Analytics Sink"; give it 4 more consumers (5 downstream edges in all).
  for (let i = 0; i < 4; i += 1) {
    state.lineageAssets.push({ id: `a-extra-${i}`, org_id: "org_test", asset_type: "sink", name: `Extra Sink ${i}`, qualified_name: `org_test:sink:extra${i}` });
    state.lineageEdges.push({
      id: `e-extra-${i}`, org_id: "org_test", from_asset_id: "a-prepared", to_asset_id: `a-extra-${i}`,
      edge_type: "WRITTEN_TO", pipeline_id: null,
    });
  }
  await mockBackend(page, state);

  // Shrink the page to 2 edges so the hub overflows it (the real default is the server's cap).
  const requested: string[] = [];
  await page.route("**/api/v1/dataconnect/lineage/a-prepared*", (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("limit", "2");
    requested.push(`${url.searchParams.get("offset") ?? "0"}`);
    return route.fallback({ url: url.toString() });
  });

  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(5);

  const prepared = page.locator(".lnode", { hasText: "prepared.customers" });
  await prepared.getByRole("button", { name: "downstream ▶" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(7); // + 2 sinks
  await expect(prepared.getByRole("button", { name: "Show 3 more downstream" })).toBeVisible();

  await prepared.getByRole("button", { name: "Show 3 more downstream" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(9); // + 2 more
  await prepared.getByRole("button", { name: "Show 1 more downstream" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(10); // all 5 sinks + the 5-node chain
  await expect(prepared.getByRole("button", { name: /more downstream/ })).toHaveCount(0);

  // Each click asked the server for the next page, not for the whole graph.
  expect(requested).toEqual(["0", "2", "4"]);
});

test("the edges table resolves asset ids to readable names", async ({ page }) => {
  const state = freshState();
  seedChain(state);
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();

  const edgeRows = page.locator(".card .tbl .tr");
  await expect(edgeRows.first()).toContainText("ERP System");
  await expect(edgeRows.first()).toContainText("erp-connector");
  await expect(edgeRows.first()).not.toContainText("a-source");
});

test("both Lineage tables show four rows and scroll for the rest, without a scrollbar", async ({ page }) => {
  const state = freshState();
  const { sink } = seedChain(state);
  // Four more hops that belong to the pipeline, so the edge list (and the asset list) is longer than four rows.
  let from = sink;
  for (let i = 1; i <= 4; i++) {
    const next = { id: `a-extra-${i}`, org_id: "org_test", asset_type: "kafka_topic", name: `extra.topic.${i}`, qualified_name: `org_test:topic:extra-${i}` };
    state.lineageAssets.push(next);
    state.lineageEdges.push({ id: `ex${i}`, org_id: "org_test", from_asset_id: from.id, to_asset_id: next.id, edge_type: "EMITS_TO", pipeline_id: "pipe-1" });
    from = next;
  }
  await mockBackend(page, state);
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();

  const tables = page.locator(".tbody.rows-4");
  await expect(tables).toHaveCount(2);
  for (let i = 0; i < 2; i++) {
    const body = tables.nth(i);
    const rows = body.locator(".tr");
    expect(await rows.count()).toBeGreaterThan(4);                                                    // more than four rows exist…
    const geometry = await body.evaluate((e) => ({
      box: e.getBoundingClientRect().height,
      row: (e.querySelector(".tr") as HTMLElement).getBoundingClientRect().height,
      scrolls: e.scrollHeight > e.clientHeight,
      barHidden: getComputedStyle(e).scrollbarWidth === "none" && getComputedStyle(e, "::-webkit-scrollbar").display === "none",
    }));
    expect(geometry.scrolls).toBe(true);                                                              // …so it scrolls…
    expect(Math.round(geometry.box / geometry.row)).toBe(4);                                          // …showing exactly four at a time…
    expect(geometry.barHidden).toBe(true);                                                            // …with no bar.
    const lastRowInsideBox = await body.evaluate((e) => {                                             // scrolled to the end, the last row sits inside the box
      e.scrollTop = e.scrollHeight;
      const last = (e.querySelectorAll(".tr")[e.querySelectorAll(".tr").length - 1] as HTMLElement).getBoundingClientRect();
      const box = e.getBoundingClientRect();
      return last.top >= box.top - 1 && last.bottom <= box.bottom + 1;
    });
    expect(lastRowInsideBox).toBe(true);
  }
});

test("the Lineage title and counts stay in place while the rest of the page scrolls", async ({ page }) => {
  const state = freshState();
  const { sink } = seedChain(state);
  let from = sink;
  for (let i = 1; i <= 4; i++) {
    const next = { id: `a-extra-${i}`, org_id: "org_test", asset_type: "kafka_topic", name: `extra.topic.${i}`, qualified_name: `org_test:topic:extra-${i}` };
    state.lineageAssets.push(next);
    state.lineageEdges.push({ id: `ex${i}`, org_id: "org_test", from_asset_id: from.id, to_asset_id: next.id, edge_type: "EMITS_TO", pipeline_id: "pipe-1" });
    from = next;
  }
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 560 });                    // short enough that the content cannot fit
  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();

  const top = async () => ({
    title: (await page.locator(".ph").boundingBox())!.y,
    counts: (await page.locator(".stats").boundingBox())!.y,
  });
  const before = await top();
  const scroller = page.locator(".page-scroll");
  expect(await scroller.evaluate((e) => e.scrollHeight > e.clientHeight)).toBe(true);        // there is more below than fits…
  await page.locator(".tbl").last().hover();
  await page.mouse.wheel(0, 600);
  await expect.poll(() => scroller.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);        // …and it scrolls…
  expect(await top()).toEqual(before);                                                       // …while the title and counts did not move
  expect(await page.locator(".cont").evaluate((e) => e.scrollTop)).toBe(0);                  // the page itself never scrolled
  expect(await scroller.evaluate((e) => getComputedStyle(e).scrollbarWidth === "none" && getComputedStyle(e, "::-webkit-scrollbar").display === "none")).toBe(true);
});

test("the asset list shows only the selected pipeline's assets, never the other pipelines' (and grows with an expansion)", async ({ page }) => {
  const state = freshState();
  seedChain(state);
  // A second pipeline whose assets must not appear while the first one is selected.
  state.pipelines.push({ id: "pipe-2", org_id: "org_test", name: "file-pipe", status: "running", topic_name: "t2", connector_name: "file-connector", mode: "stream", generated_config: {} });
  const otherConnector = { id: "b-connector", org_id: "org_test", asset_type: "connector", name: "file-connector", qualified_name: "org_test:connector:file-connector" };
  const otherTopic = { id: "b-topic", org_id: "org_test", asset_type: "kafka_topic", name: "raw.files", qualified_name: "org_test:topic:raw-files" };
  state.lineageAssets.push(otherConnector, otherTopic);
  state.lineageEdges.push({ id: "o1", org_id: "org_test", from_asset_id: otherConnector.id, to_asset_id: otherTopic.id, edge_type: "INGESTS_TO", pipeline_id: "pipe-2" });
  await mockBackend(page, state);

  await page.goto("/");
  await page.locator(".sn", { hasText: "Lineage" }).click();
  await expect(page.locator(".lgraph .lnode")).toHaveCount(5);

  const assetNames = () => page.locator(".tbl").last().locator(".tbody .tr .td:last-child").allTextContents();
  const count = (label: string) => page.locator(".sc").filter({ has: page.locator(".sc-l", { hasText: new RegExp(`^${label}$`) }) }).locator(".sc-v");
  await expect.poll(assetNames).toEqual(["ERP System", "erp-connector", "raw.customers", "Quality Processor", "prepared.customers"]);
  // The four counts are this pipeline's too (7 assets exist in the organisation; this pipeline shows 5: 1 source, 2 topics, 1 processor).
  await expect(count("Total assets")).toHaveText("5");
  await expect(count("Sources")).toHaveText("1");
  await expect(count("Topics")).toHaveText("2");
  await expect(count("Sinks/Proc")).toHaveText("1");
  expect(await page.locator(".tbl").last().innerText()).not.toContain("file-connector");       // the other pipeline's connector and topic are not listed
  expect(await page.locator(".tbl").last().innerText()).not.toContain("raw.files");

  await page.locator(".lnode", { hasText: "prepared.customers" }).getByRole("button", { name: /downstream/i }).click();
  await expect.poll(assetNames).toContain("Analytics Sink");                                    // what the user expands in the graph is listed too
  expect(await assetNames()).toHaveLength(6);
  await expect(count("Total assets")).toHaveText("6");                                           // the counts follow the expansion
  await expect(count("Sinks/Proc")).toHaveText("2");

  await page.getByRole("button", { name: "Select pipeline" }).click();
  await page.getByRole("option", { name: "file-pipe" }).click();                                 // switching pipeline switches the list
  await expect.poll(assetNames).toEqual(["file-connector", "raw.files"]);
  await expect(count("Total assets")).toHaveText("2");                                           // …and the counts above it
});
