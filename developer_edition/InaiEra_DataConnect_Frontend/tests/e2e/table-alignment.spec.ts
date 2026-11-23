/*
 * In every table a value sits directly under its heading. The first column (the name) is flush left, so its first character sits on the same x as the first
 * character of its heading. Every other column is centred, heading and value alike, so the MIDDLE of the value sits on the middle of the heading. A pill
 * leading a cell counts by its text. Centres are compared to within 3px: a date that is a little wider than its column hangs out of it, and the two sides
 * round differently. Action columns are centred as well, and are not part of this check.
 */
import { test, expect, type Page } from "@playwright/test";
import { freshState, mockBackend } from "./fixtures";

const at = "2026-10-20T10:00:00Z";

async function seeded(page: Page) {
  const state = freshState();
  state.connections.push({ id: "c1", org_id: "o", name: "shop-db", source_type: "postgresql", connection_config: { hostname: "db", database: "shop" }, status: "active", created_at: at });
  state.pipelines.push({ id: "p1", org_id: "o", name: "orders-cdc", status: "running", topic_name: "t", connector_name: "c", mode: "cdc", generated_config: {}, direction: "SOURCE", created_at: at, connection_profile_id: "c1", schema_version_id: "s1", transform_policy_id: "t1" });
  state.schemas.push({ id: "s1", org_id: "o", domain: "crm", source_system: "sf", entity: "orders", version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] }, created_at: at });
  state.qualityPolicies.push({ id: "q1", org_id: "o", name: "orders-rules", rules: [], on_failure: "log", quarantine_sink: "kafka", created_at: at });
  state.transforms.push({ id: "t1", org_id: "o", name: "mask-pii", description: "d", transforms: [], origin: "custom", created_at: at });
  state.audit.push({ id: "a1", action: "created", resource_type: "pipeline", resource_id: "p1", actor: "me", created_at: at, before_state: null, after_state: null });
  const asset = (id: string, type: string, name: string) => ({ id, org_id: "o", asset_type: type, name, qualified_name: `o:${type}:${name}` });
  state.lineageAssets.push(asset("a-src", "source", "ERP"), asset("a-con", "connector", "erp-connector"));
  state.lineageEdges.push({ id: "e1", org_id: "o", from_asset_id: "a-src", to_asset_id: "a-con", edge_type: "CONNECTS_TO", pipeline_id: "p1" });
  await mockBackend(page, state);
  await page.goto("/");
}

/** For each table on screen: [heading, how far (px) the cell's content sits right of the heading's. The first column is compared by its left edge, every other one by
 * its middle.] Action columns are skipped. */
async function offsets(page: Page) {
  return page.evaluate(() => {
    /** The left and right edge of everything visible a cell draws: its text and its icons. */
    const ink = (el: Element): [number, number] | null => {
      let left = Infinity, right = -Infinity;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        let box: DOMRect | null = null;
        if (n.nodeType === Node.TEXT_NODE) {
          if (!n.textContent?.trim()) continue;
          const r = document.createRange(); r.selectNodeContents(n); box = r.getBoundingClientRect();
        } else if ((n as Element).tagName.toLowerCase() === "svg") {
          box = (n as Element).getBoundingClientRect();
        }
        if (box && box.width) { left = Math.min(left, box.left); right = Math.max(right, box.right); }
      }
      return left === Infinity ? null : [left, right];
    };
    const out: [string, number][] = [];
    document.querySelectorAll(".thead").forEach((head) => {
      const row = head.parentElement?.querySelector(".tbody .tr");
      if (!row) return;
      [...head.children].forEach((h, i) => {
        const label = h.textContent?.trim() ?? "";
        const cell = row.children[i];
        if (!cell || !label || label === "Actions") return;
        const a = ink(h), b = ink(cell);
        if (a === null || b === null) return;
        // A value wider than the whole cell (a very narrow window, a long id in the ellipsis cell) is cut or starts at the cell's left edge by design,
        // so it is not centred: it is not part of this check.
        if (i > 0 && b[1] - b[0] > cell.getBoundingClientRect().width) return;
        const dx = i === 0 ? b[0] - a[0] : (b[0] + b[1]) / 2 - (a[0] + a[1]) / 2;
        out.push([label.replace(/[▲▼⇅\s]+$/, ""), Math.round(dx * 10) / 10]);
      });
    });
    return out;
  });
}

async function check(page: Page, nav: string, expectColumns: string[]) {
  await page.locator(".sn", { hasText: nav }).first().click();
  await expect(page.locator(".tbody .tr").first()).toBeVisible();
  const found = await offsets(page);
  expect(found.map(([label]) => label), `${nav}: columns checked`).toEqual(expect.arrayContaining(expectColumns));
  for (const [label, dx] of found) expect(Math.abs(dx), `${nav} › ${label}: ${dx}px off its heading`).toBeLessThanOrEqual(3);
}

