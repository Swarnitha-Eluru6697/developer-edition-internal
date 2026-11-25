/** Which workspace the editor shows as workspaces are created and deleted (the real hook, list mocked). */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import type { CurrentUser } from "../../src/types";

const api = { listed: undefined as { id: string; name: string }[] | undefined };
vi.mock("../../src/features/workspace/api", () => ({ useListWorkspacesQuery: () => ({ data: api.listed }) }));
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => null }));

import { useActiveWorkspace } from "../../src/features/workspace/hooks/useActiveWorkspace";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ME = {} as CurrentUser;
const ws = (...ids: string[]) => ids.map((id) => ({ id, name: id }));

function mount() {
  const result = { current: undefined as unknown as ReturnType<typeof useActiveWorkspace> };
  const Probe = () => { result.current = useActiveWorkspace(ME); return null; };
  const root = createRoot(document.createElement("div"));
  const render = () => act(() => { root.render(createElement(Probe)); });
  render();
  /** The list changed (a refetch landed): render again. */
  const setList = (ids: string[]) => { api.listed = ws(...ids); render(); };
  return { result, setList };
}

describe("active workspace", () => {
  it("starts on the first workspace", () => {
    api.listed = ws("a", "b");
    const { result } = mount();
    expect(result.current.activeWs).toBe("a");
  });

  it("stays on a workspace you just created, even before the list has refetched (the bug)", () => {
    api.listed = ws("a", "b");
    const { result, setList } = mount();
    act(() => result.current.setActiveWs("sample"));            // created -> selected straight away
    expect(result.current.activeWs).toBe("sample");             // the stale list must not bounce us back
    setList(["a", "b"]);                                        // another render with the same stale list
    expect(result.current.activeWs).toBe("sample");
    setList(["a", "b", "sample"]);                              // the refetch lands
    expect(result.current.activeWs).toBe("sample");
  });

  it("moves to the next workspace when the active one is deleted (and never back to the deleted one)", () => {
    api.listed = ws("a", "b");
    const { result, setList } = mount();
    expect(result.current.activeWs).toBe("a");
    act(() => result.current.dropWorkspace("a"));               // deleted; the list still contains it
    expect(result.current.activeWs).toBe("b");
    expect(result.current.workspaces?.map((w) => w.id)).toEqual(["b"]);
    setList(["b"]);                                             // the refetch drops it
    expect(result.current.activeWs).toBe("b");
  });

  it("deleting a workspace that is not the active one leaves the selection alone", () => {
    api.listed = ws("a", "b");
    const { result } = mount();
    act(() => result.current.dropWorkspace("b"));
    expect(result.current.activeWs).toBe("a");
  });

  it("falls back when the active workspace disappears from the list (deleted elsewhere)", () => {
    api.listed = ws("a", "b");
    const { result, setList } = mount();
    act(() => result.current.setActiveWs("b"));
    setList(["a"]);
    expect(result.current.activeWs).toBe("a");
  });

  it("has no workspace after the last one is deleted", () => {
    api.listed = ws("a");
    const { result } = mount();
    act(() => result.current.dropWorkspace("a"));
    expect(result.current.activeWs).toBeNull();
  });
});
