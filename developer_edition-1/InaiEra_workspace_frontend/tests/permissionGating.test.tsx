/**
 * UI gating from GET /api/v1/me: the hooks read the real RTK Query endpoint (fetch stubbed), so loading and errors are covered
 * too — both mean "no permissions".
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { Provider } from "react-redux";
import { configureStore } from "@reduxjs/toolkit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ jobs: [] as unknown[] }));
vi.mock("../src/features/flink/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListFlinkJobsQuery: () => ({ data: api.jobs, isLoading: false, isError: false, refetch: vi.fn() }),
    useCreateFlinkJobFromGitMutation: mutation, useSyncFlinkJobMutation: mutation, useDeleteFlinkJobMutation: mutation,
    useValidateFlinkJobMutation: mutation, useDeployFlinkJobMutation: mutation, useStopFlinkJobMutation: mutation,
    useGetFlinkUiUrlQuery: () => ({ data: undefined }),
    useLazyGetFlinkJobStatusQuery: () => [vi.fn()],
  };
});

import { workspaceApi } from "../src/api/client";
import uiReducer from "../src/store/uiSlice";
import { FlinkPanel } from "../src/features/flink/components/FlinkPanel";
import { useHasPermission, usePermissionsState } from "../src/rbac/hooks";
import { FakeRequest } from "./helpers/apiHarness";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** Answer GET /api/v1/me with `permissions` (or an HTTP error, or never). */
function serveMe(answer: string[] | number | "pending") {
  vi.stubGlobal("Request", FakeRequest);
  vi.stubGlobal("fetch", vi.fn(async (req: FakeRequest) => {
    if (!req.url.endsWith("/api/v1/me")) return new Response("{}", { status: 404 });
    if (answer === "pending") return new Promise<Response>(() => {});
    if (typeof answer === "number") return new Response(JSON.stringify({ detail: "no" }), { status: answer, headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify({ user: "ada", userId: "sub-1", tenant: "org-1", permissions: answer }), { status: 200, headers: { "content-type": "application/json" } });
  }));
}

function mount(element: React.ReactElement) {
  const store = configureStore({
    reducer: { [workspaceApi.reducerPath]: workspaceApi.reducer, ui: uiReducer },
    middleware: (gDM) => gDM({ serializableCheck: false }).concat(workspaceApi.middleware),
  });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(createElement(Provider, { store, children: element })));
  return { host, unmount: () => act(() => root.unmount()) };
}
const settle = () => act(async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setTimeout(r, 0)); });

const flinkJob = {
  id: "j1", org_id: "o", workspace_id: null, name: "orders_stream", description: null, status: "paused", sink_engine: "pg_lake",
  parallelism: 2, sql: "select 1", validation_errors: [], savepoint_path: null, last_stopped_at: null,
  source: { git_url: "https://github.com/acme/flink.git", git_ref: "main", sql_path: "a.flink.sql", commit_sha: "abc1234" },
};
const buttons = (host: HTMLElement) => [...host.querySelectorAll("button")].map((b) => b.textContent?.trim());
async function openFlinkJob(permissions: string[] | number) {
  serveMe(permissions);
  const t = mount(createElement(FlinkPanel));
  await settle();
  await act(async () => { [...t.host.querySelectorAll<HTMLElement>("div")].find((d) => d.children.length === 0 && d.textContent === "orders_stream")!.click(); });
  return t;
}

beforeEach(() => {
  document.body.innerHTML = "";
  localStorage.clear();
  api.jobs = [flinkJob];
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("Flink job actions", () => {
  it("without workspace:flink:deploy there is no Deploy or Stop", async () => {
    const t = await openFlinkJob(["workspace:access:view", "workspace:flink:edit"]);
    expect(buttons(t.host)).toContain("Validate");
    expect(buttons(t.host)).not.toContain("Deploy");
    expect(buttons(t.host)).not.toContain("Stop");
    t.unmount();
  });

  it("with workspace:flink:deploy, Deploy and Stop are there (and nothing that needs flink:edit)", async () => {
    const t = await openFlinkJob(["workspace:access:view", "workspace:flink:deploy"]);
    expect(buttons(t.host)).toContain("Deploy");
    expect(buttons(t.host)).toContain("Stop");
    expect(buttons(t.host)).not.toContain("Validate");
    t.unmount();
  });

  it("if /api/v1/me fails, nothing gated is shown", async () => {
    const t = await openFlinkJob(500);
    expect(buttons(t.host)).not.toContain("Deploy");
    expect(buttons(t.host)).not.toContain("Validate");
    t.unmount();
  });
});

describe("permission hooks", () => {
  function Probe() {
    const { status, permissions } = usePermissionsState();
    const deploy = useHasPermission("workspace:flink:deploy");
    return createElement("div", { "data-status": status, "data-count": permissions.length, "data-deploy": String(deploy) });
  }
  const probe = (host: HTMLElement) => host.firstElementChild as HTMLElement;

  it("while /api/v1/me is loading the user holds nothing", async () => {
    serveMe("pending");
    const t = mount(createElement(Probe));
    await settle();
    expect(probe(t.host).dataset).toMatchObject({ status: "loading", count: "0", deploy: "false" });
    t.unmount();
  });

  it("another app's permissions and junk are ignored", async () => {
    serveMe(["mlstudio:clusters:view", "workspace:flink:deploy", "admin"]);
    const t = mount(createElement(Probe));
    await settle();
    expect(probe(t.host).dataset).toMatchObject({ status: "ready", count: "1", deploy: "true" });
    t.unmount();
  });

  it("an error is no permissions", async () => {
    serveMe(403);
    const t = mount(createElement(Probe));
    await settle();
    expect(probe(t.host).dataset).toMatchObject({ status: "error", count: "0", deploy: "false" });
    t.unmount();
  });
});
