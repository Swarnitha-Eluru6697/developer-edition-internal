import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import MLStudioApp from "../src/MLStudioApp";
import { effectivePermissions } from "../src/context/PermissionsContext";

// "View as role": the portal passes the permission IDs of the role the user
// chose. ML Studio shows from /api/me ∩ viewPermissions — never more.
function mockApi(permissions: string[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/me")) {
      return new Response(JSON.stringify({ user: "alice", userId: "u-1", tenant: "acme", permissions }), { status: 200 });
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
const HELD = ["mlstudio:access:view", "mlstudio:dashboard:view", "mlstudio:pipelines:view", "mlstudio:experiments:view"];

describe("effectivePermissions", () => {
  it("null/undefined leave the held set unchanged", () => {
    expect(effectivePermissions(HELD, null)).toEqual(HELD);
    expect(effectivePermissions(HELD, undefined)).toEqual(HELD);
  });

  it("narrows to the intersection and never adds a permission the user lacks", () => {
    expect(effectivePermissions(HELD, ["mlstudio:experiments:view", "mlstudio:deploy:promote"])).toEqual(["mlstudio:experiments:view"]);
  });
});

describe("view as role", () => {
  it("narrowing hides a tab the full set shows", async () => {
    mockApi(HELD);
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/" viewPermissions={["mlstudio:access:view", "mlstudio:dashboard:view"]} />);
    expect(await within(nav()).findByText("Dashboard")).toBeTruthy();
    expect(within(nav()).queryByText("Pipelines")).toBeNull();
    expect(within(nav()).queryByText("Experiments")).toBeNull();
  });

  it("viewPermissions can never add a permission the user lacks", async () => {
    mockApi(["mlstudio:access:view", "mlstudio:dashboard:view"]);
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" viewPermissions={["mlstudio:dashboard:view", "mlstudio:experiments:view"]} />);
    expect(await within(nav()).findByText("Dashboard")).toBeTruthy();
    expect(within(nav()).queryByText("Experiments")).toBeNull();
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Experiments" })).toBeNull();
  });

  it("null and undefined show the full set", async () => {
    mockApi(HELD);
    const { unmount } = render(<MLStudioApp apiBase="" embedded={false} initialPath="/" viewPermissions={null} />);
    expect(await within(nav()).findByText("Experiments")).toBeTruthy();
    expect(within(nav()).getByText("Pipelines")).toBeTruthy();
    unmount();
    render(<MLStudioApp apiBase="" embedded={false} initialPath="/" />);
    expect(await within(nav()).findByText("Experiments")).toBeTruthy();
    expect(within(nav()).getByText("Pipelines")).toBeTruthy();
  });

  it("switching updates the tabs without a remount and leaves a page no longer allowed", async () => {
    const fetchMock = mockApi(HELD);
    const { rerender } = render(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" />);
    expect(await screen.findByRole("heading", { name: "Experiments" })).toBeTruthy();

    // Switch to a role without experiments: the tab goes and the open page moves to the first allowed one.
    rerender(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" viewPermissions={["mlstudio:dashboard:view"]} />);
    await waitFor(() => expect(within(nav()).queryByText("Experiments")).toBeNull());
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();

    // Back to "All roles": the tab returns.
    rerender(<MLStudioApp apiBase="" embedded={false} initialPath="/experiments" viewPermissions={null} />);
    expect(await within(nav()).findByText("Experiments")).toBeTruthy();

    // Same provider throughout (no remount): /api/me was read once, and viewPermissions never went to the backend.
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls.filter((u) => u.endsWith("/api/me"))).toHaveLength(1);
    for (const [u, init] of fetchMock.mock.calls) {
      expect(String(u)).not.toMatch(/view/i);
      expect(JSON.stringify((init as RequestInit | undefined) ?? {})).not.toContain("viewPermissions");
    }
  });
});
