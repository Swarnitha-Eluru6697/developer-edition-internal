/** Jobs → Flink: there is no sink-engine choice anywhere (every job runs on Flink); registering still sends the same fixed value as before. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ created: [] as Record<string, unknown>[], jobs: [] as unknown[] }));
vi.mock("../../src/features/flink/api", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListFlinkJobsQuery: () => ({ data: api.jobs, isLoading: false, isError: false, refetch: vi.fn() }),
    useCreateFlinkJobFromGitMutation: () => [(body: Record<string, unknown>) => {
      api.created.push(body);
      return { unwrap: async () => ({ name: "orders_stream" }) };
    }, { isLoading: false }],
    useSyncFlinkJobMutation: mutation, useDeleteFlinkJobMutation: mutation, useValidateFlinkJobMutation: mutation,
    useDeployFlinkJobMutation: mutation, useStopFlinkJobMutation: mutation,
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

const job = (id: string, name: string, engine: "trino" | "pg_lake") => ({
  id, org_id: "o", workspace_id: null, name, description: null, status: "paused", sink_engine: engine, parallelism: 2, sql: "select 1",
  validation_errors: [], savepoint_path: null, last_stopped_at: null, source: { git_url: "https://github.com/acme/flink-test.git", git_ref: "main", sql_path: "jobs/a.flink.sql", commit_sha: "abcdef1234567" },
});

beforeEach(() => {
  document.body.innerHTML = ""; api.created = [];
  api.jobs = [job("j1", "live_orders_totals", "pg_lake"), job("j2", "customers_stream", "trino")];
});

function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(FlinkPanel)));
  return host;
}
async function openForm() {
  const host = mount();
  await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Register job"))!.click(); });
  return document.querySelector('[role="dialog"]') as HTMLElement;
}

describe("Jobs → Flink: no sink engine in the UI", () => {
  it("the register form has no sink engine option, only the fields that matter", async () => {
    const drawer = await openForm();
    expect(drawer.querySelector("[aria-label='Sink engine']")).toBeNull();
    expect(drawer.textContent).not.toMatch(/sink engine|pg_lake|trino/i);
    expect(drawer.textContent).toContain("Parallelism");
    expect(drawer.textContent).toContain("SQL path in repo");
  });

  it("registers exactly as before: the fixed sink_engine value the form used to default to is still sent", async () => {
    const drawer = await openForm();
    const input = drawer.querySelector<HTMLInputElement>('input[placeholder="https://github.com/acme/flink-jobs.git"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "https://github.com/acme/r.git");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { [...drawer.querySelectorAll("button")].find((b) => b.textContent === "Register job")!.click(); });
    expect(api.created).toHaveLength(1);
    expect(api.created[0]).toMatchObject({ git_url: "https://github.com/acme/r.git", parallelism: 1, sink_engine: "pg_lake" });
  });

  it("the jobs list has no Sink column and shows no engine, whatever the job's stored value is", () => {
    const host = mount();
    const headers = [...host.querySelectorAll("div")].filter((d) => d.children.length === 0 && /^(Name|Status|Repository|Sink)$/.test(d.textContent ?? "")).map((d) => d.textContent);
    expect(headers).toEqual(["Name", "Status", "Repository"]);
    expect(host.textContent).toContain("live_orders_totals");
    expect(host.textContent).toContain("customers_stream");
    expect(host.textContent).not.toMatch(/pg_lake|trino/i);
  });

  it("a job's page does not show the engine either: not as a chip, not in the details", async () => {
    const host = mount();
    const row = [...host.querySelectorAll("div")].find((d) => d.textContent === "customers_stream")!;
    await act(async () => { row.click(); });
    expect(host.textContent).toContain("customers_stream");
    expect(host.textContent).toContain("Parallelism");
    expect(host.textContent).not.toMatch(/sink engine|pg_lake|trino/i);
  });
});
