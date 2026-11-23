import { describe, expect, it } from "vitest";
import { mimeText, notebookFeatureApi, parseCells, stripAnsi, toIpynb } from "../../src/features/notebook";
import { callEndpoint } from "../helpers/apiHarness";

describe("notebook: nbformat round-trip", () => {
  it("parses ipynb cells (array or string source) as python cells", () => {
    const cells = parseCells(JSON.stringify({ cells: [{ source: ["a = 1\n", "b = 2"] }, { source: "print(a)" }] }));
    expect(cells.map((c) => [c.language, c.source])).toEqual([["python", "a = 1\nb = 2"], ["python", "print(a)"]]);
  });

  it("treats non-JSON content as one cell, and JSON-looking garbage as empty", () => {
    expect(parseCells("x = 1")[0].source).toBe("x = 1");
    expect(parseCells("{broken")[0].source).toBe("");
  });

  it("serialises back to nbformat 4 with line-split sources", () => {
    const nb = JSON.parse(toIpynb([{ id: "c1", language: "python", source: "a\nb" }]));
    expect(nb.nbformat).toBe(4);
    expect(nb.cells[0]).toMatchObject({ cell_type: "code", source: ["a\n", "b"] });
  });
});

describe("notebook: output helpers", () => {
  it("strips ANSI colour codes from tracebacks", () => {
    expect(stripAnsi(`${String.fromCharCode(27)}[31mValueError${String.fromCharCode(27)}[0m`)).toBe("ValueError");
  });

  it("joins multi-line mime values", () => {
    expect(mimeText({ "text/plain": ["a", "b"] }, "text/plain")).toBe("ab");
    expect(mimeText(undefined, "text/plain")).toBe("");
  });
});

describe("notebook: endpoints", () => {
  it("submitNotebookExecution POSTs to /executions/notebook-cell", async () => {
    const body = { session_id: "s1", file_id: "f1", cell_id: "c1", code_text: "1+1" };
    const { request } = await callEndpoint(notebookFeatureApi.endpoints.submitNotebookExecution, body, { execution_id: "e", status: "queued", websocket_topic: "t" });
    expect(request).toEqual({ method: "POST", url: "/api/v1/executions/notebook-cell", body });
  });
});
