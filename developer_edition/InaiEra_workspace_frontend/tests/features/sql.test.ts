import { describe, expect, it } from "vitest";
import {
  FILE_CELL_ID,
  beginExecution,
  executionReducer,
  historySeed,
  isTerminal,
  resultSets,
  resultSetsFromWire,
  resultSheetName,
  resultsFileName,
  resultsToSheetRows,
  showHistoricalExecution,
  visibleExecution,
  reassignExecutionOwner,
  setExecutionResult,
  sqlFeatureApi,
} from "../../src/features/sql";
import type { ExecutionHistoryItem } from "../../src/types";
import { callEndpoint } from "../helpers/apiHarness";

const INIT = executionReducer(undefined, { type: "@@INIT" });

describe("sql: execution slice", () => {
  it("streams chunk rows for the active execution only", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, { type: "ws/execution.chunk", payload: { execution_id: "e1", payload: { columns: [{ name: "a", type: "int" }], rows: [[1], [2]] } } });
    s = executionReducer(s, { type: "ws/execution.chunk", payload: { execution_id: "other", payload: { rows: [[9]] } } });
    expect(s.columns).toEqual([{ name: "a", type: "int" }]);
    expect(s.rows).toEqual([[1], [2]]);
  });

  it("marks a result truncated when the server had more rows than streamed", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, { type: "ws/execution.chunk", payload: { execution_id: "e1", payload: { rows: [[1]] } } });
    s = executionReducer(s, { type: "ws/execution.completed", payload: { execution_id: "e1", total_rows: 10, duration_ms: 5 } });
    expect(s).toMatchObject({ state: "succeeded", rowsTruncated: true, durationMs: 5 });
  });

  it("REST fallback maps completed → succeeded and ignores stale executions", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, setExecutionResult({ execution_id: "e2", status: "completed", rows: [[1]] }));
    expect(s.state).toBe("queued");
    s = executionReducer(s, setExecutionResult({ execution_id: "e1", status: "failed", error_message: "boom" }));
    expect(s).toMatchObject({ state: "failed", error: "boom" });
  });
});

describe("sql: results helpers", () => {
  it("builds the export sheet with nulls as empty cells", () => {
    expect(resultsToSheetRows([{ name: "a" }, { name: "b" }], [[1, null]])).toEqual([["a", "b"], [1, ""]]);
  });

  it("derives a safe export file name", () => {
    expect(resultsFileName("reports/q 1.sql")).toBe("reports_q_1_results.xls");
    expect(resultsFileName(null)).toBe("query_results.xls");
  });

  it("knows terminal statuses and seeds history rows", () => {
    expect(isTerminal({ status: "completed" })).toBe(true);
    expect(isTerminal({ status: "running" })).toBe(false);
    const item = { id: "e1", status: "completed", code_text: "select 1", row_count: 3, duration_ms: 7, error_message: null } as ExecutionHistoryItem;
    expect(historySeed(item)).toMatchObject({ execution_id: "e1", rows: [], row_count: 3 });
  });
});

describe("sql: endpoints", () => {
  it("submitSqlExecution POSTs the SqlExecutionRequest body", async () => {
    const body = { session_id: "s1", file_id: "f1", code_text: "select 1", connection_id: null };
    const { request } = await callEndpoint(sqlFeatureApi.endpoints.submitSqlExecution, body, { execution_id: "e1", status: "queued", websocket_topic: "t" });
    expect(request).toEqual({ method: "POST", url: "/api/v1/executions/sql", body });
  });

  it("listExecutions sends filters and the default limit", async () => {
    const { request } = await callEndpoint(sqlFeatureApi.endpoints.listExecutions, { workspace_id: "w1", language: "sql" }, []);
    expect(request.url).toBe("/api/v1/executions?workspace_id=w1&language=sql&limit=50");
  });

  it("whole-file runs use a fixed cell id (the notebook-cell contract requires one)", () => {
    expect(FILE_CELL_ID).toBeTruthy();
  });
});


