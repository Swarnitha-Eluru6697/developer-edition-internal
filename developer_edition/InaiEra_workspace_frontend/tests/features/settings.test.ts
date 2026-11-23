import { describe, expect, it } from "vitest";
import { settingsFeatureApi, summarizeEngines } from "../../src/features/settings";
import type { EngineStatus } from "../../src/types";
import { callEndpoint } from "../helpers/apiHarness";

const engine = (over: Partial<EngineStatus>): EngineStatus => ({
  key: "k", name: "n", category: "engine", description: "", status: "connected",
  detail: null, latency_ms: 1, endpoint: null, missing: [], impact: null, ...over,
});

describe("settings: engine summary", () => {
  it("counts connected services and those needing attention", () => {
    const s = summarizeEngines([
      engine({}),
      engine({ status: "connected", missing: ["X_TOKEN"] }),
      engine({ status: "unreachable" }),
    ]);
    expect(s).toEqual({ connected: 2, needsSetup: 2 });
  });
});

describe("settings: endpoints", () => {
  it("getEngines reads /settings/engines", async () => {
    const payload = { engines: [engine({})], checked_at: 1 };
    const { request, data } = await callEndpoint(settingsFeatureApi.endpoints.getEngines, undefined, payload);
    expect(request).toMatchObject({ method: "GET", url: "/api/v1/settings/engines" });
    expect(data).toEqual(payload);
  });
});
