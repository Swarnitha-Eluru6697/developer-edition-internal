/** The Results pane: status, row/output count, copy, download, and the result — a grid for SQL rows,
 *  or the printed/returned output of a .py run (text, tables, images, tracebacks). */
import { useDispatch, useSelector } from "react-redux";
import { closeResults, toggleHistory } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import { useVisibleExecution } from "../hooks/useVisibleExecution";
import { resultSets, type ResultSet } from "../results";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";
import { PythonOutputs } from "../../notebook/components/NotebookOutputs";
import { downloadPlan, planExtension } from "../outputs";

/** Rows rendered in the grid; the full set is still downloadable. */
const PREVIEW_ROWS = 100;

export function ResultsPanel({ onDownload, onCopy }: { onDownload: () => void; onCopy: () => void }) {
  const dispatch = useDispatch<AppDispatch>();
  const exec = useVisibleExecution();      // only this tab's results, never another file's
  const historyOpen = useSelector((s: RootState) => s.ui.historyOpen);
  // One table per statement of a script, stacked top to bottom; a single statement is one table.
  const sets = resultSets(exec);
  const hasData = sets.some((r) => r.columns.length > 0 || r.rows.length > 0);
  const totalRows = sets.reduce((n, r) => n + r.rows.length, 0);
  const anyTruncated = sets.some((r) => r.truncated);
  const outputs = exec.outputs;
  const hasOutputs = outputs.length > 0;
  // The download format follows the data: .xls for tables, .png/.jpg for an image, .txt for text.
  const plan = downloadPlan(sets, outputs);
  const downloadLabel = plan
    ? `Download ${plan.kind === "sheet" ? "results as Excel" : plan.kind === "image" ? "the image" : "the output"} (.${planExtension(plan)})`
    : "Nothing to download yet";
  const summary = hasOutputs && !hasData
    ? `${outputs.length} output${outputs.length === 1 ? "" : "s"}`
    : `${sets.length > 1 ? `${sets.length} results · ` : ""}${totalRows.toLocaleString()} rows${anyTruncated ? "+" : ""}`;

  return (
    <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "5px 12px", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9", flexShrink: 0 }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>Results</span>
        <span style={{ fontSize: 11.5, color: exec.state === "failed" ? "#b23b3b" : "#2f6b14", fontWeight: 500 }}>{exec.state === "succeeded" ? "success" : exec.state}</span>
        {exec.fromHistory && <span style={{ fontSize: 10, color: "#6366f1", background: "#eef2ff", border: "0.5px solid #e0e7ff", borderRadius: 4, padding: "1px 5px" }}>from history</span>}
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 11.5, color: "#73827a" }}>{summary}</span>
          {exec.durationMs != null && <span style={{ fontSize: 11.5, color: "#73827a" }}>· {exec.durationMs}ms</span>}
          <IconAction size="sm" icon={ACTION_ICON.copy} label={plan ? "Copy results" : "Nothing to copy yet"} disabled={!plan} onClick={onCopy} />
          <IconAction size="sm" icon={ACTION_ICON.download} label={downloadLabel} disabled={!plan} onClick={onDownload} />
          <IconAction size="sm" icon="ti-history" label="Query history" on={historyOpen} onClick={() => dispatch(toggleHistory())} />
          <IconAction size="sm" icon={ACTION_ICON.close} label="Close results" onClick={() => dispatch(closeResults())} />
        </div>
      </div>
      {exec.fromHistory && exec.code && (
        <div style={{ padding: "5px 12px", borderBottom: "0.5px solid #eee", background: "#F8FAF9", fontFamily: "monospace", fontSize: 11, color: "#4b5a53", whiteSpace: "pre-wrap", maxHeight: 46, overflow: "auto", flexShrink: 0 }}>{exec.code}</div>
      )}
      <div style={{ flex: 1, overflow: "auto" }}>
        {exec.state === "failed" && exec.error && !hasOutputs && (
          <div style={{ margin: 10, padding: "10px 12px", background: "#fbe9e9", border: "0.5px solid #fecaca", borderRadius: 6, color: "#b23b3b", fontSize: 12, fontFamily: "monospace", whiteSpace: "pre-wrap" }}>
            <i className="ti ti-alert-circle" style={{ marginRight: 6 }} />{exec.error}
          </div>
        )}
        {hasOutputs && (
          <div style={{ padding: "4px 0" }}>
            <PythonOutputs outputs={outputs} error={exec.error} />
          </div>
        )}
        {!hasOutputs && exec.state !== "failed" && !hasData && sets.length <= 1 && exec.state !== "idle" && (
          <div style={{ padding: 14, fontSize: 12, color: "#73827a" }}>
            {exec.state === "succeeded" ? "Ran successfully — no rows or output returned." : "Running…"}
          </div>
        )}
        {(hasData || !hasOutputs) && sets.map((r) => (
          <ResultTable key={r.statement} set={r} stacked={sets.length > 1} />
        ))}
      </div>
    </div>
  );
}

/** One result set as a table. In a multi-statement run (`stacked`) a statement that returned no rows
 *  shows its status line instead, e.g. "INSERT 0 3", and the tables are separated by a small gap. */
function ResultTable({ set, stacked }: { set: ResultSet; stacked: boolean }) {
  if (stacked && set.columns.length === 0) {
    return (
      <div style={{ padding: "6px 12px", fontSize: 11.5, color: "#5b6d62", fontFamily: "monospace", borderBottom: "0.5px solid #E3E8E2" }} title={set.sql ?? undefined}>
        {set.status ?? "Statement ran, no rows returned."}
      </div>
    );
  }
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "monospace", fontSize: 11.5, marginBottom: stacked ? 14 : 0 }}>
      <thead>
        <tr>
          {set.columns.map((c) => (
            <th key={c.name} style={{ textAlign: "left", padding: "5px 10px", background: "#F8FAF9", borderRight: "0.5px solid #E3E8E2", borderBottom: "0.5px solid #E3E8E2", fontWeight: 600, fontSize: 11, whiteSpace: "nowrap" }}>
              {c.name}
              <div style={{ color: "#73827a", fontWeight: 400, fontSize: 9.5 }}>{c.type}</div>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {set.rows.slice(0, PREVIEW_ROWS).map((row, i) => (
          <tr key={i}>
            {row.map((v, j) => (
              <td key={j} style={{ padding: "4px 10px", borderRight: "0.5px solid #E3E8E2", borderBottom: "0.5px solid #E3E8E2", color: "#5b6d62" }}>
                {v === null ? <span style={{ color: "#cfd8d1" }}>NULL</span> : String(v)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
