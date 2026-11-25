/** Assistant → the SQL engine picker under the "Generate" target: Trino is the default and the only choice for now. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickSearchSelect, searchSelectOptions } from "../helpers/searchSelect";

const api = vi.hoisted(() => ({ submitted: [] as { body: Record<string, unknown> }[] }));
vi.mock("../../src/api/client", () => {
  const unwrap = (v: unknown) => ({ unwrap: async () => v });
  return {
    workspaceApi: { util: { invalidateTags: (t: unknown) => ({ type: "noop", payload: t }) } },
    useCreateCodegenSessionMutation: () => [() => unwrap({ id: "sess-1" }), { isLoading: false }],
    useSubmitCodegenPromptMutation: () => [(arg: { body: Record<string, unknown> }) => { api.submitted.push(arg); return unwrap({ context_included: false }); }, { isLoading: false }],
    useApplyCodegenArtifactMutation: () => [vi.fn(), { isLoading: false }],
    useListCodegenMessagesQuery: () => ({ data: undefined, error: undefined }),
  };
});

import { CodegenChatPanel, codegenReducer, setCodegenEngine, setCodegenTarget } from "../../src/features/codegen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount(language: string | null = "sql") {
  const store = configureStore({ reducer: { codegen: codegenReducer, ui: (s = { activeConnectionId: null }) => s } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(CodegenChatPanel, {
    workspaceId: "ws-1", activeFileId: null, activeFileContent: null, activeFileLanguage: language, execSessionId: "x", onArtifactApplied() {}, onInsertCode() {},
  }) })));
  const engine = () => host.querySelector<HTMLElement>("[aria-label='SQL engine']");
  const send = async (text: string) => {
    const box = host.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Send")!.click(); });
  };
  return { store, host, engine, send };
}

beforeEach(() => { api.submitted = []; localStorage.clear(); document.body.innerHTML = ""; });

describe("assistant: SQL engine picker", () => {
  it("sits under the Generate target for SQL, starts on Trino, and lists Trino then ClickHouse: no pg_lake", async () => {
    const t = mount("sql");
    expect(t.engine()).toBeTruthy();
    expect(t.engine()!.textContent).toBe("Trino");
    const options = await searchSelectOptions(t.engine()!);
    expect(options).toEqual(["Trino", "ClickHouse"]);
    expect(options).not.toContain("pg_lake");
  });

  it("lists ClickHouse after Trino as a normal, selectable row, and choosing it switches the engine and is remembered", async () => {
    const t = mount("sql");
    await searchSelectOptions(t.engine()!);
    expect(document.body.querySelector(".ss-chip")).toBeNull();
    const rows = [...document.body.querySelectorAll<HTMLElement>("[role='option']")];
    expect(rows.map((r) => r.getAttribute("aria-disabled"))).toEqual([null, null]);
    await pickSearchSelect(t.engine()!, "ClickHouse");
    expect(t.store.getState().codegen.activeEngine).toBe("clickhouse");
    expect(t.engine()!.textContent).toBe("ClickHouse");
    expect(localStorage.getItem("codegen.activeEngine.v1")).toBe("clickhouse");
  });

  it("is there for SQL and dbt, and gone for Python and FlinkSQL (they have one engine each)", () => {
    expect(mount("sql").engine()).toBeTruthy();
    document.body.innerHTML = "";
    const t = mount("sql");
    act(() => { t.store.dispatch(setCodegenTarget("dbt")); });
    expect(t.engine()).toBeTruthy();
    for (const target of ["python", "flinksql"]) {
      act(() => { t.store.dispatch(setCodegenTarget(target)); });
      expect(t.engine()).toBeNull();
    }
  });

  it("a SQL turn sends Trino by default and again on the next turn", async () => {
    const t = mount("sql");
    await t.send("first");
    expect(api.submitted.at(-1)!.body).toMatchObject({ target: "sql", engine: "trino" });
    await t.send("second");
    expect(api.submitted.at(-1)!.body).toMatchObject({ target: "sql", engine: "trino" });
  });

  it("a dbt turn sends it too", async () => {
    const t = mount("sql");
    act(() => { t.store.dispatch(setCodegenEngine("trino")); t.store.dispatch(setCodegenTarget("dbt")); });
    await t.send("a model");
    expect(api.submitted.at(-1)!.body).toMatchObject({ target: "dbt", engine: "trino" });
  });

  it("a SQL turn and a dbt turn send ClickHouse once it is chosen", async () => {
    const t = mount("sql");
    act(() => { t.store.dispatch(setCodegenEngine("clickhouse")); });
    await t.send("revenue per day");
    expect(api.submitted.at(-1)!.body).toMatchObject({ target: "sql", engine: "clickhouse" });
    act(() => { t.store.dispatch(setCodegenTarget("dbt")); });
    await t.send("a model");
    expect(api.submitted.at(-1)!.body).toMatchObject({ target: "dbt", engine: "clickhouse" });
  });

  it("a Python or FlinkSQL turn sends no engine, even after Trino was chosen", async () => {
    const t = mount("py");
    act(() => { t.store.dispatch(setCodegenEngine("trino")); });
    await t.send("a script");
    expect(api.submitted.at(-1)!.body.target).toBe("python");
    expect(api.submitted.at(-1)!.body.engine).toBeUndefined();
  });

  it("only a selectable engine is ever stored: Trino or ClickHouse, not pg_lake or anything else", () => {
    const t = mount("sql");
    act(() => { t.store.dispatch(setCodegenEngine("trino")); });
    expect(localStorage.getItem("codegen.activeEngine.v1")).toBe("trino");
    for (const other of ["pg_lake", "oracle"]) act(() => { t.store.dispatch(setCodegenEngine(other)); });
    expect(t.store.getState().codegen.activeEngine).toBe("trino");
    act(() => { t.store.dispatch(setCodegenEngine("clickhouse")); });
    expect(t.store.getState().codegen.activeEngine).toBe("clickhouse");
    expect(localStorage.getItem("codegen.activeEngine.v1")).toBe("clickhouse");
  });

  it("on a reload Trino or ClickHouse comes back, and a saved pg_lake (no longer offered) or junk falls back to Trino", async () => {
    for (const [saved, expected] of [["trino", "trino"], ["clickhouse", "clickhouse"], ["pg_lake", "trino"], ["oracle", "trino"], [null, "trino"]] as const) {
      localStorage.clear();
      if (saved) localStorage.setItem("codegen.activeEngine.v1", saved);
      vi.resetModules();
      const fresh = await import("../../src/features/codegen/codegenSlice");
      expect(fresh.default(undefined, { type: "@@init" }).activeEngine).toBe(expected);
    }
  });
});

describe("assistant: narrow panel", () => {
  it("the two dropdowns share one wrapping row: side by side when the panel is wide, the engine under the target when it is narrow", () => {
    const t = mount("sql");
    const slot = (el: HTMLElement) => { let n: HTMLElement = el; while (n.parentElement && n.parentElement.style.flexWrap !== "wrap") n = n.parentElement; return n; };
    const target = slot(t.host.querySelector<HTMLElement>("[aria-label='What to generate']")!);
    const engine = slot(t.engine()!);
    expect(target.parentElement).toBe(engine.parentElement);                     // the same row
    expect(target.parentElement!.style.flexWrap).toBe("wrap");
    expect(target.nextElementSibling).toBe(engine);                              // engine right after the target (under it once wrapped)
    for (const el of [target, engine]) {
      expect(el.style.flexBasis).toBe("118px");                                  // each needs at least this much, so two fit only in a wide panel
      expect(el.style.width).toBe("");                                           // no fixed full width forcing one per line
    }
  });

  it("the panel's columns may shrink below their content, so a narrow panel never pushes the right side out of view", () => {
    const t = mount("sql");
    const aside = t.engine()!.closest("aside")!;
    expect(aside.style.gridTemplateColumns).toBe("minmax(0, 1fr)");
    const textarea = aside.querySelector("textarea")!;
    expect(parseFloat(textarea.style.minWidth)).toBe(0);                                  // the input may shrink so Send stays visible
  });

  it("a Python turn (no engine) has just the one dropdown", () => {
    const t = mount("py");
    expect(t.engine()).toBeNull();
    expect(t.host.querySelector("[aria-label='What to generate']")).toBeTruthy();
  });

  it("the engine menu opens inside the window when its dropdown sits at the right edge", async () => {
    const t = mount("sql");
    const trigger = t.engine()!;
    Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
    trigger.getBoundingClientRect = () => ({ left: 900, right: 990, top: 100, bottom: 130, width: 90, height: 30, x: 900, y: 100, toJSON() {} });
    await act(async () => { trigger.click(); });
    const menu = document.body.querySelector<HTMLElement>(".ss-menu")!;
    const left = parseFloat(menu.style.left);
    const width = parseFloat(menu.style.width);
    expect(left + width).toBeLessThanOrEqual(1000);
    expect(left).toBeGreaterThanOrEqual(0);
  });
});
