/*
 * The Alerts page (pages/Alerts.tsx): failed / degraded pipelines and other trouble, raised by rules and sent to Slack or Teams channels
 * (app/api/routes/alerts.py). The /alerts endpoints are served from an in-memory store below, on top of the shared mock backend.
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { choose, freshState, mockBackend, rowAction, switchRole, type RoleName } from "./fixtures";

type Rec = Record<string, unknown>;

interface AlertsState {
  alerts: Rec[];
  rules: Rec[];
  channels: Rec[];
  /** Every call the page made: method, path, query, body. */
  calls: { method: string; path: string; query: URLSearchParams; body: Rec }[];
  /** What the next channel test answers. */
  testOk: boolean;
  /** A 422 the next rule / channel save answers with, then cleared. */
  saveError?: { field: string; message: string };
}

const T0 = "2026-10-30T08:00:00Z";
const KINDS = [
  { kind: "pipeline_failed", label: "Pipeline failed", description: "The pipeline's connector has failed.", threshold_hint: "Always 1.", uses_window: false, uses_min_runs: false, max_threshold: 1, default_window_seconds: 0 },
  { kind: "pipeline_degraded", label: "Pipeline degraded", description: "Some tasks failed.", threshold_hint: "Always 1.", uses_window: false, uses_min_runs: false, max_threshold: 1, default_window_seconds: 0 },
  { kind: "operation_failed", label: "Deploy or action failed", description: "A deploy, pause or restart failed.", threshold_hint: "Failed actions in the window.", uses_window: true, uses_min_runs: false, max_threshold: 1000, default_window_seconds: 3600 },
  { kind: "error_threshold_paused", label: "Paused by error threshold", description: "Paused for too many errors.", threshold_hint: "Pauses in the window.", uses_window: true, uses_min_runs: false, max_threshold: 1000, default_window_seconds: 3600 },
  { kind: "dlq_errors", label: "Dead-letter errors", description: "Records sent to the dead-letter queue.", threshold_hint: "Records in the window.", uses_window: true, uses_min_runs: false, max_threshold: 1_000_000, default_window_seconds: 3600 },
  { kind: "quality_failures", label: "Quality check failures", description: "Records failing quality checks.", threshold_hint: "Records in the window.", uses_window: true, uses_min_runs: false, max_threshold: 1_000_000, default_window_seconds: 3600 },
  { kind: "runtime_unreachable", label: "Connector state unknown", description: "Kafka Connect cannot be reached.", threshold_hint: "Minutes.", uses_window: false, uses_min_runs: false, max_threshold: 1440, default_window_seconds: 0 },
  { kind: "schema_drift_pending", label: "Schema change waiting", description: "A schema change waits for approval.", threshold_hint: "Hours.", uses_window: false, uses_min_runs: false, max_threshold: 720, default_window_seconds: 0 },
];

function alert(id: string, extra: Rec = {}): Rec {
  return {
    id, rule_id: "r-failed", rule_name: "pipeline-failed", kind: "pipeline_failed", severity: "high", pipeline_id: "p1", pipeline_name: "orders-cdc",
    direction: "SOURCE", summary: "Pipeline 'orders-cdc' (source) is failed: connector task crashed", state: "open", delivery: "sent",
    raised_at: T0, sent_at: T0, resolved_at: null, last_action: "raised", last_actor: "system", last_at: T0, ...extra,
  };
}
function rule(id: string, extra: Rec = {}): Rec {
  return { id, name: id, kind: "pipeline_failed", severity: "high", threshold: 1, window_seconds: 0, min_runs: 1, scope_pipeline_id: null,
    scope_pipeline_name: null, enabled: true, created_by: "system", created_at: T0, updated_at: T0, ...extra };
}
function channel(id: string, extra: Rec = {}): Rec {
  return { id, name: id, kind: "slack", url_hint: "hooks.slack.com/…/XXXX", min_severity: "low", scope_pipeline_id: null, scope_pipeline_name: null,
    enabled: true, last_attempt_at: null, last_ok: null, last_detail: null, created_by: "admin", created_at: T0, updated_at: T0, ...extra };
}

