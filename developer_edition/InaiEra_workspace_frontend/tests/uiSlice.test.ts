import { beforeEach, describe, expect, it } from "vitest";

import reducer, {
  CHAT_WIDTH_MIN,
  clampChatWidth,
  clearActiveTab,
  clearNotifications,
  closeAllTabs,
  closeTab,
  markNotificationsRead,
  removeNotification,
  notify,
  openTab,
  readStoredNavSection,
  readStoredTreeOpen,
  saveNavSection,
  saveTreeOpen,
  setActiveTab,
  setTreeNodeOpen,
  treeNodeKey,
} from "../src/store/uiSlice";
import type { UUID } from "../src/types";

const A: UUID = "11111111-1111-1111-1111-111111111111";
const B: UUID = "22222222-2222-2222-2222-222222222222";
const C: UUID = "33333333-3333-3333-3333-333333333333";

const INIT = { type: "@@INIT" };

describe("uiSlice reducer: tab management", () => {
  it("openTab appends a new tab and activates it, without duplicating an already-open tab", () => {
    let state = reducer(undefined, INIT);
    state = reducer(state, openTab(A));
    state = reducer(state, openTab(B));
    expect(state.openTabs).toEqual([A, B]);
    expect(state.activeTab).toBe(B);

    // Re-opening an already-open tab activates it but must not duplicate it.
    state = reducer(state, openTab(A));
    expect(state.openTabs).toEqual([A, B]);
    expect(state.activeTab).toBe(A);
  });

  it("closeTab falls back to the new last tab only when the closed tab was active", () => {
    let state = reducer(undefined, INIT);
    state = reducer(state, openTab(A));
    state = reducer(state, openTab(B));
    state = reducer(state, openTab(C));
    expect(state.activeTab).toBe(C);

    state = reducer(state, closeTab(C));
    expect(state.openTabs).toEqual([A, B]);
    expect(state.activeTab).toBe(B); // was active, falls back to the new last tab

    state = reducer(state, closeTab(A));
    expect(state.openTabs).toEqual([B]);
    expect(state.activeTab).toBe(B); // A wasn't active — activeTab untouched

    state = reducer(state, closeTab(B));
    expect(state.openTabs).toEqual([]);
    expect(state.activeTab).toBeNull();
  });

  it("closeAllTabs empties the tab strip (the workspace was deleted)", () => {
    let state = reducer(undefined, INIT);
    state = reducer(state, openTab(A));
    state = reducer(state, openTab(B));
    state = reducer(state, closeAllTabs());
    expect(state.openTabs).toEqual([]);
    expect(state.activeTab).toBeNull();
  });

  it("setActiveTab / clearActiveTab set and clear the active tab directly", () => {
    let state = reducer(undefined, INIT);
    state = reducer(state, openTab(A));
    state = reducer(state, openTab(B));
    state = reducer(state, setActiveTab(A));
    expect(state.activeTab).toBe(A);

    state = reducer(state, clearActiveTab());
    expect(state.activeTab).toBeNull();
  });
});

describe("clampChatWidth", () => {
  it("clamps a value below CHAT_WIDTH_MIN up to CHAT_WIDTH_MIN", () => {
    expect(clampChatWidth(10)).toBe(CHAT_WIDTH_MIN);
  });

  it("passes a value comfortably within range through unchanged, given a wide viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 1600, configurable: true });
    try {
      expect(clampChatWidth(300)).toBe(300);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: original, configurable: true });
    }
  });

  it("never returns less than CHAT_WIDTH_MIN even on a very narrow viewport", () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 320, configurable: true });
    try {
      expect(clampChatWidth(300)).toBeGreaterThanOrEqual(CHAT_WIDTH_MIN);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: original, configurable: true });
    }
  });
});


describe("active page survives a refresh", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("defaults to Workspace, restores a saved page, and ignores junk", () => {
    expect(readStoredNavSection()).toBe("ws");
    saveNavSection("settings");
    expect(readStoredNavSection()).toBe("settings");
    saveNavSection("dbt");
    expect(readStoredNavSection()).toBe("dbt");
    window.sessionStorage.setItem("inaiera.navSection", "not-a-page");
    expect(readStoredNavSection()).toBe("ws");
  });
});


describe("file tree folder state", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("setTreeNodeOpen records each folder per workspace and path", () => {
    const WS = "ws-1";
    let state = reducer(undefined, INIT);
    state = reducer(state, setTreeNodeOpen({ key: treeNodeKey(WS, "customers"), open: false }));
    state = reducer(state, setTreeNodeOpen({ key: treeNodeKey(WS, "customers/models"), open: true }));
    state = reducer(state, setTreeNodeOpen({ key: treeNodeKey("ws-2", "customers"), open: true }));
    expect(state.treeOpen[treeNodeKey(WS, "customers")]).toBe(false);
    expect(state.treeOpen[treeNodeKey(WS, "customers/models")]).toBe(true);
    expect(state.treeOpen[treeNodeKey("ws-2", "customers")]).toBe(true);   // another workspace is independent
    expect(state.treeOpen[treeNodeKey(WS, "never-touched")]).toBeUndefined(); // falls back to the depth default
  });

  it("is restored from storage and ignores junk", () => {
    saveTreeOpen({ "ws:a": false, "ws:b": true });
    expect(readStoredTreeOpen()).toEqual({ "ws:a": false, "ws:b": true });
    window.sessionStorage.setItem("inaiera.treeOpen", JSON.stringify({ "ws:a": "yes", "ws:b": true }));
    expect(readStoredTreeOpen()).toEqual({ "ws:b": true });
    window.sessionStorage.setItem("inaiera.treeOpen", "not json");
    expect(readStoredTreeOpen()).toEqual({});
  });
});

describe("notification log (header bell)", () => {
  it("keeps every toast newest-first with an unread count, until read or cleared", () => {
    let s = reducer(undefined, { type: "@@init" });
    s = reducer(s, notify({ kind: "success", message: "Saved" }));
    s = reducer(s, notify({ kind: "error", message: "Run failed" }));
    expect(s.notification).toEqual({ kind: "error", message: "Run failed" });
    expect(s.notificationLog.map((n) => n.message)).toEqual(["Run failed", "Saved"]);
    expect(s.notificationLog[0].id).not.toBe(s.notificationLog[1].id);
    expect(s.unreadNotifications).toBe(2);
    s = reducer(s, markNotificationsRead());
    expect(s.unreadNotifications).toBe(0);
    expect(s.notificationLog).toHaveLength(2);
    s = reducer(s, clearNotifications());
    expect(s.notificationLog).toEqual([]);
  });

  it("removes one notification by id and leaves the others", () => {
    let s = reducer(undefined, { type: "@@init" });
    for (const message of ["a", "b", "c"]) s = reducer(s, notify({ kind: "info", message }));
    const middle = s.notificationLog[1].id;                                   // "b"
    s = reducer(s, removeNotification(middle));
    expect(s.notificationLog.map((n) => n.message)).toEqual(["c", "a"]);
    s = reducer(s, removeNotification(-1));                                   // an id that is not there changes nothing
    expect(s.notificationLog).toHaveLength(2);
  });
});