describe("sql: a script of several statements shows every result, stacked", () => {
  const chunk = (execution_id: string, output_type: string, payload: Record<string, unknown>) =>
    ({ type: "ws/execution.chunk", payload: { execution_id, output_type, payload } });

  it("keeps each statement's rows in its own result set while streaming", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, chunk("e1", "rows", { statement: 0, sql: "select * from a", columns: [{ name: "id", type: "int4" }], rows: [[1], [2]] }));
    s = executionReducer(s, chunk("e1", "statement", { statement: 0, total_rows: 2 }));
    s = executionReducer(s, chunk("e1", "statement", { statement: 1, sql: "insert into t values (1)", total_rows: 0, status: "INSERT 0 1" }));
    s = executionReducer(s, chunk("e1", "rows", { statement: 2, sql: "select * from b", columns: [{ name: "name", type: "text" }], rows: [["x"]] }));
    expect(s.results.map((r) => r.statement)).toEqual([0, 1, 2]);
    expect(s.results[0]).toMatchObject({ sql: "select * from a", rows: [[1], [2]] });
    expect(s.results[1]).toMatchObject({ status: "INSERT 0 1", columns: [], rows: [] });
    expect(s.results[2]).toMatchObject({ columns: [{ name: "name", type: "text" }], rows: [["x"]] });
    expect(resultSets(s)).toHaveLength(3);
  });

  it("a single statement is untouched: one table from columns/rows, exactly as before", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, chunk("e1", "rows", { columns: [{ name: "a", type: "int" }], rows: [[1]] }));
    expect(s.results).toEqual([]);
    expect(resultSets(s)).toEqual([expect.objectContaining({ columns: [{ name: "a", type: "int" }], rows: [[1]] })]);
    // a lone statement that the server tags with index 0 is still one table, not "a script"
    s = executionReducer(INIT, beginExecution("e2"));
    s = executionReducer(s, chunk("e2", "rows", { statement: 0, columns: [{ name: "a", type: "int" }], rows: [[1]] }));
    expect(resultSets(s)).toHaveLength(1);
    expect(resultSets(s)[0].rows).toEqual([[1]]);
  });

  it("the REST result replaces the live view with the server's per-statement results", () => {
    let s = executionReducer(INIT, beginExecution("e1"));
    s = executionReducer(s, setExecutionResult({
      execution_id: "e1", status: "completed",
      columns: [{ name: "name", type: "text" }], rows: [["x"]],
      results: [
        { statement: 0, sql: "select * from a", columns: [{ name: "id", type: "int4" }], rows: [[1], [2]], row_count: 2 },
        { statement: 1, sql: "select * from b", columns: [{ name: "name", type: "text" }], rows: [["x"]], row_count: 1 },
      ],
    }));
    expect(resultSets(s).map((r) => r.rows)).toEqual([[[1], [2]], [["x"]]]);
    // no `results` (a single statement) clears any stale ones
    s = executionReducer(s, setExecutionResult({ execution_id: "e1", status: "completed", columns: [{ name: "a", type: "int" }], rows: [[9]] }));
    expect(s.results).toEqual([]);
    expect(resultSets(s)).toHaveLength(1);
  });

  it("a history entry restores all of its results; a new run starts clean", () => {
    let s = executionReducer(INIT, showHistoricalExecution({
      execution_id: "h1", status: "completed", columns: [], rows: [],
      results: resultSetsFromWire([
        { statement: 0, columns: [{ name: "a", type: "int" }], rows: [[1]] },
        { statement: 1, columns: [{ name: "b", type: "int" }], rows: [[2]] },
      ]),
    }));
    expect(resultSets(s)).toHaveLength(2);
    s = executionReducer(s, beginExecution("e9"));
    expect(s.results).toEqual([]);
  });

  it("flags a truncated statement and names Excel sheets per result", () => {
    expect(resultSetsFromWire([{ statement: 0, columns: [], rows: [[1]], row_count: 5 }])[0].truncated).toBe(true);
    expect(resultSetsFromWire([{ statement: 0, columns: [], rows: [[1]], row_count: 1 }])[0].truncated).toBe(false);
    expect(resultSheetName(0, 1)).toBe("Results");
    expect([0, 1].map((i) => resultSheetName(i, 2))).toEqual(["Result 1", "Result 2"]);
  });
});