const json = (route: Route, status: number, body?: unknown) =>
  route.fulfill({ status, contentType: "application/json", body: body === undefined ? "" : JSON.stringify(body) });

async function mockAlerts(page: Page, init: Partial<AlertsState> = {}): Promise<AlertsState> {
  const s: AlertsState = { alerts: [], rules: [], channels: [], calls: [], testOk: true, ...init };
  const state = freshState();
  state.pipelines.push(
    { id: "p1", org_id: "org_test", name: "orders-cdc", direction: "SOURCE", status: "failed", topic_name: "t1", generated_config: {} },
    { id: "p2", org_id: "org_test", name: "orders-sink", direction: "SINK", status: "running", generated_config: {} },
  );
  await mockBackend(page, state);
  await page.route(/\/api\/v1\/dataconnect\/alerts(\/|\?|$)/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^.*\/api\/v1\/dataconnect/, "");
    const method = req.method();
    const body = (req.postDataJSON() as Rec | null) ?? {};
    s.calls.push({ method, path, query: url.searchParams, body });
    const failSave = () => {
      const e = s.saveError;
      s.saveError = undefined;
      return e ? json(route, 422, { code: "validation_error", message: e.message, retryable: false, field_errors: [{ ...e, code: "invalid" }] }) : null;
    };
    const live = (st: string) => s.alerts.filter((a) => a.state === st && (st !== "open" || a.delivery === "sent"));

    if (path === "/alerts/counts") {
      return json(route, 200, { open: live("open").length, acknowledged: live("acknowledged").length, resolved: live("resolved").length,
        undelivered: s.alerts.filter((a) => a.state === "open" && a.delivery !== "sent").length });
    }
    if (path === "/alerts" && method === "GET") {
      const st = url.searchParams.get("state") ?? "open";
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 10);
      const all = live(st);
      return json(route, 200, { items: all.slice(offset, offset + limit), total: all.length,
        undelivered: s.alerts.filter((a) => a.state === "open" && a.delivery !== "sent").length });
    }
    if (path === "/alerts/rules/kinds") return json(route, 200, KINDS);
    if (path === "/alerts/rules" && method === "GET") return json(route, 200, s.rules);
    if (path === "/alerts/rules" && method === "POST") {
      const f = failSave(); if (f) return f;
      const r = rule(`r-${s.rules.length + 1}`, body);
      s.rules.push(r);
      return json(route, 201, r);
    }
    if (path === "/alerts/rules/run") return json(route, 200, { ran: true, detail: "ok", raised: 1, cleared: 0, delivered: 1, sources: [] });
    let m = path.match(/^\/alerts\/rules\/([^/]+)$/);
    if (m && method === "PATCH") {
      const f = failSave(); if (f) return f;
      const r = s.rules.find((x) => x.id === m![1])!;
      Object.assign(r, body);
      return json(route, 200, r);
    }
    if (m && method === "DELETE") { s.rules = s.rules.filter((x) => x.id !== m![1]); return route.fulfill({ status: 204, body: "" }); }
    if (path === "/alerts/channels" && method === "GET") return json(route, 200, s.channels);
    if (path === "/alerts/channels" && method === "POST") {
      const f = failSave(); if (f) return f;
      const { webhook_url: _url, ...rest } = body;
      const c = channel(`c-${s.channels.length + 1}`, rest);
      s.channels.push(c);
      return json(route, 201, c);
    }
    m = path.match(/^\/alerts\/channels\/([^/]+)\/test$/);
    if (m) {
      const c = s.channels.find((x) => x.id === m![1])!;
      Object.assign(c, { last_ok: s.testOk, last_attempt_at: T0, last_detail: s.testOk ? "sent" : "HTTP 404 from the webhook" });
      return json(route, 200, { ok: s.testOk, detail: c.last_detail, channel: c });
    }
    m = path.match(/^\/alerts\/channels\/([^/]+)$/);
    if (m && method === "PATCH") {
      const f = failSave(); if (f) return f;
      const c = s.channels.find((x) => x.id === m![1])!;
      const { webhook_url: _url, ...rest } = body;
      Object.assign(c, rest);
      return json(route, 200, c);
    }
    if (m && method === "DELETE") { s.channels = s.channels.filter((x) => x.id !== m![1]); return route.fulfill({ status: 204, body: "" }); }
    m = path.match(/^\/alerts\/([^/]+)\/(acknowledge|resolve)$/);
    if (m) {
      const a = s.alerts.find((x) => x.id === m![1])!;
      Object.assign(a, { state: m[2] === "acknowledge" ? "acknowledged" : "resolved", last_action: m[2] === "acknowledge" ? "acknowledged" : "resolved", last_actor: "admin" });
      return json(route, 200, { ...a, events: [] });
    }
    m = path.match(/^\/alerts\/([^/]+)$/);
    if (m && method === "GET") {
      const a = s.alerts.find((x) => x.id === m![1]);
      return a ? json(route, 200, { ...a, events: [{ action: "raised", actor: "system", note: null, at: T0 }, { action: "sent", actor: "system", note: "to data-oncall", at: T0 }] })
        : json(route, 404, { code: "not_found", message: "Alert not found", retryable: false, field_errors: null });
    }
    return json(route, 404, { code: "not_found", message: `no mock for ${method} ${path}`, retryable: false, field_errors: null });
  });
  return s;
}

