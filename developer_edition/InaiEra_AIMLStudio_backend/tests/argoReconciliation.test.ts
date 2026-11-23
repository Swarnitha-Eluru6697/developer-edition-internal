import { describe, it, expect } from "vitest";
import { extractStageOrderFromArgoNodeName } from "../src/execution/workflowEngine";

describe("extractStageOrderFromArgoNodeName", () => {
  it("extracts the real stage order from a plain Steps-template node name", () => {
    expect(extractStageOrderFromArgoNodeName("stage-1-validate-dataset")).toBe(1);
    expect(extractStageOrderFromArgoNodeName("stage-5-deploy")).toBe(5);
  });

  it("extracts the real stage order from a hierarchical node path Argo sometimes reports instead", () => {
    expect(extractStageOrderFromArgoNodeName("my-pipeline-id[0].stage-2-train-model")).toBe(2);
    expect(extractStageOrderFromArgoNodeName("my-pipeline-id.stage-3-evaluation")).toBe(3);
  });

  it("does not misread a double-digit stage order as a different single-digit one", () => {
    expect(extractStageOrderFromArgoNodeName("stage-10-deploy")).toBe(10);
    expect(extractStageOrderFromArgoNodeName("stage-12-register")).toBe(12);
  });

  it("is genuinely robust to whatever human title text follows — the whole real point of this fix", () => {
    // The old approach (n.displayName?.includes(stage.title)) would
    // fail here: the real DB title has spaces/casing/punctuation Argo's
    // own node naming never reproduces verbatim. This matches on the
    // stable numeric prefix instead, regardless of what text follows.
    expect(extractStageOrderFromArgoNodeName("stage-1-validate-dataset")).toBe(
      extractStageOrderFromArgoNodeName("stage-1-anything-else-entirely")
    );
  });

  it("returns null for a node name with no real stage-<N> segment at all", () => {
    expect(extractStageOrderFromArgoNodeName("main")).toBeNull();
    expect(extractStageOrderFromArgoNodeName("")).toBeNull();
    expect(extractStageOrderFromArgoNodeName("my-pipeline-id")).toBeNull();
  });
});
