/**
 * Codegen chat → "Clear history".
 *
 * Unlike the other panel tests this one runs the REAL store and the REAL RTK Query cache (only `fetch` is faked): the bug a mocked hook
 * hides is the one that matters here — a cleared chat that comes back because the query cache or a stored pointer still has it.
 */
import { createRoot, type Root } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { workspaceApi } from "../../src/api/client";
import { CodegenChatPanel, codegenReducer } from "../../src/features/codegen";
import { beginCodegenTurn, clearCodegenHistory, failCodegenTurn, setCodegenSession } from "../../src/features/codegen/codegenSlice";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const msg = (id: string, role: "user" | "assistant", text: string) => ({
  id, role, content: [{ type: "text", text }], generated_path: null, generated_code: null, validation_status: null, created_at: "2026-10-17T00:00:00Z",
});
const server = { messages: {} as Record<string, unknown[]>, calls: [] as string[], created: 0 };

// RTK's fetchBaseQuery builds `new Request("/api/v1/…")`, which Node's Request refuses without a host.
const NodeRequest = globalThis.Request;
beforeEach(() => {
  server.messages = { s1: [msg("m1", "user", "create a sql file"), msg("m2", "assistant", "Here is your SQL for workspace ONE")],
                      s2: [msg("m3", "user", "hello two"), msg("m4", "assistant", "Answer for workspace TWO")] };
  server.calls = []; server.created = 0; localStorage.clear(); document.body.innerHTML = "";
  globalThis.Request = class extends NodeRequest { constructor(u: string | Request, init?: RequestInit) { super(typeof u === "string" ? new URL(u, "http://localhost").toString() : u, init); } } as typeof Request;
  globalThis.fetch = (async (input: Request) => {
    const url = new URL(input.url); server.calls.push(`${input.method} ${url.pathname}`);
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    const m = url.pathname.match(/\/codegen\/sessions\/([^/]+)\/messages$/);
    if (m && input.method === "GET") return json(server.messages[m[1]] ?? []);
    if (url.pathname.endsWith("/codegen/sessions") && input.method === "POST") { server.created += 1; return json({ id: `fresh-${server.created}` }, 201); }
    if (m && input.method === "POST") return json({ context_included: false }, 202);
    return json({}, 404);
  }) as typeof fetch;
});
afterEach(() => { globalThis.Request = NodeRequest; });

function makeStore() {
  return configureStore({
    reducer: { [workspaceApi.reducerPath]: workspaceApi.reducer, codegen: codegenReducer, ui: (s = { activeConnectionId: null }) => s },
    middleware: (d) => d({ serializableCheck: false }).concat(workspaceApi.middleware),
  });
}

async function settle() { await act(async () => { await new Promise((r) => setTimeout(r, 30)); }); }

async function mount(workspaceId: string, sessions: Record<string, string>) {
  const store = makeStore();
  for (const [ws, sid] of Object.entries(sessions)) act(() => { store.dispatch(setCodegenSession({ workspaceId: ws, sessionId: sid })); });
  const host = document.createElement("div"); document.body.appendChild(host);
  const root: Root = createRoot(host);
  const render = (ws: string) => act(() => root.render(createElement(Provider, { store, children: createElement(CodegenChatPanel, {
    workspaceId: ws, activeFileId: null, activeFileContent: null, activeFileLanguage: "sql", execSessionId: "x", onArtifactApplied() {}, onInsertCode() {},
  }) })));
  render(workspaceId); await settle();
  const clearBtn = () => host.querySelector<HTMLButtonElement>("button[aria-label$=\"chat history\"], button[aria-label=\"Nothing to clear\"]")!;
  const type = async (text: string) => {
    const box = host.querySelector("textarea")!;
    await act(async () => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, text); box.dispatchEvent(new Event("input", { bubbles: true })); });
  };
  const send = async () => { await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Send")!.click(); }); await settle(); };
  return { store, host, render, clearBtn, type, send };
}

