import { describe, expect, it } from "vitest";
import { codegenFeatureApi, codegenReducer, setCodegenTarget } from "../../src/features/codegen";
import { callEndpoint } from "../helpers/apiHarness";

describe("codegen: slice", () => {
  it("switches the active target without touching the session map", () => {
    const init = codegenReducer(undefined, { type: "@@INIT" });
    const next = codegenReducer(init, setCodegenTarget("dbt"));
    expect(next.activeTarget).toBe("dbt");
    expect(next.panelSessionByWorkspace).toEqual(init.panelSessionByWorkspace);
  });
});

describe("codegen: endpoints", () => {
  it("submitCodegenPrompt POSTs the prompt to the session's messages", async () => {
    const body = { prompt: "count orders", ws_session_id: "s1" };
    const { request } = await callEndpoint(codegenFeatureApi.endpoints.submitCodegenPrompt, { sessionId: "cs1", body }, {});
    expect(request).toEqual({ method: "POST", url: "/api/v1/codegen/sessions/cs1/messages", body });
  });

  it("applyCodegenArtifact POSTs to /apply", async () => {
    const { request } = await callEndpoint(codegenFeatureApi.endpoints.applyCodegenArtifact, { sessionId: "cs1", body: { message_id: "m1" } }, {});
    expect(request).toMatchObject({ method: "POST", url: "/api/v1/codegen/sessions/cs1/apply" });
  });
});
