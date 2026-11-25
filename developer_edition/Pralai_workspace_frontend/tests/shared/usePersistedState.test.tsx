/**
 * A screen remembers its place across a refresh: `useState` that also keeps its value in sessionStorage (per browser tab). What comes back
 * is validated, the default is stored as nothing, and unavailable storage never breaks the screen.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isStringOrNull, oneOf, readPersisted, usePersistedState, writePersisted } from "../../src/shared/usePersistedState";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const stored = (key: string) => window.sessionStorage.getItem(`inaiera.ui.${key}`);
const TABS = oneOf("runs", "schedule");

afterEach(() => { vi.restoreAllMocks(); });

describe("readPersisted / writePersisted", () => {
  it("round-trips a value and stores the default as nothing", () => {
    writePersisted("k", "schedule", "runs");
    expect(stored("k")).toBe('"schedule"');
    expect(readPersisted("k", "runs", TABS)).toBe("schedule");
    writePersisted("k", "runs", "runs");
    expect(stored("k")).toBeNull();
    expect(readPersisted("k", "runs", TABS)).toBe("runs");
  });

  it("ignores a stored value that is not valid, whatever is wrong with it", () => {
    for (const bad of ['"nonsense"', "5", "null", "{", "[]"]) {
      window.sessionStorage.setItem("inaiera.ui.k", bad);
      expect(readPersisted("k", "runs", TABS)).toBe("runs");
    }
  });

  it("accepts null where null is valid", () => {
    window.sessionStorage.setItem("inaiera.ui.k", "null");
    expect(readPersisted<string | null>("k", "x", isStringOrNull)).toBeNull();
    window.sessionStorage.setItem("inaiera.ui.k", "7");
    expect(readPersisted<string | null>("k", "x", isStringOrNull)).toBe("x");
  });

  it("works when storage is unavailable: nothing throws, the default is used", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    expect(readPersisted("k", "runs", TABS)).toBe("runs");
    expect(() => writePersisted("k", "schedule", "runs")).not.toThrow();
  });

  it("oneOf accepts only the listed strings", () => {
    expect([TABS("runs"), TABS("schedule"), TABS("other"), TABS(1), TABS(null)]).toEqual([true, true, false, false, false]);
  });
});

function Probe({ id }: { id: string }) {
  const [value, setValue] = usePersistedState<string>(`probe.${id}`, "runs", TABS);
  return createElement("button", { onClick: () => setValue("schedule") }, value);
}
function mount(id = "a") {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(createElement(Probe, { id })));
  return { host, unmount: () => act(() => root.unmount()) };
}

describe("usePersistedState", () => {
  it("starts at the default, remembers a change, and a new mount (a refresh) finds it again", async () => {
    const first = mount();
    expect(first.host.textContent).toBe("runs");
    await act(async () => { first.host.querySelector("button")!.click(); });
    expect(first.host.textContent).toBe("schedule");
    first.unmount();
    expect(mount().host.textContent).toBe("schedule");
  });

  it("keeps separate places under separate keys", async () => {
    const a = mount("a");
    await act(async () => { a.host.querySelector("button")!.click(); });
    expect(mount("b").host.textContent).toBe("runs");
  });

  it("ignores a remembered value that is no longer valid", () => {
    window.sessionStorage.setItem("inaiera.ui.probe.a", '"removed-tab"');
    expect(mount("a").host.textContent).toBe("runs");
  });
});