// A result belongs to the editor tab that ran it: a different tab — e.g. a brand-new "Untitled-1.sql" — must not show another file's output.
describe("sql: results belong to their editor tab", () => {
  const withRows = (owner: string | null) => {
    let s = executionReducer(INIT, beginExecution("e1", owner));
    s = executionReducer(s, { type: "ws/execution.chunk", payload: { execution_id: "e1", payload: { columns: [{ name: "a", type: "int" }], rows: [[1], [2]] } } });
    return executionReducer(s, { type: "ws/execution.completed", payload: { execution_id: "e1", duration_ms: 5 } });
  };

  it("records the tab that started the run", () => {
    expect(withRows("file-A").ownerTab).toBe("file-A");
    expect(executionReducer(INIT, beginExecution("e1")).ownerTab).toBeNull();      // callers that pass no tab keep working
  });

  it("shows the results to the tab that ran them", () => {
    const s = withRows("file-A");
    expect(visibleExecution(s, "file-A")).toBe(s);
    expect(visibleExecution(s, "file-A").rows).toEqual([[1], [2]]);
  });

  it("shows another tab an empty pane, not the previous query's output", () => {
    const s = withRows("file-A");
    for (const other of ["file-B", "local-untitled-1", null]) {
      const v = visibleExecution(s, other);
      expect(v.id).toBeNull();
      expect(v.rows).toEqual([]);
      expect(v.state).toBe("idle");
    }
  });

  it("hands back the same empty object every time, so selecting it never re-renders by itself", () => {
    const s = withRows("file-A");
    expect(visibleExecution(s, "file-B")).toBe(visibleExecution(s, "file-C"));
  });

  it("shows nothing before anything has run", () => {
    expect(visibleExecution(INIT, "file-A").id).toBeNull();
  });

  it("a new run in a different tab takes the pane over, and the first tab no longer sees its old rows", () => {
    let s = withRows("file-A");
    s = executionReducer(s, beginExecution("e2", "file-B"));
    expect(visibleExecution(s, "file-B").id).toBe("e2");
    expect(visibleExecution(s, "file-A").id).toBeNull();
  });

  it("a past query opened from history belongs to the tab it was opened in", () => {
    const s = executionReducer(INIT, showHistoricalExecution({
      execution_id: "h1", status: "completed", columns: [{ name: "a", type: "int" }], rows: [[1]], ownerTab: "file-A",
    }));
    expect(visibleExecution(s, "file-A").fromHistory).toBe(true);
    expect(visibleExecution(s, "file-B").id).toBeNull();
  });

  it("streamed rows for the owning run still accumulate while another tab is active", () => {
    let s = executionReducer(INIT, beginExecution("e1", "file-A"));
    s = executionReducer(s, { type: "ws/execution.chunk", payload: { execution_id: "e1", payload: { rows: [[1]] } } });
    expect(visibleExecution(s, "file-B").rows).toEqual([]);
    expect(visibleExecution(s, "file-A").rows).toEqual([[1]]);               // back on A: the rows that streamed in meanwhile are there
  });

  it("saving an unsaved file under a new id keeps its results with it", () => {
    let s = withRows("local-untitled-1");
    s = executionReducer(s, reassignExecutionOwner({ from: "local-untitled-1", to: "server-file-9" }));
    expect(visibleExecution(s, "server-file-9").rows).toEqual([[1], [2]]);
    expect(visibleExecution(s, "local-untitled-1").id).toBeNull();
  });

  it("saving a different file does not steal someone else's results", () => {
    let s = withRows("file-A");
    s = executionReducer(s, reassignExecutionOwner({ from: "local-untitled-2", to: "server-file-9" }));
    expect(visibleExecution(s, "file-A").rows).toEqual([[1], [2]]);
  });
});