async function openAlerts(page: Page, role?: RoleName) {
  await page.setViewportSize({ width: 1500, height: 900 });
  await page.goto("/");
  if (role) await switchRole(page, role);
  await page.locator(".sn", { hasText: "Alerts" }).click();
  await expect(page.getByRole("tablist", { name: "Alert views" })).toBeVisible();
}
const tab = (page: Page, name: string) => page.getByRole("tab", { name: new RegExp(`^${name}`) });
const toast = (page: Page) => page.locator(".toast").last();
const lastCall = (s: AlertsState, method: string, path: RegExp) => [...s.calls].reverse().find((c) => c.method === method && path.test(c.path));

// ── Alerts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

test("the sidebar and the tabs count the alerts; Open lists only open alerts sent to a channel", async ({ page }) => {
  await mockAlerts(page, { alerts: [
    alert("a1"), alert("a2", { pipeline_name: "orders-sink", rule_name: "high-dlq-errors", severity: "medium" }),
    alert("a3", { state: "acknowledged" }), alert("a4", { state: "resolved" }), alert("a5", { state: "resolved" }),
  ] });
  await openAlerts(page);
  await expect(page.locator(".sn", { hasText: "Alerts" }).locator(".sn-count")).toHaveText("2");
  await expect(page.getByRole("tab")).toHaveText(["Open2", "Acknowledged1", "Resolved2", "Rules", "Channels"]);
  await expect(tab(page, "Open")).toHaveAttribute("aria-selected", "true");
  const cards = page.locator(".al-card");
  await expect(cards).toHaveCount(2);
  await expect(cards.first()).toContainText("pipeline-failed");
  await expect(cards.first()).toContainText("orders-cdc");
  await expect(cards.first()).toContainText("connector task crashed");
  await expect(cards.nth(1)).toContainText("high-dlq-errors");
  await tab(page, "Resolved").click();
  await expect(cards).toHaveCount(2);
  await expect(page.locator(".al-intro").first()).toContainText("resolved by a person, or cleared by themselves");
});

test("no sidebar badge and an explaining empty state when nothing is open", async ({ page }) => {
  await mockAlerts(page);
  await openAlerts(page);
  await expect(page.locator(".sn-count")).toHaveCount(0);
  await expect(page.locator(".al-empty")).toContainText("No open alerts");
  await tab(page, "Acknowledged").click();
  await expect(page.locator(".al-empty")).toContainText("Nothing is acknowledged right now.");
});

test("alerts that reached no channel are counted in a note that leads to Channels", async ({ page }) => {
  await mockAlerts(page, { alerts: [alert("a1", { delivery: "no_channel" }), alert("a2", { delivery: "failed" })] });
  await openAlerts(page);
  const note = page.locator(".al-undelivered");
  await expect(note).toContainText("2 alerts reached no channel yet");
  await expect(page.locator(".al-card")).toHaveCount(0);
  await note.getByRole("button", { name: "Channels" }).click();
  await expect(tab(page, "Channels")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".al-empty")).toContainText("No channels yet");
});

