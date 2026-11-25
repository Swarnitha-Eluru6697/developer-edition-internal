/**
 * The engine a new dbt, SQL or Python job is registered on: Trino (the default) or ClickHouse. pg_lake is not offered any more.
 */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { pickSearchSelect, searchSelectOptions } from "../helpers/searchSelect";
import type { JobKind } from "../../src/features/dbt/jobKinds";

const api = vi.hoisted(() => ({ registered: [] as Record<string, unknown>[] }));
vi.mock("../../src/features/dbt/api", () => ({
  useRegisterDbtProjectMutation: () => [(body: Record<string, unknown>) => {
    api.registered.push(body);
    return { unwrap: async () => ({ name: "job" }) };
  }, { isLoading: false }],
}));
vi.mock("react-redux", () => ({ useDispatch: () => vi.fn() }));

import { DbtRegisterDrawer } from "../../src/features/dbt/components/DbtRegisterDrawer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => { api.registered = []; document.body.innerHTML = ""; });

function open(kind: JobKind) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => createRoot(host).render(createElement(DbtRegisterDrawer, { projects: [], onClose: () => {}, onRegistered: () => {}, kind })));
  return host;
}
const engineSelect = () => document.body.querySelector<HTMLElement>("[aria-label='Engine']")!;

async function fillAndRegister(host: HTMLElement, path: string) {
  const type = (placeholder: string, value: string) => {
    const input = host.ownerDocument.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  };
  await act(async () => { type("https://github.com/acme/dbt-project.git", "https://github.com/acme/jobs.git"); });
  const field = host.ownerDocument.querySelector<HTMLInputElement>("input[type='text'][placeholder='customers'], input[placeholder='customers'], input[placeholder*='.sql'], input[placeholder*='.py']")!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, path);
  await act(async () => { field.dispatchEvent(new Event("input", { bubbles: true })); });
  await act(async () => { [...host.ownerDocument.querySelectorAll("button")].find((b) => /^Register/.test(b.textContent ?? ""))!.click(); });
}

describe("register a job: engine", () => {
  it.each(["dbt", "sql", "python"] as const)("a %s job offers Trino then ClickHouse, starts on Trino, and offers no pg_lake", async (kind) => {
    open(kind);
    expect(engineSelect().textContent).toBe("Trino");
    const options = await searchSelectOptions(engineSelect());
    expect(options).toEqual(["Trino", "ClickHouse"]);
    expect(document.body.querySelectorAll("[role='option'][aria-disabled='true']")).toHaveLength(0);   // both can be chosen
  });

  it("registers on Trino unless another engine is picked", async () => {
    const host = open("dbt");
    await fillAndRegister(host, "customers");
    expect(api.registered).toHaveLength(1);
    expect(api.registered[0]).toMatchObject({ engine: "trino", kind: "dbt" });
  });

  it.each([["dbt", "customers"], ["sql", "jobs/daily.sql"], ["python", "jobs/etl.py"]] as const)("a %s job registers on ClickHouse when it is picked", async (kind, path) => {
    const host = open(kind);
    await pickSearchSelect(engineSelect(), "ClickHouse");
    expect(engineSelect().textContent).toBe("ClickHouse");
    await fillAndRegister(host, path);
    expect(api.registered).toHaveLength(1);
    expect(api.registered[0]).toMatchObject({ engine: "clickhouse", kind, project_path: path });
  });
});
