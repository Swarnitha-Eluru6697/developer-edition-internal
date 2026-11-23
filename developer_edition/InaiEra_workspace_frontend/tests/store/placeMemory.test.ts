/**
 * A refresh keeps your place inside a page; going to a page from the sidebar opens it on its list, as it always did.
 * (Without this the SQL page came back inside the job last opened, while dbt and Python, with none open, showed lists.)
 */
import { configureStore } from "@reduxjs/toolkit";
import { afterEach, describe, expect, it, vi } from "vitest";
import uiReducer, { setNavSection, type NavSection } from "../../src/store/uiSlice";
import { forgetPlaceOnNavigate, PLACE_PREFIX } from "../../src/store/placeMemory";
import { forgetPersisted, writePersisted } from "../../src/shared/usePersistedState";
import { store as appStore } from "../../src/store";

const key = (k: string) => `inaiera.ui.${k}`;
const remember = () => {
  writePersisted("jobs.dbt.project", "p1", null);
  writePersisted("jobs.dbt.tab", "dependents", "runs");
  writePersisted("jobs.sql.project", "p2", null);
  writePersisted("jobs.sql.tab", "schedule", "runs");
  writePersisted("jobs.python.project", "p3", null);
  writePersisted("flink.job", "j1", null);
  writePersisted("dq.tab", "render", "bindings");
  writePersisted("dq.binding", "b1", null);
  writePersisted("alerts.tab", "channels", "open");
  writePersisted("ws.filePanelTab", "changes", "files");
  writePersisted("ws.editorTabs", { workspaceId: "w", openTabs: ["f"], activeTab: "f" }, null);
};
const left = () => Object.keys(window.sessionStorage).map((k) => k.replace("inaiera.ui.", "")).sort();
const makeStore = (section: NavSection) => {
  const store = configureStore({
    reducer: { ui: uiReducer },
    preloadedState: { ui: { ...uiReducer(undefined, { type: "@@INIT" }), navSection: section } },
    middleware: (getDefault) => getDefault().concat(forgetPlaceOnNavigate),
  });
  return store;
};

afterEach(() => { vi.restoreAllMocks(); });

describe("forgetPersisted", () => {
  it("removes only the places that start with the prefix", () => {
    remember();
    forgetPersisted("jobs.sql.");
    expect(left()).not.toContain("jobs.sql.project");
    expect(left()).not.toContain("jobs.sql.tab");
    expect(left()).toContain("jobs.dbt.project");
    expect(left()).toContain("dq.tab");
  });

  it("never throws when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "key").mockImplementation(() => { throw new Error("denied"); });
    expect(() => forgetPersisted("jobs.")).not.toThrow();
  });
});

describe("going to a page opens it on its list", () => {
  it("each page forgets ITS place and nobody else's", () => {
    for (const [section, prefix] of Object.entries(PLACE_PREFIX) as [NavSection, string][]) {
      window.sessionStorage.clear();
      remember();
      makeStore("settings").dispatch(setNavSection(section));
      expect(left().filter((k) => k.startsWith(prefix)), section).toEqual([]);
      const others = Object.values(PLACE_PREFIX).filter((p) => p !== prefix);
      for (const p of others) expect(left().some((k) => k.startsWith(p)), `${section} must keep ${p}`).toBe(true);
    }
  });

  it("covers every page that has an inner place, and none that does not", () => {
    expect(Object.keys(PLACE_PREFIX).sort()).toEqual(["alerts", "dbt", "dq", "flink", "pythonjobs", "sqljobs"]);
  });

  it("the SQL page opens on its list even though a job was open there, while a refresh would have kept it", () => {
    remember();
    expect(window.sessionStorage.getItem(key("jobs.sql.project"))).toBe('"p2"');       // a refresh dispatches nothing: still remembered
    makeStore("dbt").dispatch(setNavSection("sqljobs"));
    expect(window.sessionStorage.getItem(key("jobs.sql.project"))).toBeNull();
    expect(window.sessionStorage.getItem(key("jobs.sql.tab"))).toBeNull();
  });

  it("opening the page you are already on changes nothing", () => {
    remember();
    makeStore("sqljobs").dispatch(setNavSection("sqljobs"));
    expect(window.sessionStorage.getItem(key("jobs.sql.project"))).toBe('"p2"');
  });

  it("the Workspace and Settings have no inner place to forget: its open files stay", () => {
    remember();
    makeStore("dbt").dispatch(setNavSection("ws"));
    makeStore("dbt").dispatch(setNavSection("settings"));
    expect(left()).toEqual(expect.arrayContaining(["ws.editorTabs", "ws.filePanelTab", "jobs.sql.project", "dq.binding"]));
  });

  it("the page still changes as before", () => {
    const store = makeStore("dbt");
    store.dispatch(setNavSection("sqljobs"));
    expect(store.getState().ui.navSection).toBe("sqljobs");
  });

  it("is wired into the app's real store", () => {
    remember();
    const section = appStore.getState().ui.navSection;
    const other: NavSection = section === "pythonjobs" ? "dq" : "pythonjobs";
    appStore.dispatch(setNavSection(other));
    expect(left().some((k) => k.startsWith(PLACE_PREFIX[other]!))).toBe(false);
    appStore.dispatch(setNavSection(section));
  });
});
