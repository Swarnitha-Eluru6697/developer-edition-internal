/** Jobs → Flink: refreshing the page keeps the job whose page you had open (it used to drop you back at the list). */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ jobs: undefined as unknown[] | undefined }));
vi.mock("../../src/features/flink/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListFlinkJobsQuery: () => ({ data: api.jobs, isLoading: api.jobs === undefined, isError: false, refetch: vi.fn() }),
    useCreateFlinkJobFromGitMutation: mutation, useSyncFlinkJobMutation: mutation, useDeleteFlinkJobMutation: mutation,
    useValidateFlinkJobMutation: mutation, useDeployFlinkJobMutation: mutation, useStopFlinkJobMutation: mutation,
    useGetFlinkUiUrlQuery: () => ({ data: undefined }),
    useLazyGetFlinkJobStatusQuery: () => [vi.fn()],
  };
});
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn(), useSelector: () => undefined }));
// Every Workspace permission: these specs are about the screen, not about gating.
vi.mock("../../src/rbac/hooks", async () => {
  const { PERMISSIONS } = await import("../../src/rbac/generated/permissions");
  return { useHasPermission: () => true, useHasAnyPermission: () => true, usePermissions: () => PERMISSIONS };
});

import { FlinkPanel } from "../../src/features/flink/components/FlinkPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const job = (id: string, name: string) => ({
  id, org_id: "o", workspace_id: null, name, description: null, status: "paused", sink_engine: "pg_lake", parallelism: 2, sql: "select 1",
  validation_errors: [], savepoint_path: null, last_stopped_at: null,
  source: { git_url: "https://github.com/acme/flink-test.git", git_ref: "main", sql_path: "jobs/a.flink.sql", commit_sha: "abcdef1234567" },
});

let screens: { unmount: () => void }[] = [];
function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const render = () => act(() => root.render(createElement(FlinkPanel)));
  render();
  const handle = { host, render, unmount: () => act(() => root.unmount()) };
  screens.push(handle);
  return handle;
}
const inJob = (host: HTMLElement) => !!host.querySelector('button[title="Back to all jobs"]');
const jobName = (host: HTMLElement) => host.querySelector('button[title="Back to all jobs"]')?.nextElementSibling?.firstElementChild?.textContent;
const openJob = async (host: HTMLElement, name: string) =>
  act(async () => { [...host.querySelectorAll<HTMLElement>("div")].find((d) => d.children.length === 0 && d.textContent === name)!.click(); });

beforeEach(() => {
  screens.forEach((s) => { try { s.unmount(); } catch { /* gone */ } });
  screens = [];
  document.body.innerHTML = "";
  api.jobs = [job("j1", "live_orders_totals"), job("j2", "customers_stream")];
});

describe("Jobs → Flink: refreshing keeps your place", () => {
  it("a refresh inside a job keeps its page open", async () => {
    const first = mount();
    await openJob(first.host, "customers_stream");
    expect(jobName(first.host)).toBe("customers_stream");
    first.unmount();
    const again = mount();
    expect(inJob(again.host)).toBe(true);
    expect(jobName(again.host)).toBe("customers_stream");
  });

  it("going back to the list is remembered: a refresh then stays on the list", async () => {
    const first = mount();
    await openJob(first.host, "customers_stream");
    await act(async () => { first.host.querySelector<HTMLButtonElement>('button[title="Back to all jobs"]')!.click(); });
    first.unmount();
    expect(inJob(mount().host)).toBe(false);
  });

  it("while the list is loading the choice is kept, then the job opens", () => {
    window.sessionStorage.setItem("inaiera.ui.flink.job", '"j2"');
    api.jobs = undefined;
    const screen = mount();
    expect(inJob(screen.host)).toBe(false);
    expect(window.sessionStorage.getItem("inaiera.ui.flink.job")).toBe('"j2"');
    api.jobs = [job("j1", "live_orders_totals"), job("j2", "customers_stream")];
    screen.render();
    expect(jobName(screen.host)).toBe("customers_stream");
  });

  it("a job that no longer exists is forgotten and the list shows", () => {
    window.sessionStorage.setItem("inaiera.ui.flink.job", '"deleted-elsewhere"');
    expect(inJob(mount().host)).toBe(false);
    expect(window.sessionStorage.getItem("inaiera.ui.flink.job")).toBeNull();
  });

  it("junk in storage is ignored", () => {
    window.sessionStorage.setItem("inaiera.ui.flink.job", "42");
    expect(inJob(mount().host)).toBe(false);
  });
});
