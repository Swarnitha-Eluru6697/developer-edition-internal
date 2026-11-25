import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import MLStudioApp from "../src/MLStudioApp";

// The UI shows pages from GET <apiBase>/api/me only. Every other API call is
// answered 404 here, so pages render their empty/error states.
function mockApi(me: { ok: boolean; permissions?: string[] }) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/me")) {
      return me.ok
        ? new Response(JSON.stringify({ user: "alice", userId: "u-1", tenant: "acme", permissions: me.permissions ?? [] }), { status: 200 })
        : new Response(JSON.stringify({ error: "request did not come through the gateway" }), { status: 401 });
    }
    return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const nav = () => screen.getByRole("complementary");

describe("pages from permissions", () => {
  it("a user without mlstudio:experiments:view does not see Experiments", async () => {
    mockApi({ ok: true, permissions: ["mlstudio:access:view", "mlstudio:dashboard:view", "mlstudio:pipelines:view"] });
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" />);
    // Permissions loaded: the pages they allow appear…
    expect(await within(nav()).findByText("Dashboard")).toBeTruthy();
    expect(within(nav()).getByText("Pipelines")).toBeTruthy();
    // …Experiments does not, and a deep link to it lands elsewhere.
    expect(within(nav()).queryByText("Experiments")).toBeNull();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Experiments" })).toBeNull());
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();
  });

  it("a user with mlstudio:experiments:view sees Experiments", async () => {
    mockApi({ ok: true, permissions: ["mlstudio:access:view", "mlstudio:experiments:view"] });
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" />);
    expect(await within(nav()).findByText("Experiments")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Experiments" })).toBeTruthy();
    // Only what is held: no Dashboard entry without mlstudio:dashboard:view.
    expect(within(nav()).queryByText("Dashboard")).toBeNull();
  });

  it("unknown permissions (the /api/me call fails) show no page at all", async () => {
    mockApi({ ok: false });
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" />);
    expect(await screen.findByText(/could not read your access/)).toBeTruthy();
    expect(within(nav()).queryByRole("link")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Experiments" })).toBeNull();
  });

  it("while permissions are loading nothing is shown", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" />);
    expect(within(nav()).queryByRole("link")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Experiments" })).toBeNull();
  });

  it("reads /api/me under the host's apiBase and sends no identity or role headers", async () => {
    const fetchMock = mockApi({ ok: true, permissions: ["mlstudio:experiments:view"] });
    render(<MLStudioApp apiBase="/control_plane/api/dataplane/mlstudio" embedded initialPath="/experiments" />);
    expect(await screen.findByRole("heading", { name: "Experiments" })).toBeTruthy();
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls).toContain("/control_plane/api/dataplane/mlstudio/api/me");
    for (const [, init] of fetchMock.mock.calls) {
      const names = Object.keys((init as RequestInit | undefined)?.headers ?? {}).map((h) => h.toLowerCase());
      expect(names.filter((h) => h.startsWith("x-user") || h === "x-tenant-id" || h.startsWith("x-inaiera"))).toEqual([]);
    }
  });
});
