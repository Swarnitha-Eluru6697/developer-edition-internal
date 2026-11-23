/** Unsaved changes: dirty tracking, new files that exist only in the editor, and the close prompt. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { describe, expect, it } from "vitest";
import { editorContent, useEditorDrafts } from "../../src/features/workspace/hooks/useEditorDrafts";
import { parseCells, toIpynb } from "../../src/features/notebook/notebookFormat";
import { useLocalBuffers } from "../../src/features/workspace/hooks/useLocalBuffers";
import {
  isLocalBufferId, localBufferFile, localBufferName, localBufferSaveName, nextUntitledNumber,
} from "../../src/features/workspace/localBuffers";
import { askSaveChanges, currentDialog, popDialog } from "../../src/shared/ui/dialogStore";
import type { FileWithContent } from "../../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** Minimal renderHook: mounts a component that runs `fn` on every render. */
function renderHook<T>(fn: () => T) {
  const result = { current: undefined as unknown as T };
  const Probe = () => { result.current = fn(); return null; };
  const root = createRoot(document.createElement("div"));
  act(() => { root.render(createElement(Probe)); });
  return { result, unmount: () => act(() => root.unmount()) };
}

const serverFile = (id: string, content: string) => ({ id, content } as FileWithContent);

describe("unsaved changes: dirty tracking", () => {
  it("a loaded file is clean, becomes dirty when edited, and clean again once saved", () => {
    const { result } = renderHook(() => useEditorDrafts(serverFile("a", "select 1"), "a"));
    expect(result.current.drafts.a).toBe("select 1");
    expect(result.current.isDirty("a")).toBe(false);

    act(() => result.current.setDraft("a", "select 2"));
    expect(result.current.isDirty("a")).toBe(true);
    expect([...result.current.dirtyIds]).toEqual(["a"]);

    act(() => result.current.markSaved("a", "select 2"));
    expect(result.current.isDirty("a")).toBe(false);

    act(() => result.current.setDraft("a", "select 3"));
    act(() => result.current.setDraft("a", "select 2"));          // typed back to the saved text
    expect(result.current.isDirty("a")).toBe(false);
  });

  it("Don't Save forgets the text, so reopening shows the saved file again", () => {
    const file = serverFile("a", "saved");
    const { result } = renderHook(() => useEditorDrafts(file, "a"));
    act(() => result.current.setDraft("a", "edited"));
    expect(result.current.isDirty("a")).toBe(true);

    act(() => result.current.forget("a"));
    expect(result.current.isDirty("a")).toBe(false);
    expect(result.current.drafts.a).toBeUndefined();               // re-seeded from the server on next open
  });

  it("reset drops every draft and baseline at once", () => {
    const { result } = renderHook(() => useEditorDrafts(undefined, null));
    act(() => result.current.startBuffer("a", "x", ""));
    act(() => result.current.startBuffer("b", "y", ""));
    expect(result.current.dirtyIds.size).toBe(2);
    act(() => result.current.reset());
    expect(result.current.dirtyIds.size).toBe(0);
    expect(result.current.drafts).toEqual({});
  });

  it("a notebook is compared in the form its editor writes, so opening one does not make it dirty", () => {
    // stored with different formatting / metadata than toIpynb writes
    const raw = JSON.stringify({ nbformat: 4, nbformat_minor: 4, metadata: {}, cells: [
      { cell_type: "code", source: ["a = 1\n", "b = 2"], outputs: [], metadata: {}, execution_count: 3 },
      { cell_type: "code", source: "print(a)", outputs: [], metadata: {}, execution_count: null },
    ] });
    const file = { id: "n", language: "ipynb", content: raw } as FileWithContent;
    const { result } = renderHook(() => useEditorDrafts(file, "n"));
    expect(result.current.drafts.n).toBe(editorContent("ipynb", raw));
    expect(result.current.isDirty("n")).toBe(false);

    const edited = toIpynb(parseCells(raw).map((c, i) => (i === 1 ? { ...c, source: "print(b)" } : c)));
    act(() => result.current.setDraft("n", edited));            // what the notebook editor reports after an edit
    expect(result.current.isDirty("n")).toBe(true);
    act(() => result.current.markSaved("n", edited));
    expect(result.current.isDirty("n")).toBe(false);
  });

  it("editorContent leaves every other kind of file alone", () => {
    expect(editorContent("sql", "select 1")).toBe("select 1");
    expect(editorContent("yaml", "a: 1\n")).toBe("a: 1\n");
  });

  it("nothing is dirty before a file's content has loaded", () => {
    const { result } = renderHook(() => useEditorDrafts(undefined, "a"));
    expect(result.current.isDirty("a")).toBe(false);
  });

  it("a new buffer: untouched template is clean, text from codegen (baseline empty) is dirty", () => {
    const { result } = renderHook(() => useEditorDrafts(undefined, null));
    act(() => result.current.startBuffer("local:1", "-- template", "-- template"));
    act(() => result.current.startBuffer("local:2", "select 1", ""));
    expect(result.current.isDirty("local:1")).toBe(false);
    expect(result.current.isDirty("local:2")).toBe(true);
    act(() => result.current.appendToDraft("local:1", "\nselect 1"));
    expect(result.current.isDirty("local:1")).toBe(true);
  });
});

