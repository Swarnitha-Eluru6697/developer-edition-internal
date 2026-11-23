/** The alert drawers and tabs: every form opens on the RIGHT (never a centred dialog), channel / rule validation, the correct test icon,
 *  acknowledge / resolve, open the job. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import uiReducer from "../../src/store/uiSlice";
import { alert, channel, KINDS, rule } from "../helpers/alertsMocks";
import { pickSearchSelect } from "../helpers/searchSelect";

const api = vi.hoisted(() => ({
  createChannel: [] as unknown[], updateChannel: [] as unknown[], testChannel: [] as unknown[], deleteChannel: [] as unknown[],
  createRule: [] as unknown[], updateRule: [] as unknown[], deleteRule: [] as unknown[], ack: [] as unknown[], resolve: [] as unknown[],
  fail: null as null | (() => unknown), testResult: { ok: true, detail: "delivered" } as Record<string, unknown>,
  confirm: true, detail: null as unknown,
}));
vi.mock("../../src/features/alerts/api", async () => {
  const { mutation: m } = await import("../helpers/alertsMocks");
  return {
    useCreateAlertChannelMutation: m(api.createChannel, (a: { name: string }) => ({ id: "new", name: a.name }), () => api.fail?.()),
    useUpdateAlertChannelMutation: m(api.updateChannel, {}, () => api.fail?.()),
    useTestAlertChannelMutation: m(api.testChannel, () => ({ ...api.testResult, channel: {} })),
    useDeleteAlertChannelMutation: m(api.deleteChannel, undefined),
    useCreateAlertRuleMutation: m(api.createRule, {}, () => api.fail?.()),
    useUpdateAlertRuleMutation: m(api.updateRule, {}, () => api.fail?.()),
    useDeleteAlertRuleMutation: m(api.deleteRule, undefined),
    useAcknowledgeAlertMutation: m(api.ack, {}),
    useResolveAlertMutation: m(api.resolve, {}),
    useGetAlertQuery: (id: string) => ({ data: api.detail ?? { ...alert({ id }), events: [{ action: "raised", actor: "rule sweep", note: null, at: "2026-10-29T09:42:00Z" }, { action: "sent to #oncall", actor: "system", note: null, at: "2026-10-29T09:42:05Z" }] } }),
  };
});
vi.mock("../../src/shared/ui/dialogStore", () => ({ askConfirm: vi.fn(async () => api.confirm) }));

import { AlertDetailDrawer } from "../../src/features/alerts/AlertDetailDrawer";
import { ChannelDrawer } from "../../src/features/alerts/ChannelDrawer";
import { ChannelsTab } from "../../src/features/alerts/ChannelsTab";
import { RuleDrawer } from "../../src/features/alerts/RuleDrawer";
import { RulesTab } from "../../src/features/alerts/RulesTab";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const JOBS = [{ key: "j1", name: "customers", kind: "dbt" as const }, { key: "f1", name: "orders-stream", kind: "flink" as const }];

function mount(node: React.ReactElement) {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: node })));
  const dialog = () => host.querySelector<HTMLElement>("[role=dialog]");
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === text);
  const input = (label: string) => host.querySelector<HTMLInputElement>(`input[aria-label='${label}']`)!;
  const type = (label: string, value: string) => act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input(label), value);
    input(label).dispatchEvent(new Event("input", { bubbles: true }));
  });
  const click = async (el: Element | undefined) => { await act(async () => { (el as HTMLElement).click(); }); };
  return { store, host, dialog, button, input, type, click, text: () => host.textContent ?? "", messages: () => store.getState().ui.notificationLog.map((n) => n.message) };
}

beforeEach(() => {
  document.body.innerHTML = "";
  for (const k of ["createChannel", "updateChannel", "testChannel", "deleteChannel", "createRule", "updateRule", "deleteRule", "ack", "resolve"] as const) api[k].length = 0;
  api.fail = null;
  api.testResult = { ok: true, detail: "delivered" };
  api.confirm = true;
  api.detail = null;
});

const onRight = (el: HTMLElement | null) => !!el && el.style.position === "fixed" && el.style.right === "0px" && el.style.top === "0px" && el.style.bottom === "0px";

// ── channel drawer ─────────────────────────────────────────────────────────────────────────────────────────────────────────
describe("channel drawer", () => {
  it("is a slide-over on the right, not a centred dialog", () => {
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose() {} }));
    expect(t.dialog()?.getAttribute("aria-label")).toBe("Add alert channel");
    expect(onRight(t.dialog())).toBe(true);
    expect(t.dialog()!.style.left).toBe("");                               // nothing pins it to the middle
  });

  it("switching between Slack and Teams changes the hint and the example URL", () => {
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose() {} }));
    expect(t.input("Webhook URL").placeholder).toContain("hooks.slack.com");
    expect(t.text()).toContain("hooks.slack.com");
    const teams = [...t.host.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")].find((b) => b.textContent === "Microsoft Teams")!;
    act(() => teams.click());
    expect(teams.getAttribute("aria-pressed")).toBe("true");
    expect(t.input("Webhook URL").placeholder).toContain("webhook.office.com");
  });

  it("cannot be added until it has a name and a webhook URL", () => {
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose() {} }));
    expect(t.button("Add channel")!.disabled).toBe(true);
    t.type("Name", "#oncall");
    expect(t.button("Add channel")!.disabled).toBe(true);
    t.type("Webhook URL", "https://hooks.slack.com/services/T/B/X");
    expect(t.button("Add channel")!.disabled).toBe(false);
  });

  it("adds the channel with exactly what was typed, tells you, and closes", async () => {
    const onClose = vi.fn();
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose }));
    t.type("Name", "  #oncall ");
    t.type("Webhook URL", " https://hooks.slack.com/services/T/B/X ");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Severity']")!, "High only — just the ones worth waking someone for");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='For job']")!, "orders-stream");
    await t.click(t.button("Add channel"));
    expect(api.createChannel).toEqual([{ name: "#oncall", kind: "slack", webhook_url: "https://hooks.slack.com/services/T/B/X", min_severity: "high", enabled: true,
      scope_job_key: "f1", scope_job_name: "orders-stream" }]);
    expect(t.messages()).toContain('Channel "#oncall" added');
    expect(onClose).toHaveBeenCalled();
  });

  it("puts a refused URL's message under the webhook field and stays open", async () => {
    api.fail = () => ({ status: 400, data: { detail: { code: "VALIDATION_FAILED", message: "A Slack webhook must be on hooks.slack.com.", details: { field: "webhook_url" } } } });
    const onClose = vi.fn();
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose }));
    t.type("Name", "x");
    t.type("Webhook URL", "https://evil.example.com/x");
    await t.click(t.button("Add channel"));
    expect(t.host.querySelector("[role=alert]")?.textContent).toBe("A Slack webhook must be on hooks.slack.com.");
    expect(t.input("Webhook URL").parentElement!.textContent).toContain("must be on hooks.slack.com");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("puts a duplicate-name message under the name", async () => {
    api.fail = () => ({ status: 409, data: { detail: { code: "ALERT_CHANNEL_NAME_TAKEN", message: "A channel named 'x' already exists.", details: { field: "name" } } } });
    const t = mount(createElement(ChannelDrawer, { channel: null, jobs: JOBS, onClose() {} }));
    t.type("Name", "x");
    t.type("Webhook URL", "https://hooks.slack.com/services/T/B/X");
    await t.click(t.button("Add channel"));
    expect(t.input("Name").parentElement!.textContent).toContain("already exists");
  });

  it("editing shows only the hint, keeps the stored URL when left blank, and sends no URL then", async () => {
    const onClose = vi.fn();
    const t = mount(createElement(ChannelDrawer, { channel: channel(), jobs: JOBS, onClose }));
    expect(t.dialog()?.getAttribute("aria-label")).toBe("Edit #oncall");
    expect(t.text()).toContain("hooks.slack.com/…/x7Q2");
    expect(t.text()).toContain("leave blank to keep the current one");
    expect(t.input("Webhook URL").value).toBe("");
    expect(t.button("Save changes")!.disabled).toBe(false);
    await t.click(t.button("Save changes"));
    expect(api.updateChannel).toHaveLength(1);
    expect((api.updateChannel[0] as { body: Record<string, unknown> }).body).not.toHaveProperty("webhook_url");
    expect(onClose).toHaveBeenCalled();
  });

  it("editing with a new URL sends it, and changing the type demands one", async () => {
    const t = mount(createElement(ChannelDrawer, { channel: channel(), jobs: JOBS, onClose() {} }));
    act(() => [...t.host.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")].find((b) => b.textContent === "Microsoft Teams")!.click());
    expect(t.button("Save changes")!.disabled).toBe(true);                   // the old URL belongs to Slack
    t.type("Webhook URL", "https://x.webhook.office.com/webhookb2/a");
    expect(t.button("Save changes")!.disabled).toBe(false);
    await t.click(t.button("Save changes"));
    expect((api.updateChannel[0] as { body: Record<string, unknown> }).body).toMatchObject({ kind: "teams", webhook_url: "https://x.webhook.office.com/webhookb2/a" });
  });

  it("clearing the job scope on an edit sends an empty key so the backend clears it", async () => {
    const t = mount(createElement(ChannelDrawer, { channel: channel({ scope_job_key: "j1", scope_job_name: "customers" }), jobs: JOBS, onClose() {} }));
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='For job']")!, "All jobs");
    await t.click(t.button("Save changes"));
    expect((api.updateChannel[0] as { body: Record<string, unknown> }).body).toMatchObject({ scope_job_key: "", scope_job_name: null });
  });
});

// ── rule drawer ────────────────────────────────────────────────────────────────────────────────────────────────────────────
describe("rule drawer", () => {
  it("is a slide-over on the right", () => {
    const t = mount(createElement(RuleDrawer, { rule: null, kinds: KINDS, jobs: JOBS, onClose() {} }));
    expect(t.dialog()?.getAttribute("aria-label")).toBe("New alert rule");
    expect(onRight(t.dialog())).toBe(true);
  });

  it("shows only the numbers the chosen kind uses", async () => {
    const t = mount(createElement(RuleDrawer, { rule: null, kinds: KINDS, jobs: JOBS, onClose() {} }));
    expect(t.text()).toContain("Window");
    expect(t.text()).not.toContain("Minimum runs");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Kind']")!, "Failure rate");
    expect(t.text()).toContain("Minimum runs");
    expect(t.text()).toContain("Percent of runs failed");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Kind']")!, "Flink job failed");
    expect(t.text()).not.toContain("Window");
    expect(t.input("Threshold").disabled).toBe(true);
  });

  it("will not save a threshold outside what the kind allows", () => {
    const t = mount(createElement(RuleDrawer, { rule: null, kinds: KINDS, jobs: JOBS, onClose() {} }));
    t.type("Rule name", "r");
    expect(t.button("Create rule")!.disabled).toBe(false);
    t.type("Threshold", "0");
    expect(t.button("Create rule")!.disabled).toBe(true);
    expect(t.host.querySelector("[role=alert]")?.textContent).toContain("1 to 1000");
    t.type("Threshold", "abc");
    expect(t.button("Create rule")!.disabled).toBe(true);
    t.type("Threshold", "5");
    expect(t.button("Create rule")!.disabled).toBe(false);
  });

  it("creates a rule with what was chosen", async () => {
    const onClose = vi.fn();
    const t = mount(createElement(RuleDrawer, { rule: null, kinds: KINDS, jobs: JOBS, onClose }));
    t.type("Rule name", " customers-tight ");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Kind']")!, "Failure rate");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Severity']")!, "high");
    t.type("Threshold", "40");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='Window']")!, "6 hours");
    t.type("Minimum runs", "8");
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='For job']")!, "customers");
    await t.click(t.button("Create rule"));
    expect(api.createRule).toEqual([{ name: "customers-tight", kind: "failure_rate", severity: "high", threshold: 40, window_seconds: 21600, min_runs: 8,
      scope_job_key: "j1", scope_job_name: "customers", enabled: true }]);
    expect(t.messages()).toContain('Rule "customers-tight" created');
    expect(onClose).toHaveBeenCalled();
  });

  it("choosing All jobs on a scoped rule sends an empty key so the backend clears the scope", async () => {
    const t = mount(createElement(RuleDrawer, { rule: rule({ scope_job_key: "j1", scope_job_name: "customers" }), kinds: KINDS, jobs: JOBS, onClose() {} }));
    await pickSearchSelect(t.host.querySelector<HTMLElement>("[aria-label='For job']")!, "All jobs");
    await t.click(t.button("Save changes"));
    expect((api.updateRule[0] as { body: Record<string, unknown> }).body).toMatchObject({ scope_job_key: "", scope_job_name: null });
  });

  it("editing starts from the rule and sends the change; a duplicate name is shown under the name", async () => {
    const t = mount(createElement(RuleDrawer, { rule: rule({ scope_job_key: "j1", scope_job_name: "customers" }), kinds: KINDS, jobs: JOBS, onClose() {} }));
    expect(t.input("Rule name").value).toBe("job-run-failed");
    expect(t.input("Threshold").value).toBe("1");
    api.fail = () => ({ status: 409, data: { detail: { code: "ALERT_RULE_NAME_TAKEN", message: "A rule named 'x' already exists.", details: { field: "name" } } } });
    t.type("Rule name", "x");
    await t.click(t.button("Save changes"));
    expect(api.updateRule).toHaveLength(1);
    expect(t.input("Rule name").parentElement!.textContent).toContain("already exists");
  });
});

// ── alert drawer ───────────────────────────────────────────────────────────────────────────────────────────────────────────
describe("alert drawer", () => {
  it("is a slide-over on the right with the summary and the history", () => {
    const t = mount(createElement(AlertDetailDrawer, { alert: alert(), canAct: true, onClose() {} }));
    expect(onRight(t.dialog())).toBe(true);
    const text = t.text();
    for (const s of ["job-run-failed", "customers", "Compilation Error", "raised", "rule sweep", "sent to #oncall", "system"]) expect(text).toContain(s);
  });

  it("an open alert can be acknowledged (with a note), which leaves it open to resolve", async () => {
    const t = mount(createElement(AlertDetailDrawer, { alert: alert(), canAct: true, onClose() {} }));
    t.type("Note", "looking");
    await t.click(t.button("Acknowledge"));
    expect(api.ack).toEqual([{ id: "a1", note: "looking" }]);
    expect(t.messages()).toContain("Alert acknowledged");
    expect(t.input("Note").value).toBe("");
  });

  it("resolving closes the drawer", async () => {
    const onClose = vi.fn();
    const t = mount(createElement(AlertDetailDrawer, { alert: alert(), canAct: true, onClose }));
    await t.click(t.button("Resolve"));
    expect(api.resolve).toEqual([{ id: "a1", note: "" }]);
    expect(onClose).toHaveBeenCalled();
  });

  it("an acknowledged alert can only be resolved; a resolved one has no actions but Open job", () => {
    api.detail = { ...alert({ state: "acknowledged" }), events: [] };
    let t = mount(createElement(AlertDetailDrawer, { alert: alert({ state: "acknowledged" }), canAct: true, onClose() {} }));
    expect(t.button("Acknowledge")).toBeUndefined();
    expect(t.button("Resolve")).toBeTruthy();
    document.body.innerHTML = "";
    api.detail = { ...alert({ state: "resolved" }), events: [] };
    t = mount(createElement(AlertDetailDrawer, { alert: alert({ state: "resolved" }), canAct: true, onClose() {} }));
    expect(t.button("Acknowledge")).toBeUndefined();
    expect(t.button("Resolve")).toBeUndefined();
    expect(t.button("Open job")).toBeTruthy();
  });

  it("someone who may not act sees no Acknowledge / Resolve", () => {
    const t = mount(createElement(AlertDetailDrawer, { alert: alert(), canAct: false, onClose() {} }));
    expect(t.button("Acknowledge")).toBeUndefined();
    expect(t.button("Resolve")).toBeUndefined();
    expect(t.host.querySelector("input[aria-label='Note']")).toBeNull();
  });

  it("says when no channel has received an open alert", () => {
    api.detail = { ...alert({ delivery: "no_channel" }), events: [] };
    const t = mount(createElement(AlertDetailDrawer, { alert: alert({ delivery: "no_channel" }), canAct: true, onClose() {} }));
    expect(t.text()).toContain("No channel has received this yet: none matches it.");
  });

  it("Open job goes to the page of the job's own kind, on that job", async () => {
    for (const [kind, section] of [["dbt", "dbt"], ["sql", "sqljobs"], ["python", "pythonjobs"], ["flink", "flink"]] as const) {
      document.body.innerHTML = "";
      const onClose = vi.fn();
      const a = alert({ job_kind: kind, job_key: `key-${kind}` });
      api.detail = { ...a, events: [] };
      const t = mount(createElement(AlertDetailDrawer, { alert: a, canAct: true, onClose }));
      await t.click(t.button("Open job"));
      expect(t.store.getState().ui.navSection).toBe(section);
      expect(t.store.getState().ui.openJob).toEqual({ section, id: `key-${kind}` });
      expect(onClose).toHaveBeenCalled();
    }
  });
});

// ── channels tab ───────────────────────────────────────────────────────────────────────────────────────────────────────────
describe("channels tab", () => {
  const tab = () => mount(createElement(ChannelsTab, { channels: [channel(), channel({ id: "c2", name: "off-one", enabled: false })], loading: false, canManage: true, onEdit() {} }));

  it("Send test uses the platform's test icon (the plug), not a generic one", () => {
    const t = tab();
    const send = [...t.host.querySelectorAll("button")].find((b) => b.textContent === "Send test")!;
    expect(send.querySelector("svg path")?.getAttribute("d")).toContain("M7 12l5 5l-1.5 1.5");      // Glyph "pi-test" (the other apps' i-test)
    expect(send.querySelector("i.ti")).toBeNull();
  });

  it("Send test reports the result", async () => {
    const t = tab();
    await t.click([...t.host.querySelectorAll("button")].find((b) => b.textContent === "Send test"));
    expect(api.testChannel).toEqual(["c1"]);
    expect(t.messages()).toContain("Test message sent to #oncall");
    api.testResult = { ok: false, detail: "404 no_service" };
    await t.click([...t.host.querySelectorAll("button")].find((b) => b.textContent === "Send test"));
    expect(t.messages()).toContain("Test to #oncall failed: 404 no_service");
  });

  it("removing a channel asks first, and nothing happens if you say no", async () => {
    const t = tab();
    api.confirm = false;
    await t.click(t.host.querySelector("button[aria-label='Remove #oncall']")!);
    expect(api.deleteChannel).toEqual([]);
    api.confirm = true;
    await t.click(t.host.querySelector("button[aria-label='Remove #oncall']")!);
    expect(api.deleteChannel).toEqual(["c1"]);
    expect(t.messages()).toContain('Removed channel "#oncall"');
  });

  it("a channel that is off says so", () => {
    expect(tab().text()).toContain("off");
  });
});

// ── rules tab ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
describe("rules tab", () => {
  const tab = () => mount(createElement(RulesTab, { rules: [rule(), rule({ id: "r2", name: "off-rule", enabled: false })], loading: false, kinds: KINDS, canManage: true, onEdit() {} }));

  it("the switch turns a rule on or off", async () => {
    const t = tab();
    const sw = t.host.querySelectorAll<HTMLButtonElement>("button[role=switch]");
    expect(sw[0].getAttribute("aria-checked")).toBe("true");
    expect(sw[1].getAttribute("aria-checked")).toBe("false");
    await t.click(sw[0]);
    expect(api.updateRule).toEqual([{ id: "r1", body: { enabled: false } }]);
    await t.click(sw[1]);
    expect(api.updateRule.at(-1)).toEqual({ id: "r2", body: { enabled: true } });
  });

  it("deleting a rule asks first", async () => {
    const t = tab();
    api.confirm = false;
    await t.click(t.host.querySelector("button[aria-label='Delete job-run-failed']")!);
    expect(api.deleteRule).toEqual([]);
    api.confirm = true;
    await t.click(t.host.querySelector("button[aria-label='Delete job-run-failed']")!);
    expect(api.deleteRule).toEqual(["r1"]);
  });
});