describe("codegen chat: Clear history", () => {
  it("is in the panel header and is enabled only while the chat has something in it", async () => {
    const withChat = await mount("ws-1", { "ws-1": "s1" });
    expect(withChat.host.textContent).toContain("Here is your SQL for workspace ONE");
    expect(withChat.clearBtn()).toBeTruthy();
    expect(withChat.clearBtn().disabled).toBe(false);
    document.body.innerHTML = "";
    const empty = await mount("ws-9", {});
    expect(empty.clearBtn().disabled).toBe(true);
  });

  it("empties the chat immediately on click, with nothing refetched or reloaded, and then disables itself", async () => {
    const t = await mount("ws-1", { "ws-1": "s1" });
    const before = server.calls.length;
    await act(async () => { t.clearBtn().click(); });          // one click, no waiting, no re-render trigger other than the click itself
    expect(t.host.textContent).not.toContain("Here is your SQL for workspace ONE");
    expect(t.host.textContent).not.toContain("create a sql file");
    expect(t.clearBtn().disabled).toBe(true);
    await settle();
    expect(t.host.textContent).not.toContain("Here is your SQL");                 // and it does not come back once the cache catches up
    expect(server.calls.slice(before).filter((c) => c.includes("/sessions/s1/messages"))).toEqual([]);
  });

  it("clears only this workspace: the other workspace keeps its chat, in the store and in what is saved for a reload", async () => {
    const t = await mount("ws-1", { "ws-1": "s1", "ws-2": "s2" });
    await act(async () => { t.clearBtn().click(); });
    expect(t.store.getState().codegen.panelSessionByWorkspace).toEqual({ "ws-2": "s2" });
    expect(JSON.parse(localStorage.getItem("codegen.panelSessions.v1")!)).toEqual({ "ws-2": "s2" });   // what a refresh would load
    t.render("ws-2"); await settle();                                                                    // switching workspace / tab
    expect(t.host.textContent).toContain("Answer for workspace TWO");
    expect(t.clearBtn().disabled).toBe(false);
    t.render("ws-1"); await settle();                                                                    // and back: still empty
    expect(t.host.textContent).not.toContain("workspace ONE");
    expect(t.clearBtn().disabled).toBe(true);
  });

  it("starts a brand-new session on the next message, and the old conversation does not return", async () => {
    const t = await mount("ws-1", { "ws-1": "s1" });
    await act(async () => { t.clearBtn().click(); });
    await t.type("a new question"); await t.send();
    expect(server.created).toBe(1);
    expect(t.store.getState().codegen.panelSessionByWorkspace["ws-1"]).toBe("fresh-1");
    expect(server.calls).toContain("POST /api/v1/codegen/sessions/fresh-1/messages");
    expect(t.host.textContent).not.toContain("Here is your SQL for workspace ONE");
  });

  it("also drops this workspace's in-flight or failed turn, but never another workspace's", async () => {
    const t = await mount("ws-1", { "ws-1": "s1", "ws-2": "s2" });
    act(() => { t.store.dispatch(beginCodegenTurn("s1")); });
    await settle();
    expect(t.host.textContent).toContain("Working…");
    await act(async () => { t.clearBtn().click(); });
    expect(t.store.getState().codegen.status).toBe("idle");
    expect(t.host.textContent).not.toContain("Working…");
    // another workspace's turn is untouched
    act(() => { t.store.dispatch(beginCodegenTurn("s2")); });
    act(() => { t.store.dispatch(clearCodegenHistory({ workspaceId: "ws-1" })); });
    expect(t.store.getState().codegen.sessionId).toBe("s2");
    expect(t.store.getState().codegen.status).toBe("thinking");
  });

  it("clears a stale error that no chat owns (a failure before any session existed)", async () => {
    const t = await mount("ws-9", {});
    act(() => { t.store.dispatch(failCodegenTurn("No active workspace session yet — try again in a moment.")); });
    expect(t.host.textContent).toContain("Generation failed");
    expect(t.clearBtn().disabled).toBe(false);                         // there is something on screen, so it can be cleared
    await act(async () => { t.clearBtn().click(); });
    expect(t.host.textContent).not.toContain("Generation failed");
    expect(t.clearBtn().disabled).toBe(true);
  });
});