test("Pipelines, Schemas, Quality, Transforms, Audit and Lineage: every value sits under its heading (first column left, the others centred)", async ({ page }) => {
  await seeded(page);
  await check(page, "Pipelines", ["Name", "Created at", "Status", "Topic", "Mode"]);
  await check(page, "Schemas", ["Contract", "Created at", "Pipeline Name", "Status", "Version"]);
  await check(page, "Quality", ["Quality Check Name", "Created at", "Rules", "On failure"]);
  await check(page, "Transforms", ["Chain Name", "Created at", "Type", "Pipeline Name", "SMTs"]);
  await check(page, "Audit", ["When", "Actor", "Action", "Resource"]);
  await check(page, "Lineage", ["Type", "Qualified name"]);
});

test("Connections and the Dashboard line up too", async ({ page }) => {
  await seeded(page);
  await page.locator(".sn", { hasText: "Connections" }).first().click();
  await page.locator(".conn-grid >> text=postgresql").first().click();
  await expect(page.locator(".tbody .tr").first()).toBeVisible();
  for (const [label, dx] of await offsets(page)) expect(Math.abs(dx), `Connections › ${label}: ${dx}px`).toBeLessThanOrEqual(3);
  await page.locator(".sn", { hasText: "Dashboard" }).first().click();
  await expect(page.locator(".tbody .tr").first()).toBeVisible();
  const dash = await offsets(page);
  expect(dash.map(([l]) => l)).toEqual(expect.arrayContaining(["Pipeline", "Type", "State"]));
  for (const [label, dx] of dash) expect(Math.abs(dx), `Dashboard › ${label}: ${dx}px`).toBeLessThanOrEqual(3);
});

test("the heading row and a data row use the same left edge, so the columns cannot drift apart", async ({ page }) => {
  await seeded(page);
  await page.locator(".sn", { hasText: "Pipelines" }).first().click();
  await expect(page.locator(".tbody .tr").first()).toBeVisible();
  const [head, row] = await page.evaluate(() => [".thead", ".tbody .tr"].map((s) => {
    const el = document.querySelector(s)!; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return { left: r.left, width: r.width, content: r.width - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth) };
  }));
  expect(Math.abs(head!.content - row!.content)).toBeLessThan(0.5);                              // the same width for the columns to share out
});

// A very long contract name must not run across the neighbouring columns: it is cut at the Contract column's edge with "…", and only a cut name
// shows the whole name on hover (a name that fits shows nothing).
test("Schemas: a long contract name is cut with an ellipsis inside its column, with the full name on hover", async ({ page }) => {
  const long = `gap-sch.manual.gap_sch_${"e".repeat(120)}`;
  const state = freshState();
  state.schemas.push(
    { id: "s-long", org_id: "o", domain: "gap-sch", source_system: "manual", entity: `gap_sch_${"e".repeat(120)}`, version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] }, created_at: new Date().toISOString() },
    { id: "s-short", org_id: "o", domain: "crm", source_system: "sf", entity: "orders", version: "v1", schema_format: "json_schema", compatibility: "BACKWARD", status: "active", schema_definition: { fields: [] }, created_at: new Date().toISOString() },
  );
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 800 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Schemas" }).click();
  const row = page.locator(".tr", { hasText: "gap-sch.manual" });
  const name = row.locator(".cell-cut").first();
  await expect(name).toBeVisible();
  const m = await name.evaluate((e) => { const cell = e.closest(".td")!.getBoundingClientRect(), r = e.getBoundingClientRect(); return { right: r.right, cellRight: cell.right, cut: e.scrollWidth > e.clientWidth, overflow: getComputedStyle(e).textOverflow }; });
  expect(m.cut).toBe(true);                                                  // it is longer than it is shown…
  expect(m.overflow).toBe("ellipsis");                                       // …and is cut with "…"
  expect(m.right).toBeLessThanOrEqual(m.cellRight + 1);                      // inside its own column
  await expect(name).toHaveAttribute("title", long);                         // a cut name carries the whole name for hover
  // the hover text itself wraps inside the window, however long the name is
  await name.hover();
  const tip = page.locator(".fast-tip");
  await expect(tip).toBeVisible();
  expect(await tip.evaluate((e) => e.scrollWidth <= e.clientWidth + 1)).toBe(true);                // nothing runs out of the tooltip's box
  // the next column's value starts where its heading does — nothing was pushed or overlapped
  const created = await row.locator(".tdm").first().boundingBox(), head = await page.locator(".thead .th").nth(1).boundingBox();
  expect(Math.abs(created!.x - head!.x)).toBeLessThanOrEqual(2);
  // a name that fits is left alone: no tooltip attribute, and nothing appears when the pointer rests on it
  const short = page.locator(".tr", { hasText: "crm.sf.orders" }).locator(".cell-cut").first();
  expect(await short.evaluate((e) => e.scrollWidth > e.clientWidth)).toBe(false);
  await expect(short).not.toHaveAttribute("title", /.+/);
  await short.hover();
  await page.waitForTimeout(500);                                                                   // longer than the tooltip's delay
  await expect(page.locator(".fast-tip")).toHaveCount(0);
  // ...and the contract id, which fits, shows nothing either
  const shortId = page.locator(".tr", { hasText: "crm.sf.orders" }).locator(".cell-cut").nth(1);
  await expect(shortId).not.toHaveAttribute("title", /.+/);
});

