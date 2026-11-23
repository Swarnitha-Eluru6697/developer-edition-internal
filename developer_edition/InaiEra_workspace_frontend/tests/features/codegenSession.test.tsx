/** Sending from the AI panel when the open file is an unsaved local one ("Untitled-1.sql"), and when the session cannot be created. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ created: [] as Record<string, unknown>[], fail: false, submitted: [] as unknown[] }));
vi.mock("../../src/api/client", () => {
  const unwrap = (v: unknown) => ({ unwrap: async () => v });
  return {
    workspaceApi: { util: { invalidateTags: (t: unknown) => ({ type: "noop", payload: t }) } },
    useCreateCodegenSessionMutation: () => [(body: Record<string, unknown>) => {
      api.created.push(body);
      if (api.fail) return { unwrap: async () => { throw { status: 422, data: { detail: { message: "file_id: not a valid UUID" } } }; } };
      return unwrap({ id: "sess-new" });
    }, { isLoading: false }],
    useSubmitCodegenPromptMutation: () => [(arg: unknown) => { api.submitted.push(arg); return unwrap({ context_included: false }); }, { isLoading: false }],
    useApplyCodegenArtifactMutation: () => [vi.fn(), { isLoading: false }],
    useListCodegenMessagesQuery: () => ({ data: undefined, error: undefined }),
  };
});

import { CodegenChatPanel, codegenReducer } from "../../src/features/codegen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount(activeFileId: string | null) {
  const store = configureStore({ reducer: { codegen: codegenReducer, ui: (s = { activeConnectionId: null }) => s } });
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(Provider, { store, children: createElement(CodegenChatPanel, {
    workspaceId: "ws-1", activeFileId, activeFileContent: null, activeFileLanguage: "sql", execSessionId: "x", onArtifactApplied() {}, onInsertCode() {},
  }) })));
  const send = async (text: string) => {
    const box = host.querySelector("textarea")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Send")!.click(); });
  };
  return { store, host, send };
}

beforeEach(() => { api.created = []; api.submitted = []; api.fail = false; localStorage.clear(); document.body.innerHTML = ""; });

describe("codegen: starting a chat session", () => {
  it("does not send an unsaved local file's id (the backend rejects it with a 422) and the message goes out", async () => {
    const { store, send } = mount("local:abc-123");
    await send("create a sql file");
    expect(api.created).toHaveLength(1);
    expect(api.created[0].file_id).toBeUndefined();
    expect(api.submitted).toHaveLength(1);
    expect(store.getState().codegen.panelSessionByWorkspace).toEqual({ "ws-1": "sess-new" });
  });

  it("still links a saved file to the session", async () => {
    const { send } = mount("0b5e4c1e-9f6f-4d0e-8a2b-3f2d5a7c9e10");
    await send("hi");
    expect(api.created[0].file_id).toBe("0b5e4c1e-9f6f-4d0e-8a2b-3f2d5a7c9e10");
  });

  it("says so when the session cannot be created, instead of doing nothing", async () => {
    api.fail = true;
    const { host, send } = mount("0b5e4c1e-9f6f-4d0e-8a2b-3f2d5a7c9e10");
    await send("hi");
    expect(host.textContent).toContain("Generation failed");
    expect(host.textContent).toContain("not a valid UUID");
    expect(api.submitted).toHaveLength(0);
  });
});
