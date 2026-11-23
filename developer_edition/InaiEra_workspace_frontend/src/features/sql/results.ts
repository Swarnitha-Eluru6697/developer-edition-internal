/** Pure helpers for the SQL results pane. */
import type { ExecutionHistoryItem, ExecutionResult, ExecutionResultSet } from "../../types";

/** Default height (px) of the editor's bottom Results/History panel. */
export const RESULTS_HEIGHT_DEFAULT = 190;

/** Header + body rows for the Excel export; null becomes an empty cell (not "null"). */
export function resultsToSheetRows(columns: { name: string }[], rows: unknown[][]): unknown[][] {
  return [columns.map((c) => c.name), ...rows.map((row) => row.map((v) => (v === null ? "" : v)))];
}

/** "<file stem>_results.<ext>" — the file's name without extension, made filesystem-safe. The extension
 *  follows the data: xls for tables, png/jpg for an image, txt for text output. */
export function resultsFileName(filePath: string | null | undefined, ext = "xls"): string {
  const stem = (filePath ?? "query").replace(/\.[^./]+$/, "").replace(/[^A-Za-z0-9_-]+/g, "_") || "query";
  return `${stem}_results.${ext}`;
}

/** True once a persisted execution has reached a terminal status. */
export const isTerminal = (r: Pick<ExecutionResult, "status">) => r.status === "completed" || r.status === "failed";

/** What the history panel already knows about a past run, before its rows load. */
export function historySeed(item: ExecutionHistoryItem) {
  return {
    execution_id: item.id, status: item.status, code_text: item.code_text,
    columns: [], rows: [], results: [], row_count: item.row_count, duration_ms: item.duration_ms,
    error_message: item.error_message,
  };
}

/** One result table of a run. A script of several statements has one per statement, shown stacked
 *  in the Results pane (like an online SQL editor); a single statement has exactly one. */
export interface ResultSet {
  statement: number;
  sql: string | null;
  columns: { name: string; type: string }[];
  rows: unknown[][];
  truncated: boolean;
  /** Command tag of a statement that returns no rows, e.g. "INSERT 0 3". */
  status: string | null;
}

export const emptyResultSet = (statement: number): ResultSet =>
  ({ statement, sql: null, columns: [], rows: [], truncated: false, status: null });

/** The server's per-statement results, in the shape the Results pane keeps. */
export function resultSetsFromWire(wire: ExecutionResultSet[] | undefined): ResultSet[] {
  return (wire ?? []).map((r) => ({
    statement: r.statement,
    sql: r.sql ?? null,
    columns: r.columns ?? [],
    rows: r.rows ?? [],
    truncated: !!r.truncated || (r.row_count ?? 0) > (r.rows?.length ?? 0),
    status: r.status ?? null,
  }));
}

/** The tables to render: every statement's result for a multi-statement script, otherwise the
 *  single result exactly as before (so a one-statement run looks and behaves as it always did). */
export function resultSets(exec: {
  results: ResultSet[]; columns: ResultSet["columns"]; rows: unknown[][]; rowsTruncated: boolean;
}): ResultSet[] {
  if (exec.results.length > 1) return exec.results;
  return [{ ...emptyResultSet(0), columns: exec.columns, rows: exec.rows, truncated: exec.rowsTruncated }];
}

/** Excel sheet name for result `index` (0-based) of `total`: "Results" for a lone result, otherwise
 *  "Result 1", "Result 2", … (Excel sheet names are capped at 31 characters). */
export const resultSheetName = (index: number, total: number): string =>
  total <= 1 ? "Results" : `Result ${index + 1}`;
