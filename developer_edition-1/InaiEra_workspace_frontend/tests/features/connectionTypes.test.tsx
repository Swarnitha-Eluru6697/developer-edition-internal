/** "Add database connection" offers Iceberg, PostgreSQL, FlinkSQL and Custom only; a connection that already is pg_lake still opens. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const ui = vi.hoisted(() => ({ editingConnectionId: null as string | null }));
const conns = vi.hoisted(() => ({ list: [] as unknown[] }));
vi.mock("react-redux", () => ({
  useDispatch: () => vi.fn(),
  useSelector: (fn: (s: unknown) => unknown) => fn({ ui }),
}));
vi.mock("../../src/api/client", () => {
  const mutation = () => [vi.fn(() => ({ unwrap: async () => ({}) })), { isLoading: false }];
  return {
    useListConnectionsQuery: () => ({ data: conns.list, isLoading: false }),
    useListUcsCatalogsQuery: () => ({ data: [] }),
    useCreateConnectionMutation: mutation,
    useUpdateConnectionMutation: mutation,
    useTestConnectionMutation: mutation,
  };
});

import { DatabaseConnectionModal } from "../../src/features/catalog/components/DatabaseConnectionModal";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** The type cards: the labelled rows inside the "Connection type" field. */
function typeLabels(): string[] {
  const field = [...document.querySelectorAll("label, div")].find((e) => e.textContent?.startsWith("Connection type"));
  const cards = field?.parentElement?.querySelectorAll<HTMLElement>("[style*='cursor']") ?? [];
  return [...cards].map((c) => c.querySelector("div > div:first-child")?.textContent ?? "").filter(Boolean);
}

beforeEach(() => { document.body.innerHTML = ""; ui.editingConnectionId = null; conns.list = []; });

describe("connection types", () => {
  it("adding a connection offers Iceberg, PostgreSQL, FlinkSQL and Custom, in that order", () => {
    act(() => createRoot(document.body.appendChild(document.createElement("div"))).render(createElement(DatabaseConnectionModal)));
    expect(typeLabels()).toEqual(["Iceberg", "PostgreSQL", "FlinkSQL", "Custom"]);
    expect(document.body.textContent).not.toContain("pg_lake");
    expect(document.body.textContent).not.toContain("Trino + Iceberg");
  });

  it("an existing pg_lake connection still opens for editing, showing its own type", () => {
    ui.editingConnectionId = "c1";
    conns.list = [{ id: "c1", name: "lake", connection_type: "pg_lake", host: "h", port: 5432, database_name: "d", catalog_name: null,
      use_tls: false, description: null, ucs_resource_fqn: null }];
    act(() => createRoot(document.body.appendChild(document.createElement("div"))).render(createElement(DatabaseConnectionModal)));
    expect(typeLabels()).toEqual(["pg_lake"]);
  });

  it("the example name and host follow the chosen type and no longer say Trino", async () => {
    act(() => createRoot(document.body.appendChild(document.createElement("div"))).render(createElement(DatabaseConnectionModal)));
    const placeholders = () => ({
      name: document.querySelector<HTMLInputElement>("input[placeholder^='e.g.']")?.placeholder,
      host: [...document.querySelectorAll<HTMLInputElement>("input")].find((i) => /yourcompany/.test(i.placeholder))?.placeholder,
    });
    expect(placeholders()).toEqual({ name: "e.g. production-iceberg", host: "iceberg.yourcompany.com" });
    const card = (label: string) => [...document.querySelectorAll<HTMLElement>("[style*='cursor']")].find((c) => c.textContent?.startsWith(label))!;
    await act(async () => { card("PostgreSQL").click(); });
    expect(placeholders()).toEqual({ name: "e.g. analytics-postgres", host: "postgres.yourcompany.com" });
    await act(async () => { card("FlinkSQL").click(); });
    expect(placeholders()).toEqual({ name: "e.g. streaming-flink", host: "flink-gateway.yourcompany.com" });
    await act(async () => { card("Custom").click(); });
    expect(placeholders()).toEqual({ name: "e.g. reporting-database", host: "db.yourcompany.com" });
    await act(async () => { card("Iceberg").click(); });
    expect(document.body.textContent).not.toMatch(/trino/i);
  });
});