describe("unsaved changes: new files live in the editor until saved", () => {
  it("names and ids — every kind the Add New menu offers starts as an unsaved buffer", () => {
    expect(isLocalBufferId("local:3")).toBe(true);
    expect(isLocalBufferId("11111111-1111-1111-1111-111111111111")).toBe(false);
    expect(isLocalBufferId(null)).toBe(false);
    expect(localBufferName(1, "sql")).toBe("Untitled-1.sql");
    expect(localBufferName(2, "flinksql")).toBe("Untitled-2.flink.sql");
    expect(localBufferName(3, "ipynb")).toBe("Untitled-3.ipynb");
    expect(localBufferName(4, "py")).toBe("Untitled-4.py");
    expect(localBufferName(5, "yaml")).toBe("Untitled-5.yml");
    expect(["sql", "flinksql", "ipynb", "py", "yaml"].map((l) => localBufferSaveName(l as never)))
      .toEqual(["query.sql", "query.flink.sql", "notebook.ipynb", "script.py", "config.yml"]);
  });

  it("opens numbered buffers and removes them", () => {
    const { result } = renderHook(() => useLocalBuffers());
    let a = { id: "" }, b = { id: "" };
    act(() => { a = result.current.create("sql"); });
    act(() => { b = result.current.create("flinksql"); });
    expect(Object.values(result.current.buffers).map((x) => x.name)).toEqual(["Untitled-1.sql", "Untitled-2.flink.sql"]);
    act(() => result.current.remove(a.id));
    expect(Object.keys(result.current.buffers)).toEqual([b.id]);
  });

  it("reuses the lowest free number: closing Untitled-1 and opening another gives Untitled-1 again", () => {
    expect(nextUntitledNumber([])).toBe(1);
    expect(nextUntitledNumber([{ number: 1 }, { number: 2 }])).toBe(3);
    expect(nextUntitledNumber([{ number: 2 }, { number: 3 }])).toBe(1);   // 1 was closed
    expect(nextUntitledNumber([{ number: 1 }, { number: 3 }])).toBe(2);   // fills the gap

    const { result } = renderHook(() => useLocalBuffers());
    const names = () => Object.values(result.current.buffers).map((x) => x.name);
    let first = { id: "" };
    act(() => { first = result.current.create("sql"); });
    expect(names()).toEqual(["Untitled-1.sql"]);
    act(() => result.current.remove(first.id));
    act(() => { result.current.create("sql"); });                          // reopened: still Untitled-1
    expect(names()).toEqual(["Untitled-1.sql"]);
    act(() => { result.current.create("sql"); result.current.create("flinksql"); });   // back to back: distinct numbers
    expect(names()).toEqual(["Untitled-1.sql", "Untitled-2.sql", "Untitled-3.flink.sql"]);
    // the id is never reused, even though the number is
    expect(new Set(Object.keys(result.current.buffers)).size).toBe(3);
    expect(Object.keys(result.current.buffers)).not.toContain(first.id);
  });

  it("clear removes every open buffer", () => {
    const { result } = renderHook(() => useLocalBuffers());
    act(() => { result.current.create("sql"); result.current.create("sql"); });
    expect(Object.keys(result.current.buffers)).toHaveLength(2);
    act(() => result.current.clear());
    expect(result.current.buffers).toEqual({});
    act(() => { result.current.create("sql"); });                        // numbering starts over
    expect(Object.values(result.current.buffers)[0].name).toBe("Untitled-1.sql");
  });

  it("looks like a file to the rest of the editor, with no server identity", () => {
    const f = localBufferFile({ id: "local:1", number: 1, name: "Untitled-1.sql", language: "sql" }, "ws-1", "select 1");
    expect(f).toMatchObject({ id: "local:1", path: "Untitled-1.sql", language: "sql", content: "select 1", sha256: null, workspace_id: "ws-1" });
  });
});

describe("unsaved changes: the close prompt", () => {
  it("offers Save / Don't Save / Cancel and returns the choice", async () => {
    const pending = askSaveChanges("README.md");
    const dialog = currentDialog();
    expect(dialog).toMatchObject({ kind: "save", title: "Save changes to README.md?" });
    if (dialog?.kind !== "save") throw new Error("expected a save dialog");
    dialog.resolve("discard");
    popDialog();
    await expect(pending).resolves.toBe("discard");
  });
});
