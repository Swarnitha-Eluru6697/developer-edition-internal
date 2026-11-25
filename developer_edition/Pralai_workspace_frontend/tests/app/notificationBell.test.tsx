/** Header bell: five notifications per page (newest first), Prev / Next / Delete all at the bottom, a delete button per row on hover. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it } from "vitest";
import uiReducer, { notify } from "../../src/store/uiSlice";
import { NotificationBell } from "../../src/app/NotificationBell";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount(count: number) {
  const store = configureStore({ reducer: { ui: uiReducer } });
  for (let i = 1; i <= count; i++) store.dispatch(notify({ kind: "success", message: `note ${i}` }));   // note `count` is the newest
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(NotificationBell, { buttonStyle: {} }) })));
  const bell = () => host.querySelector<HTMLButtonElement>("button[title='Notifications']")!;
  const byLabel = (label: string) => host.querySelector<HTMLButtonElement>(`button[aria-label='${label}']`);
  const rows = () => [...host.querySelectorAll<HTMLElement>("div[style*='align-items: flex-start']")];
  return {
    store, host, bell, byLabel, rows,
    open: () => act(() => bell().click()),
    messages: () => rows().map((r) => r.textContent?.replace(/just now|\d+m ago/, "")),
    range: () => host.textContent?.match(/\d+–\d+ of \d+/)?.[0],
    click: (label: string) => act(() => byLabel(label)!.click()),
    hover: (row: HTMLElement) => act(() => { row.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); }),
    unhover: (row: HTMLElement) => act(() => { row.dispatchEvent(new MouseEvent("mouseout", { bubbles: true })); }),
  };
}

beforeEach(() => { document.body.innerHTML = ""; });

describe("notification bell", () => {
  it("shows the five newest first, with no delete button in the header", () => {
    const t = mount(14);
    t.open();
    expect(t.messages()).toEqual(["note 14", "note 13", "note 12", "note 11", "note 10"]);
    expect(t.range()).toBe("1–5 of 14");
    const header = t.host.querySelector("span")!.parentElement!;
    expect(header.textContent).toBe("Notifications");
    expect(header.querySelector("button")).toBeNull();
  });

  it("Prev / Next page through them and are disabled at the ends", () => {
    const t = mount(14);
    t.open();
    expect(t.byLabel("Previous notifications")!.disabled).toBe(true);
    t.click("Next notifications");
    expect(t.messages()).toEqual(["note 9", "note 8", "note 7", "note 6", "note 5"]);
    expect(t.range()).toBe("6–10 of 14");
    t.click("Next notifications");
    expect(t.messages()).toEqual(["note 4", "note 3", "note 2", "note 1"]);
    expect(t.range()).toBe("11–14 of 14");
    expect(t.byLabel("Next notifications")!.disabled).toBe(true);
    t.click("Previous notifications");
    expect(t.range()).toBe("6–10 of 14");
  });

  it("a row's delete button appears only on hover, and deletes just that notification", () => {
    const t = mount(7);
    t.open();
    const row = t.rows()[1];                                                     // "note 6"
    const del = () => row.querySelector<HTMLButtonElement>("button[aria-label='Delete this notification']")!;
    expect(del().style.visibility).toBe("hidden");
    t.hover(row);
    expect(del().style.visibility).toBe("visible");
    t.unhover(row);
    expect(del().style.visibility).toBe("hidden");
    t.hover(row);
    act(() => del().click());
    expect(t.store.getState().ui.notificationLog.map((n) => n.message)).toEqual(["note 7", "note 5", "note 4", "note 3", "note 2", "note 1"]);
    expect(t.messages()).toEqual(["note 7", "note 5", "note 4", "note 3", "note 2"]);        // the next one moves up into the page
  });

  it("deleting the only row of the last page goes back a page instead of showing an empty one", () => {
    const t = mount(6);
    t.open();
    t.click("Next notifications");
    expect(t.messages()).toEqual(["note 1"]);
    const row = t.rows()[0];
    t.hover(row);
    act(() => row.querySelector<HTMLButtonElement>("button[aria-label='Delete this notification']")!.click());
    expect(t.messages()).toEqual(["note 6", "note 5", "note 4", "note 3", "note 2"]);
    expect(t.range()).toBe("1–5 of 5");
  });

  it("Delete all empties the list, shows the empty message and drops the footer", () => {
    const t = mount(9);
    t.open();
    t.click("Delete all notifications");
    expect(t.store.getState().ui.notificationLog).toEqual([]);
    expect(t.host.textContent).toContain("No notifications yet");
    expect(t.byLabel("Next notifications")).toBeNull();
    expect(t.byLabel("Delete all notifications")).toBeNull();
  });

  it("no footer when there is nothing, and opening again starts on the first page", () => {
    const empty = mount(0);
    empty.open();
    expect(empty.byLabel("Delete all notifications")).toBeNull();
    document.body.innerHTML = "";
    const t = mount(12);
    t.open();
    t.click("Next notifications");
    t.open();                                                                    // close
    t.open();                                                                    // open again
    expect(t.range()).toBe("1–5 of 12");
  });

  it("a short list is one page: Prev and Next are both disabled", () => {
    const t = mount(3);
    t.open();
    expect(t.range()).toBe("1–3 of 3");
    expect(t.byLabel("Previous notifications")!.disabled).toBe(true);
    expect(t.byLabel("Next notifications")!.disabled).toBe(true);
  });
});