test("an alert opens in the right-hand drawer with its history; acknowledging sends the note", async ({ page }) => {
  const s = await mockAlerts(page, { alerts: [alert("a1")] });
  await openAlerts(page);
  await page.getByRole("button", { name: "Open alert pipeline-failed for orders-cdc" }).click();
  const drawer = page.locator(".modal");
  await expect(drawer).toBeVisible();
  expect(await drawer.evaluate((e) => Math.round(e.getBoundingClientRect().right))).toBe(1500);      // pinned to the right edge, not centred
  await expect(drawer.locator(".al-history li")).toHaveCount(2);
  await expect(drawer.locator(".al-history")).toContainText("to data-oncall");
  await drawer.locator("#alert-note").fill("looking into it");
  await drawer.getByRole("button", { name: "Acknowledge" }).click();
  await expect(toast(page)).toContainText("Alert acknowledged");
  expect(lastCall(s, "POST", /\/acknowledge$/)?.body).toEqual({ note: "looking into it" });
  await expect(tab(page, "Acknowledged")).toContainText("1");
  await expect(drawer.getByRole("button", { name: "Acknowledge" })).toHaveCount(0);                  // acknowledged: only Resolve is left
  await drawer.getByRole("button", { name: "Resolve" }).click();
  await expect(toast(page)).toContainText("Alert resolved");
  await expect(drawer).toHaveCount(0);
  expect(lastCall(s, "POST", /\/resolve$/)?.body).toEqual({ note: null });
});

test("Open pipeline goes to the alert's pipeline", async ({ page }) => {
  await mockAlerts(page, { alerts: [alert("a1")] });
  await openAlerts(page);
  await page.locator(".al-card").first().click();
  await page.locator(".modal").getByRole("button", { name: "Open pipeline" }).click();
  await expect(page.locator(".modal")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Pipelines" }).or(page.getByText("orders-cdc").first())).toBeVisible();
  await expect(page.locator(".sn.on, .sn[aria-current=page]").first()).toContainText("Pipelines");
});

test("12 open alerts page by 10, and the date filters reach the request", async ({ page }) => {
  const s = await mockAlerts(page, { alerts: Array.from({ length: 12 }, (_, i) => alert(`a${i + 1}`, { rule_name: `rule-${i + 1}` })) });
  await openAlerts(page);
  await expect(page.locator(".al-card")).toHaveCount(10);
  await expect(page.locator(".al-pager")).toContainText("Showing 1–10 of 12 alerts");
  await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.locator(".al-card")).toHaveCount(2);
  await expect(page.locator(".al-pager")).toContainText("Showing 11–12 of 12 alerts");
  await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();

  await page.getByLabel("From date").fill("2026-10-01");
  await expect.poll(() => lastCall(s, "GET", /^\/alerts$/)?.query.get("from")).toBe("2026-10-01");
  expect(lastCall(s, "GET", /^\/alerts$/)?.query.get("offset")).toBe("0");                          // a new filter starts again at the first page
  await page.getByLabel("To date").fill("2026-10-30");
  await expect.poll(() => lastCall(s, "GET", /^\/alerts$/)?.query.get("to")).toBe("2026-10-30");
  await page.getByRole("button", { name: /Clear dates/ }).click();
  await expect.poll(() => lastCall(s, "GET", /^\/alerts$/)?.query.get("from")).toBe(null);
});

test("a refresh keeps the tab; arriving from the sidebar opens Open", async ({ page }) => {
  await mockAlerts(page, { rules: [rule("pipeline-failed")] });
  await openAlerts(page);
  await tab(page, "Rules").click();
  await page.reload();
  await expect(tab(page, "Rules")).toHaveAttribute("aria-selected", "true");
  await page.locator(".sn", { hasText: "Dashboard" }).click();
  await page.locator(".sn", { hasText: "Alerts" }).click();
  await expect(tab(page, "Open")).toHaveAttribute("aria-selected", "true");
});

