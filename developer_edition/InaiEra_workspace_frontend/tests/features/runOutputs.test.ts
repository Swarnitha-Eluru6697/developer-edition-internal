/** A .py run's kernel output in the Results pane: kept by the slice, copied and downloaded in the format its data calls for. */
import { describe, expect, it } from "vitest";
import { beginExecution, executionReducer, resultsFileName, setExecutionResult } from "../../src/features/sql";
import { copyText, downloadPlan, htmlTables, outputsToText, planExtension, toTsv } from "../../src/features/sql/outputs";
import type { ResultSet } from "../../src/features/sql/results";

const stream = (text: string, name = "stdout") => ({ output_type: "stream", payload: { name, text } });
const value = (bundle: Record<string, unknown>) => ({ output_type: "execute_result", payload: { mime_bundle: bundle } });
const DF_HTML = `<table class="dataframe"><thead><tr><th></th><th>name</th><th>qty</th></tr></thead>
  <tbody><tr><th>0</th><td>apple</td><td>3</td></tr><tr><th>1</th><td>pear</td><td>5</td></tr></tbody></table>`;
const sqlSet = (rows: unknown[][]): ResultSet => ({ statement: 0, sql: null, truncated: false, status: null, columns: [{ name: "a", type: "int" }, { name: "b", type: "text" }], rows });

describe("execution slice keeps kernel outputs", () => {
  it("stores outputs from the REST result and clears them on the next run", () => {
    let s = executionReducer(undefined, beginExecution("e1"));
    s = executionReducer(s, setExecutionResult({ execution_id: "e1", status: "completed", outputs: [stream("hi\n")] }));
    expect(s.state).toBe("succeeded");
    expect(s.outputs).toHaveLength(1);
    s = executionReducer(s, beginExecution("e2"));
    expect(s.outputs).toEqual([]);
  });
});

describe("run output → text / tables", () => {
  it("joins streams, values and tracebacks in order", () => {
    const text = outputsToText([
      stream("hello\n"),
      value({ "text/plain": "42" }),
      { output_type: "error", payload: { traceback: ["\u001b[31mValueError\u001b[0m: bad"] } },
    ]);
    expect(text).toBe("hello\n42\nValueError: bad");
  });

  it("reads a pandas DataFrame's HTML table into header + rows", () => {
    expect(htmlTables(DF_HTML)).toEqual([{ columns: ["", "name", "qty"], rows: [["0", "apple", "3"], ["1", "pear", "5"]] }]);
  });

  it("tab-separates cells and flattens tabs/newlines inside them", () => {
    expect(toTsv(["a", "b"], [[1, "x\ty"], [null, "z\nw"]])).toBe("a\tb\n1\tx y\n\tz w");
  });
});

describe("download format follows the data", () => {
  it("SQL rows → .xls", () => {
    const p = downloadPlan([sqlSet([[1, "x"]])], [])!;
    expect(p.kind).toBe("sheet");
    expect(planExtension(p)).toBe("xls");
    expect(resultsFileName("q.sql", planExtension(p))).toBe("q_results.xls");
  });

  it("a DataFrame shown as the value → .xls with its rows", () => {
    const p = downloadPlan([sqlSet([])].map((s) => ({ ...s, columns: [] })), [value({ "text/html": DF_HTML, "text/plain": "df" })])!;
    expect(p.kind).toBe("sheet");
    if (p.kind === "sheet") expect(p.sets[0].rows).toEqual([["0", "apple", "3"], ["1", "pear", "5"]]);
  });

  it("a lone chart → its image format", () => {
    const p = downloadPlan([], [{ output_type: "display_data", payload: { mime_bundle: { "image/png": "iVBORw0K\nGgo=", "text/plain": "<Figure>" } } }])!;
    expect(p).toMatchObject({ kind: "image", ext: "png", mime: "image/png", base64: "iVBORw0KGgo=" });
    expect(resultsFileName("plot.py", planExtension(p))).toBe("plot_results.png");
  });

  it("printed text → .txt", () => {
    const p = downloadPlan([], [stream("done\n")])!;
    expect(p).toEqual({ kind: "text", text: "done" });
    expect(planExtension(p)).toBe("txt");
  });

  it("nothing to download for an empty run", () => {
    expect(downloadPlan([], [])).toBeNull();
  });
});

describe("copy", () => {
  it("copies SQL rows as TSV with a header line", () => {
    expect(copyText([sqlSet([[1, "x"], [2, null]])], [])).toBe("a\tb\n1\tx\n2\t");
  });
  it("copies a .py run's output as text", () => {
    expect(copyText([], [stream("a\n"), stream("b\n")])).toBe("a\nb");
  });
});
