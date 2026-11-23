/** Settings shows the engines and runners; the five platform services (UCS, database, Redis, object storage, secrets) are not listed. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EngineStatus } from "../../src/types";

const api = vi.hoisted(() => ({ engines: [] as unknown[] }));
vi.mock("../../src/features/settings/api", () => ({
  useGetEnginesQuery: () => ({ data: { engines: api.engines, checked_at: 1 }, isFetching: false, error: undefined, refetch: vi.fn() }),
}));

import { SettingsPanel } from "../../src/features/settings/components/SettingsPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const engine = (key: string, name: string, category: EngineStatus["category"]): EngineStatus => ({
  key, name, category, description: `${name} description`, status: "connected", detail: null, latency_ms: 5, endpoint: null, missing: [], impact: null,
});

function render() {
  const host = document.createElement("div");
  act(() => createRoot(host).render(createElement(SettingsPanel)));
  return host;
}

beforeEach(() => {
  api.engines = [
    engine("trino", "Trino", "engine"), engine("flink", "Flink runner", "runner"),
    engine("ucs", "Unified Catalog (UCS)", "platform"), engine("db", "Workspace database", "platform"), engine("redis", "Redis", "platform"),
    engine("s3", "Object storage (s3)", "platform"), engine("secrets", "Secrets backend (local file)", "platform"),
  ];
});

describe("settings panel", () => {
  it("lists engines and runners but none of the platform services", () => {
    const text = render().textContent ?? "";
    expect(text).toContain("Trino");
    expect(text).toContain("Flink runner");
    for (const gone of ["Unified Catalog (UCS)", "Workspace database", "Redis", "Object storage (s3)", "Secrets backend", "Platform"]) expect(text).not.toContain(gone);
  });

  it("counts only the services it shows", () => {
    expect(render().textContent).toContain("2 of 2 services connected");
  });
});
