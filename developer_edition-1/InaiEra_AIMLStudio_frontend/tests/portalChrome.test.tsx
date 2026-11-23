import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import MLStudioApp from "../src/MLStudioApp";

// /api/me grants the dashboard; every other call is answered 404.
function mockApi() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith("/api/me")) {
      return new Response(JSON.stringify({ user: "alice", userId: "u-1", tenant: "acme", permissions: ["mlstudio:access:view", "mlstudio:dashboard:view"] }), { status: 200 });
    }
    return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
  }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const bar = () => document.querySelector("header.topbar") as HTMLElement;

describe("portal chrome", () => {
  it("hosted (roleBadges given): the role strip is drawn, ML Studio's bell and avatar are not", async () => {
    mockApi();
    render(<MLStudioApp apiBase="" embedded={false} roleBadges={<span>ML Engineer | ML Viewer</span>} />);
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();
    expect(bar().querySelector(".topbar-roles")?.textContent).toBe("ML Engineer | ML Viewer");
    expect(bar().querySelector(".notif")).toBeNull();
    expect(bar().querySelector(".topbar-avatar")).toBeNull();
    // The app's own logo still leads its sidebar.
    expect(screen.getByRole("complementary").firstElementChild?.className).toBe("brand");
  });

  it("standalone (no roleBadges): bell and avatar stay, no role strip", async () => {
    mockApi();
    render(<MLStudioApp apiBase="" embedded={false} />);
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeTruthy();
    expect(bar().querySelector(".topbar-roles")).toBeNull();
    expect(bar().querySelector(".notif")).toBeTruthy();
    expect(screen.getByLabelText("Signed in as alice")).toBeTruthy();
  });

  it("an older host's accountMenu still takes the avatar's place", async () => {
    mockApi();
    render(<MLStudioApp apiBase="" embedded={false} accountMenu={<button type="button">Account</button>} />);
    expect(await screen.findByRole("button", { name: "Account" })).toBeTruthy();
    expect(bar().querySelector(".topbar-avatar")).toBeNull();
  });
});
