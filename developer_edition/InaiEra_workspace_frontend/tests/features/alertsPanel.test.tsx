/** The Alerts page: five tabs (Open / Acknowledged / Resolved / Rules / Channels), the lists, paging, dates, and who sees which buttons. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import uiReducer from "../../src/store/uiSlice";
import { alert, channel, KINDS, rule } from "../helpers/alertsMocks";

const api = vi.hoisted(() => ({
  caps: new Set<string>(["workspace:alerts:view", "workspace:alerts:edit", "workspace:alerts:manage"]),
  listCalls: [] as Record<string, unknown>[],
  pages: {} as Record<string, { items: unknown[]; total: number; undelivered: number }>,
  counts: { open: 2, acknowledged: 1, resolved: 3, undelivered: 0 },
  rules: [] as unknown[], channels: [] as unknown[],
  run: vi.fn(),
  runResult: { ran: true, detail: "ok", raised: 1, cleared: 0, delivered: 1, sources: [] } as Record<string, unknown>,
}));
vi.mock("../../src/rbac/hooks", () => ({ useHasPermission: (c: string) => api.caps.has(c), usePermissions: () => [...api.caps] }));
vi.mock("../../src/features/alerts/useJobChoices", () => ({ useJobChoices: () => [{ key: "j1", name: "customers", kind: "dbt" }, { key: "f1", name: "orders-stream", kind: "flink" }] }));
vi.mock("../../src/features/alerts/api", () => {
  const noop = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListAlertsQuery: (args: Record<string, unknown>) => { api.listCalls.push(args); return { data: api.pages[String(args.state)] ?? { items: [], total: 0, undelivered: 0 }, isLoading: false }; },
    useGetAlertCountsQuery: () => ({ data: api.counts }),
    useGetAlertQuery: (id: string) => ({ data: { ...alert({ id }), events: [{ action: "raised", actor: "rule sweep", note: null, at: "2026-10-29T09:42:00Z" }] } }),
    useListAlertRulesQuery: () => ({ data: api.rules, isLoading: false }),
    useListAlertRuleKindsQuery: () => ({ data: KINDS }),
    useListAlertChannelsQuery: () => ({ data: api.channels, isLoading: false }),
    useRunAlertRulesMutation: () => [() => ({ unwrap: async () => { api.run(); return api.runResult; } }), { isLoading: false }],
    useAcknowledgeAlertMutation: noop, useResolveAlertMutation: noop, useCreateAlertRuleMutation: noop, useUpdateAlertRuleMutation: noop,
    useDeleteAlertRuleMutation: noop, useCreateAlertChannelMutation: noop, useUpdateAlertChannelMutation: noop, useDeleteAlertChannelMutation: noop,
    useTestAlertChannelMutation: noop,
  };
});

import { AlertsPanel } from "../../src/features/alerts/AlertsPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount() {
  const store = configureStore({ reducer: { ui: uiReducer } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(AlertsPanel) })));
  const tab = (name: string) => [...host.querySelectorAll<HTMLElement>("[role=tab]")].find((t) => t.textContent?.trim().toLowerCase().startsWith(name))!;
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === text);
  const click = (el: Element | undefined) => act(() => { (el as HTMLElement).click(); });
  const setDate = (label: string, value: string) => act(() => {
    const input = host.querySelector<HTMLInputElement>(`input[aria-label='${label}']`)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return { store, host, tab, button, click, setDate, text: () => host.textContent ?? "", cards: () => host.querySelectorAll("button[aria-label^='Open alert']") };
}

beforeEach(() => {
  document.body.innerHTML = "";
  sessionStorage.clear();
  api.caps = new Set(["workspace:alerts:view", "workspace:alerts:edit", "workspace:alerts:manage"]);
  api.listCalls = [];
  api.pages = {};
  api.counts = { open: 2, acknowledged: 1, resolved: 3, undelivered: 0 };
  api.rules = []; api.channels = [];
  api.run.mockClear();
});

describe("alerts page: tabs", () => {
  it("shows the five tabs, in order, with the number of alerts on the three alert tabs", () => {
    const t = mount();
    const tabs = [...t.host.querySelectorAll("[role=tab]")].map((x) => x.textContent?.trim().toLowerCase());
    expect(tabs).toEqual(["open2", "acknowledged1", "resolved3", "rules", "channels"]);
    expect(t.host.querySelector("h1")?.textContent).toBe("Alerts");
    expect(t.text()).toContain("Operate");
  });

  it("starts on Open and asks the API for open alerts, ten at a time", () => {
    mount();
    expect(api.listCalls.at(-1)).toMatchObject({ state: "open", limit: 10, offset: 0 });
  });

  it("remembers the tab across a refresh", () => {
    const first = mount();
    first.click(first.tab("channels"));
    expect(sessionStorage.getItem("inaiera.ui.alerts.tab")).toBe('"channels"');
    document.body.innerHTML = "";
    const again = mount();
    expect(again.tab("channels").getAttribute("aria-selected")).toBe("true");
  });
});

describe("alerts page: the lists", () => {
  it("lists an alert as a card: severity, rule, job, time, what happened and the last thing done", () => {
    api.pages.open = { items: [alert()], total: 1, undelivered: 0 };
    const t = mount();
    expect(t.cards()).toHaveLength(1);
    const card = t.cards()[0].textContent!;
    for (const s of ["high", "job-run-failed", "customers", "Compilation Error", "sent to #oncall", "system"]) expect(card).toContain(s);
    expect(t.text()).toContain("Showing 1–1 of 1 alert");
  });

  it("marks an acknowledged alert on the Open-style card and lists it under Acknowledged", () => {
    api.pages.acknowledged = { items: [alert({ state: "acknowledged" })], total: 1, undelivered: 0 };
    const t = mount();
    t.click(t.tab("acknowledged"));
    expect(api.listCalls.at(-1)).toMatchObject({ state: "acknowledged", offset: 0 });
    expect(t.cards()[0].textContent).toContain("acknowledged");
  });

  it("says nothing is here, per tab, when it is empty", () => {
    const t = mount();
    expect(t.text()).toContain("No open alerts");
    t.click(t.tab("resolved"));
    expect(t.text()).toContain("Nothing has been resolved yet");
  });

  it("tells how many open alerts no channel has received, and sends you to Channels", () => {
    api.pages.open = { items: [], total: 0, undelivered: 3 };
    const t = mount();
    expect(t.text()).toContain("3 alerts reached no channel yet");
    t.click(t.button("Channels"));
    expect(t.tab("channels").getAttribute("aria-selected")).toBe("true");
  });

  it("is silent about undelivered alerts when there are none, and the note is only on Open", () => {
    const t = mount();
    expect(t.text()).not.toContain("reached no channel");
    api.pages.acknowledged = { items: [], total: 0, undelivered: 4 };
    t.click(t.tab("acknowledged"));
    expect(t.text()).not.toContain("reached no channel");
  });

  it("opens the alert's drawer from its card", () => {
    api.pages.open = { items: [alert()], total: 1, undelivered: 0 };
    const t = mount();
    t.click(t.cards()[0]);
    expect(t.host.querySelector("[role=dialog]")?.getAttribute("aria-label")).toBe("Alert");
    expect(t.host.querySelector("[role=dialog]")?.textContent).toContain("job-run-failed");
  });
});

describe("alerts page: paging and dates", () => {
  it("pages with Previous / Next, ten per page, and the buttons stop at the ends", () => {
    api.pages.open = { items: Array.from({ length: 10 }, (_, i) => alert({ id: `a${i}` })), total: 23, undelivered: 0 };
    const t = mount();
    expect(t.text()).toContain("Showing 1–10 of 23 alerts");
    expect(t.button("Previous")!.disabled).toBe(true);
    t.click(t.button("Next"));
    expect(api.listCalls.at(-1)).toMatchObject({ offset: 10 });
    t.click(t.button("Next"));
    expect(api.listCalls.at(-1)).toMatchObject({ offset: 20 });
    expect(t.button("Next")!.disabled).toBe(true);
    t.click(t.button("Previous"));
    expect(api.listCalls.at(-1)).toMatchObject({ offset: 10 });
  });

  it("filters by date, goes back to the first page when the dates change, and Clear dates removes them", () => {
    api.pages.open = { items: [alert()], total: 25, undelivered: 0 };
    const t = mount();
    t.click(t.button("Next"));
    t.setDate("From date", "2026-10-01");
    expect(api.listCalls.at(-1)).toMatchObject({ from: "2026-10-01", offset: 0 });
    t.setDate("To date", "2026-10-29");
    expect(api.listCalls.at(-1)).toMatchObject({ from: "2026-10-01", to: "2026-10-29" });
    expect(t.text()).toContain("Clear dates");
    t.click(t.button("Clear dates"));
    expect(api.listCalls.at(-1)?.from).toBeUndefined();
    expect(t.text()).not.toContain("Clear dates");
  });

  it("changing tab starts again from the first page", () => {
    api.pages.open = { items: [alert()], total: 25, undelivered: 0 };
    api.pages.resolved = { items: [alert({ state: "resolved" })], total: 25, undelivered: 0 };
    const t = mount();
    t.click(t.button("Next"));
    t.click(t.tab("resolved"));
    expect(api.listCalls.at(-1)).toMatchObject({ state: "resolved", offset: 0 });
  });
});

describe("alerts page: rules and channels tabs", () => {
  it("lists the rules with their sentence, and nothing about what is breaching (the Open tab already shows it)", () => {
    api.rules = [rule(), rule({ id: "r2", name: "tight", kind: "failure_rate", threshold: 50, min_runs: 5, window_seconds: 0, severity: "medium", scope_job_name: "customers" })];
    const t = mount();
    t.click(t.tab("rules"));
    const text = t.text();
    expect(text).toContain("≥ 1 failed run · 1 hour");
    expect(text).toContain("≥ 50% failed · min 5 runs · 24 hours");
    expect(text).toContain("Showing 1–2 of 2 rules");
    expect(text).not.toContain("Currently breaching");
    expect(text).not.toContain("already open");
    expect(text).not.toContain("would raise");
  });

  it("lists the channels and shows how the last send went", () => {
    api.channels = [channel(), channel({ id: "c2", name: "Agents Teams", kind: "teams", last_ok: false, last_detail: "404 no_service", min_severity: "high", scope_job_name: "customers" })];
    const t = mount();
    t.click(t.tab("channels"));
    const text = t.text();
    expect(text).toContain("#oncall");
    expect(text).toContain("last send ok");
    expect(text).toContain("last send failed");
    expect(text).toContain("404 no_service");
    expect(text).toContain("Receives high only from customers");
    expect(text).toContain("hooks.slack.com/…/x7Q2");
    expect(text).toContain("Showing 1–2 of 2 channels");
  });

  it("says there are no channels, and why that matters", () => {
    const t = mount();
    t.click(t.tab("channels"));
    expect(t.text()).toContain("No channels yet");
  });
});

describe("alerts page: who sees which buttons", () => {
  it("a manager gets Run rules now and New rule on Rules, and Add channel on Channels", () => {
    const t = mount();
    t.click(t.tab("rules"));
    expect(t.button("Run rules now")).toBeTruthy();
    expect(t.button("New rule")).toBeTruthy();
    t.click(t.tab("channels"));
    expect(t.button("Add channel")).toBeTruthy();
    expect(t.button("New rule")).toBeUndefined();
  });

  it("someone who may only look sees no action buttons, no edit / delete, and the switches are disabled", () => {
    api.caps = new Set(["workspace:alerts:view"]);
    api.rules = [rule()];
    api.channels = [channel()];
    const t = mount();
    t.click(t.tab("rules"));
    expect(t.button("Run rules now")).toBeUndefined();
    expect(t.button("New rule")).toBeUndefined();
    expect(t.host.querySelector("button[aria-label='Edit job-run-failed']")).toBeNull();
    expect(t.host.querySelector<HTMLButtonElement>("button[role=switch]")!.disabled).toBe(true);
    t.click(t.tab("channels"));
    expect(t.button("Add channel")).toBeUndefined();
    expect(t.host.querySelector("button[aria-label='Edit #oncall']")).toBeNull();
    expect(t.text()).not.toContain("Send test");
  });

  it("Run rules now runs one sweep and says what it did", async () => {
    const t = mount();
    t.click(t.tab("rules"));
    await act(async () => { t.button("Run rules now")!.click(); });
    expect(api.run).toHaveBeenCalledTimes(1);
    expect(t.store.getState().ui.notificationLog[0].message).toBe("Rules checked: 1 raised, 0 cleared, 1 sent");
    api.runResult = { ran: false, detail: "A sweep is already running.", raised: 0, cleared: 0, delivered: 0, sources: [] };
    await act(async () => { t.button("Run rules now")!.click(); });
    expect(t.store.getState().ui.notificationLog[0].message).toBe("A sweep is already running.");
    api.runResult = { ran: true, detail: "the dbt Runner could not be reached (ConnectError)", raised: 0, cleared: 0, delivered: 0, sources: [] };
    await act(async () => { t.button("Run rules now")!.click(); });
    expect(t.store.getState().ui.notificationLog[0].message).toContain("(the dbt Runner could not be reached (ConnectError))");
  });
});