test("Pipelines: a long pipeline name and topic are cut inside their columns, with the full text on hover; short ones get no tooltip", async ({ page }) => {
  const longName = `groc-pg-quality-sink-${"x".repeat(80)}`;
  const longTopic = `raw-groc-pg-groceries_customers-${"y".repeat(60)}-batch-v1`;
  const state = freshState();
  const now = new Date().toISOString();
  state.pipelines.push(
    { id: "p-long", org_id: "o", name: longName, status: "running", topic_name: longTopic, connector_name: "c1", mode: "batch", generated_config: {}, direction: "SOURCE", created_at: now },
    { id: "p-short", org_id: "o", name: "orders", status: "running", topic_name: "t1", connector_name: "c2", mode: "batch", generated_config: {}, direction: "SOURCE", created_at: now },
  );
  await mockBackend(page, state);
  await page.setViewportSize({ width: 1300, height: 800 });
  await page.goto("/");
  await page.locator(".sn", { hasText: "Pipelines" }).click();
  const row = page.locator(".tr", { hasText: "groc-pg-quality-sink" });
  const [name, topic] = [row.locator(".cell-cut").nth(0), row.locator(".cell-cut").nth(1)];
  await expect(name).toHaveAttribute("title", longName);                     // cut: the whole name on hover
  await expect(topic).toHaveAttribute("title", longTopic);                   // and the whole topic
  for (const cell of [name, topic]) {
    const m = await cell.evaluate((e) => ({ cut: e.scrollWidth > e.clientWidth, overflow: getComputedStyle(e).textOverflow }));
    expect(m).toEqual({ cut: true, overflow: "ellipsis" });
  }
  const short = page.locator(".tr", { hasText: "orders" }).locator(".cell-cut");
  await expect(short.nth(0)).not.toHaveAttribute("title", /.+/);              // fits: no tooltip
  await expect(short.nth(1)).not.toHaveAttribute("title", /.+/);
});

// A long value (an Azure host, an email address: no space or hyphen to break at) must stay inside its own column instead of running into the next one.
test("a long host and a long actor stay inside their own columns", async ({ page }) => {
  const state = freshState();
  const host = "psql-inaiera-dp-dev.postgres.database.azure.com";
  state.connections.push({ id: "c1", org_id: "o", name: "postgres_source", source_type: "postgresql", connection_config: { hostname: host, port: 5432, database: "sales_demo" }, status: "active", created_at: at });
  state.audit.push({ id: "a1", action: "created", resource_type: "connection", resource_id: "0fa1ebc1-2b5a-4b47-8e4f-7824fcb3e9e1", actor: "tamma.lithesh@aadhyaanalytics.com", created_at: at, before_state: null, after_state: null });
  await mockBackend(page, state);
  await page.goto("/");
  const overflowing = () => page.evaluate(() => [...document.querySelectorAll(".tbody .tr > *")]
    .filter((c) => !c.classList.contains("acts") && c.scrollWidth > c.clientWidth + 1)
    .map((c) => `${(c as HTMLElement).innerText.slice(0, 40)} (${c.scrollWidth} > ${c.clientWidth})`));

  await page.locator(".sn", { hasText: "Connections" }).first().click();
  await page.locator(".conn-grid >> text=postgresql").first().click();
  await expect(page.locator(".tbody .tr").first()).toContainText(host);
  expect(await overflowing(), "Connections: a cell's text runs out of its cell").toEqual([]);

  await page.locator(".sn", { hasText: "Audit" }).first().click();
  await expect(page.locator(".tbody .tr").first()).toContainText("aadhyaanalytics.com");
  expect(await overflowing(), "Audit: a cell's text runs out of its cell").toEqual([]);
});
