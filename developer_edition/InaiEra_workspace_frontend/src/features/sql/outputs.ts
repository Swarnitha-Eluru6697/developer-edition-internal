/**
 * Pure helpers for what a non-SQL run (a .py file on the notebook kernel)
 * returns: Jupyter-style `outputs` — stdout/stderr streams, execute_result /
 * display_data mime bundles (text, HTML tables, images) and errors. They decide
 * how the Results pane copies and downloads that output: tables become a
 * spreadsheet, a lone image keeps its own format, everything else is text.
 */
import { mimeText, stripAnsi, type NbOutput } from "../notebook/notebookFormat";
import type { ResultSet } from "./results";

interface Payload {
  name?: string;
  text?: string | string[];
  mime_bundle?: Record<string, unknown>;
  traceback?: string[];
  ename?: string;
  evalue?: string;
  error_message?: string;
  error_code?: string;
}

const payloadOf = (o: NbOutput) => (o.payload || {}) as Payload;
const bundleOf = (o: NbOutput) => payloadOf(o).mime_bundle ?? {};
const isRich = (o: NbOutput) => o.output_type === "display_data" || o.output_type === "execute_result";

/** Text of one output, or null when it has none (e.g. an image). */
function outputText(o: NbOutput): string | null {
  const p = payloadOf(o);
  if (o.output_type === "stream") return Array.isArray(p.text) ? p.text.join("") : (p.text ?? "");
  if (o.output_type === "error") {
    if (Array.isArray(p.traceback) && p.traceback.length) return stripAnsi(p.traceback.join("\n"));
    return `${p.ename ? `${p.ename}: ` : ""}${p.evalue || p.error_message || p.error_code || "Error"}`;
  }
  if (isRich(o)) {
    const bundle = bundleOf(o);
    const plain = mimeText(bundle, "text/plain");
    if (plain) return plain;
    const tables = htmlTables(mimeText(bundle, "text/html"));
    if (tables.length) return tables.map((t) => toTsv(t.columns, t.rows)).join("\n\n");
  }
  return null;
}

/** Every output's text, in order — what a .txt download or a copy contains. */
export function outputsToText(outputs: NbOutput[]): string {
  return outputs.map(outputText).filter((t): t is string => t !== null && t !== "").map((t) => t.replace(/\n+$/, "")).join("\n");
}

/** The <table>s in kernel HTML (e.g. a pandas DataFrame), as header + rows. Parsed with DOMParser only —
 *  the HTML is never attached to the page. */
export function htmlTables(html: string | null | undefined): { columns: string[]; rows: string[][] }[] {
  if (!html || !html.includes("<table") || typeof DOMParser === "undefined") return [];
  const doc = new DOMParser().parseFromString(html, "text/html");
  return [...doc.querySelectorAll("table")].map((table) => {
    const cells = (tr: Element) => [...tr.querySelectorAll("th,td")].map((c) => (c.textContent ?? "").trim());
    const headRows = [...table.querySelectorAll("thead tr")];
    const bodyRows = [...table.querySelectorAll("tbody tr")];
    const all = bodyRows.length || headRows.length ? bodyRows : [...table.querySelectorAll("tr")];
    // pandas writes the index name on a second header row; the last header row carries the column names
    // only when it is the sole one, so join multi-row headers column by column.
    let columns: string[] = [];
    if (headRows.length) {
      const grid = headRows.map(cells);
      const width = Math.max(...grid.map((r) => r.length));
      columns = Array.from({ length: width }, (_, i) => grid.map((r) => r[i] ?? "").filter(Boolean).join(" ").trim());
    }
    const rows = (headRows.length ? all : all.slice(1)).map(cells);
    if (!headRows.length && all.length) columns = cells(all[0]);
    return { columns, rows };
  }).filter((t) => t.columns.length || t.rows.length);
}

/** Tab-separated text (header line first) — pastes straight into Excel or Sheets as cells. */
export function toTsv(columns: string[], rows: unknown[][]): string {
  const cell = (v: unknown) => (v === null || v === undefined ? "" : String(v)).replace(/[\t\r\n]+/g, " ");
  return [columns, ...rows].map((r) => r.map(cell).join("\t")).join("\n");
}

/** How the current result downloads, decided by what it holds. */
export type DownloadPlan =
  | { kind: "sheet"; sets: ResultSet[] }                        // SQL rows, or tables printed by Python
  | { kind: "image"; base64: string; mime: string; ext: "png" | "jpg" }
  | { kind: "text"; text: string };

export function downloadPlan(sets: ResultSet[], outputs: NbOutput[]): DownloadPlan | null {
  const withData = sets.filter((r) => r.columns.length > 0 || r.rows.length > 0);
  if (withData.length) return { kind: "sheet", sets: withData };
  if (!outputs.length) return null;

  // Output that is only table(s) — a DataFrame shown as the cell's value — is data: download a spreadsheet.
  const text = outputs.filter((o) => o.output_type === "stream" || o.output_type === "error");
  const rich = outputs.filter(isRich);
  const tables = rich.flatMap((o) => htmlTables(mimeText(bundleOf(o), "text/html")));
  if (!text.length && tables.length && tables.length === rich.length) {
    return {
      kind: "sheet",
      sets: tables.map((t, i) => ({
        statement: i, sql: null, truncated: false, status: null,
        columns: t.columns.map((name) => ({ name, type: "" })), rows: t.rows,
      })),
    };
  }
  // A lone chart/image keeps its own format.
  if (!text.length && rich.length === 1) {
    const b = bundleOf(rich[0]);
    for (const [mime, ext] of [["image/png", "png"], ["image/jpeg", "jpg"]] as const) {
      const data = b[mime];
      if (typeof data === "string" && data) return { kind: "image", base64: data.replace(/\s/g, ""), mime, ext };
    }
  }
  const all = outputsToText(outputs);
  return all ? { kind: "text", text: all } : null;
}

/** File extension a plan downloads as. */
export const planExtension = (p: DownloadPlan): string => (p.kind === "sheet" ? "xls" : p.kind === "image" ? p.ext : "txt");

/** What "Copy" puts on the clipboard: tables as TSV (one block per table), otherwise the output text. */
export function copyText(sets: ResultSet[], outputs: NbOutput[]): string {
  const withData = sets.filter((r) => r.columns.length > 0 || r.rows.length > 0);
  if (withData.length) return withData.map((r) => toTsv(r.columns.map((c) => c.name), r.rows)).join("\n\n");
  return outputsToText(outputs);
}