test("the tabs move with the arrow keys", async ({ page }) => {
  await mockAlerts(page);
  await openAlerts(page);
  await tab(page, "Open").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(page, "Acknowledged")).toHaveAttribute("aria-selected", "true");
  await expect(tab(page, "Acknowledged")).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await expect(tab(page, "Channels")).toHaveAttribute("aria-selected", "true");
});

// ── Rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

test("the Rules table says in words when each rule fires; the switch turns a rule off", async ({ page }) => {
  const s = await mockAlerts(page, { rules: [
    rule("pipeline-failed"),
    rule("high-dlq-errors", { kind: "dlq_errors", severity: "medium", threshold: 100, window_seconds: 3600 }),
    rule("orders-tight", { kind: "quality_failures", threshold: 5, window_seconds: 900, scope_pipeline_id: "p1", scope_pipeline_name: "orders-cdc", enabled: false }),
  ] });
  await openAlerts(page);
  await tab(page, "Rules").click();
  const rows = page.locator(".tbl .tr");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("while the pipeline is failed");
  await expect(rows.nth(1)).toContainText("≥ 100 dead-letter records · 1 hour");
  await expect(rows.nth(2)).toContainText("≥ 5 failing records · 15 minutes");
  await expect(rows.nth(2)).toContainText("orders-cdc");
  await expect(rows.nth(0)).toContainText("all pipelines");
  await expect(rows.nth(2)).toHaveClass(/al-off/);

  await rows.nth(0).getByRole("switch").click();
  await expect.poll(() => lastCall(s, "PATCH", /\/rules\//)?.body).toEqual({ enabled: false });
  await expect(rows.nth(0).getByRole("switch")).toHaveAttribute("aria-checked", "false");
});

test("Run rules now reports what the check did", async ({ page }) => {
  await mockAlerts(page);
  await openAlerts(page);
  await tab(page, "Rules").click();
  await page.getByRole("button", { name: "Run rules now" }).click();
  await expect(toast(page)).toContainText("Rules checked: 1 raised, 0 cleared, 1 sent");
});

test("a new rule: state kinds fix the threshold, count kinds take a window, and the saved body is what was picked", async ({ page }) => {
  const s = await mockAlerts(page);
  await openAlerts(page);
  await tab(page, "Rules").click();
  await page.getByRole("button", { name: "+ New rule" }).click();
  const drawer = page.locator(".modal");
  await expect(drawer.locator("#rule-threshold")).toBeDisabled();                                   // "pipeline failed" is a state: always 1
  await expect(drawer.locator("#rule-window")).toHaveCount(0);
  const create = drawer.getByRole("button", { name: "Create rule" });
  await expect(create).toBeDisabled();                                                              // no name yet

  await drawer.locator("#rule-name").fill("orders-dlq");
  await choose(drawer.locator("#rule-kind"), "dlq_errors");
  await expect(drawer).toContainText("Records sent to the dead-letter queue.");                      // the backend's own description
  await expect(drawer.locator("#rule-threshold")).toBeEnabled();
  await drawer.locator("#rule-threshold").fill("0");
  await expect(drawer.locator("#rule-threshold-range")).toContainText("1 to 1,000,000");
  await expect(create).toBeDisabled();
  await drawer.locator("#rule-threshold").fill("250");
  await choose(drawer.locator("#rule-window"), "900");
  await choose(drawer.locator("#rule-severity"), "high");
  await choose(drawer.locator("#rule-scope"), "p2");
  await create.click();
  await expect(toast(page)).toContainText('Rule "orders-dlq" created');
  expect(lastCall(s, "POST", /^\/alerts\/rules$/)?.body).toEqual({
    name: "orders-dlq", kind: "dlq_errors", severity: "high", threshold: 250, window_seconds: 900, min_runs: 1,
    scope_pipeline_id: "p2", scope_pipeline_name: "orders-sink", enabled: true,
  });
  await expect(page.locator(".tbl .tr")).toContainText(["≥ 250 dead-letter records · 15 minutes"]);
});

test("a rule the server refuses shows the reason under the field, and the drawer stays open", async ({ page }) => {
  const s = await mockAlerts(page, { rules: [rule("pipeline-failed")] });
  s.saveError = { field: "name", message: "A rule named pipeline-failed already exists." };
  await openAlerts(page);
  await tab(page, "Rules").click();
  await page.getByRole("button", { name: "+ New rule" }).click();
  const drawer = page.locator(".modal");
  await drawer.locator("#rule-name").fill("pipeline-failed");
  await drawer.getByRole("button", { name: "Create rule" }).click();
  await expect(drawer.locator("#rule-name-error")).toHaveText("A rule named pipeline-failed already exists.");
  await expect(drawer.locator("#rule-name")).toHaveAttribute("aria-invalid", "true");
});

test("editing a rule PATCHes it; deleting asks first", async ({ page }) => {
  const s = await mockAlerts(page, { rules: [rule("high-dlq-errors", { kind: "dlq_errors", severity: "medium", threshold: 100, window_seconds: 3600 })] });
  await openAlerts(page);
  await tab(page, "Rules").click();
  const row = page.locator(".tbl .tr").first();
  await rowAction(row, "Edit");
  const drawer = page.locator(".modal");
  await expect(drawer.locator("#rule-threshold")).toHaveValue("100");
  await drawer.locator("#rule-threshold").fill("500");
  await drawer.getByRole("button", { name: "Save changes" }).click();
  await expect(toast(page)).toContainText('Rule "high-dlq-errors" saved');
  expect(lastCall(s, "PATCH", /\/rules\//)?.body).toMatchObject({ threshold: 500, window_seconds: 3600, scope_pipeline_id: "" });

  page.once("dialog", (d) => d.dismiss());
  await rowAction(row, "Delete");
  expect(lastCall(s, "DELETE", /\/rules\//)).toBeUndefined();                                       // cancelled: nothing deleted
  page.once("dialog", (d) => d.accept());
  await rowAction(row, "Delete");
  await expect(page.locator(".tbl .empty")).toContainText("No rules");
});

// ── Channels ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

test("adding a channel needs a name and a URL; the URL is sent but never shown again", async ({ page }) => {
  const s = await mockAlerts(page);
  await openAlerts(page);
  await tab(page, "Channels").click();
  await page.getByRole("button", { name: "+ Add channel" }).click();
  const drawer = page.locator(".modal");
  const add = drawer.getByRole("button", { name: "Add channel" });
  await drawer.locator("#channel-name").fill("data-oncall");
  await expect(add).toBeDisabled();                                                                 // no URL yet
  await drawer.getByRole("button", { name: "Microsoft Teams" }).click();
  await expect(drawer.getByRole("button", { name: "Microsoft Teams" })).toHaveAttribute("aria-pressed", "true");
  await drawer.locator("#channel-url").fill("https://example.webhook.office.com/abc");
  await choose(drawer.locator("#channel-severity"), "high");
  await choose(drawer.locator("#channel-scope"), "p1");
  await add.click();
  await expect(toast(page)).toContainText('Channel "data-oncall" added');
  expect(lastCall(s, "POST", /^\/alerts\/channels$/)?.body).toEqual({
    name: "data-oncall", kind: "teams", min_severity: "high", enabled: true, scope_pipeline_id: "p1", scope_pipeline_name: "orders-cdc",
    webhook_url: "https://example.webhook.office.com/abc",
  });
  const card = page.getByLabel("Channel data-oncall");
  await expect(card).toContainText("Teams");
  await expect(card).toContainText("high only");
  await expect(card).toContainText("orders-cdc");
  await expect(card).not.toContainText("example.webhook.office.com/abc");
});

test("editing a channel with the URL left blank keeps the stored one", async ({ page }) => {
  const s = await mockAlerts(page, { channels: [channel("data-oncall")] });
  await openAlerts(page);
  await tab(page, "Channels").click();
  await rowAction(page.getByLabel("Channel data-oncall"), "Edit");
  const drawer = page.locator(".modal");
  await expect(drawer.locator("label[for=channel-url]")).toContainText("leave blank to keep the current one");
  await drawer.getByRole("switch", { name: "Enabled" }).click();
  await drawer.getByRole("button", { name: "Save changes" }).click();
  await expect(toast(page)).toContainText('Channel "data-oncall" saved');
  const sent = lastCall(s, "PATCH", /\/channels\//)?.body ?? {};
  expect(sent).not.toHaveProperty("webhook_url");
  expect(sent).toMatchObject({ enabled: false });
  await expect(page.getByLabel("Channel data-oncall")).toContainText("off");
});

test("a channel's URL error from the server shows under the URL field", async ({ page }) => {
  const s = await mockAlerts(page);
  s.saveError = { field: "webhook_url", message: "The webhook host does not resolve to a public address." };
  await openAlerts(page);
  await tab(page, "Channels").click();
  await page.getByRole("button", { name: "+ Add channel" }).click();
  const drawer = page.locator(".modal");
  await drawer.locator("#channel-name").fill("x");
  await drawer.locator("#channel-url").fill("https://hooks.slack.com/services/a");
  await drawer.getByRole("button", { name: "Add channel" }).click();
  await expect(drawer.locator("#channel-webhook_url-error")).toContainText("does not resolve to a public address");
});

test("Send test reports success and failure, and the card shows the last send", async ({ page }) => {
  const s = await mockAlerts(page, { channels: [channel("data-oncall")] });
  await openAlerts(page);
  await tab(page, "Channels").click();
  const card = page.getByLabel("Channel data-oncall");
  await expect(card).toContainText("not used yet");
  await card.getByRole("button", { name: "Send test" }).click();
  await expect(toast(page)).toContainText("Test message sent to data-oncall");
  await expect(card).toContainText("last send ok");
  s.testOk = false;
  await card.getByRole("button", { name: "Send test" }).click();
  await expect(toast(page)).toContainText("Test to data-oncall failed: HTTP 404 from the webhook");
  await expect(card).toContainText("last send failed");
});

test("removing a channel asks first", async ({ page }) => {
  await mockAlerts(page, { channels: [channel("data-oncall")] });
  await openAlerts(page);
  await tab(page, "Channels").click();
  page.once("dialog", (d) => { expect(d.message()).toContain('Remove channel "data-oncall"'); return d.accept(); });
  await rowAction(page.getByLabel("Channel data-oncall"), "Remove data-oncall");
  await expect(toast(page)).toContainText('Removed channel "data-oncall"');
  await expect(page.locator(".al-empty")).toContainText("No channels yet");
});

// ── Roles ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

test("a Viewer sees alerts, rules and channels but can change nothing", async ({ page }) => {
  await mockAlerts(page, { alerts: [alert("a1")], rules: [rule("pipeline-failed")], channels: [channel("data-oncall")] });
  await openAlerts(page, "Viewer");
  await page.locator(".al-card").first().click();
  const drawer = page.locator(".modal");
  await expect(drawer.getByRole("button", { name: "Open pipeline" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: /Acknowledge|Resolve/ })).toHaveCount(0);
  await expect(drawer.locator("#alert-note")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await tab(page, "Rules").click();
  await expect(page.getByRole("button", { name: "+ New rule" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run rules now" })).toHaveCount(0);
  await expect(page.locator(".tbl .tr").first().getByRole("switch")).toBeDisabled();
  await expect(page.getByRole("button", { name: /^Actions for / })).toHaveCount(0);
  await tab(page, "Channels").click();
  await expect(page.getByRole("button", { name: "+ Add channel" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send test" })).toHaveCount(0);
});

test("an Integrator can acknowledge and resolve, but not manage rules or channels", async ({ page }) => {
  await mockAlerts(page, { alerts: [alert("a1")], rules: [rule("pipeline-failed")] });
  await openAlerts(page, "Integrator");
  await page.locator(".al-card").first().click();
  await expect(page.locator(".modal").getByRole("button", { name: "Acknowledge" })).toBeVisible();
  await expect(page.locator(".modal").getByRole("button", { name: "Resolve" })).toBeVisible();
  await page.keyboard.press("Escape");
  await tab(page, "Rules").click();
  await expect(page.getByRole("button", { name: "+ New rule" })).toHaveCount(0);
  await tab(page, "Channels").click();
  await expect(page.getByRole("button", { name: "+ Add channel" })).toHaveCount(0);
});
