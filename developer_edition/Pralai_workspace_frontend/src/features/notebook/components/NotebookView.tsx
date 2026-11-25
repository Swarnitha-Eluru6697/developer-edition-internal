/**
 * NotebookView — a Jupyter-style cell notebook for .ipynb files.
 *
 * Cells are polyglot: each cell is either a **SQL** cell or a **Python** cell,
 * chosen per-cell like a Jupyter notebook with SQL magics.
 *   - SQL cells   run against the selected Database Explorer connection via
 *     the lakehouse query service and render a result table.
 *   - Python cells run in the session's JupyterHub kernel and render Jupyter
 *     outputs (stdout/stderr streams, execute_result, display_data images/HTML,
 *     and tracebacks) below the cell.
 *
 * Per-cell language is persisted in each cell's `metadata.language` so the
 * notebook round-trips as valid nbformat JSON.
 */
import { useState, useRef, useEffect } from "react";
import type { CSSProperties } from "react";
import { useSelector, useDispatch } from "react-redux";
import type { RootState, AppDispatch } from "../../../store";
import type { ExecutionAccepted, ExecutionResult } from "../../../types";
import { newCellId as rid, parseCells, toIpynb, type Cell } from "../notebookFormat";
import { PythonOutputs } from "./NotebookOutputs";
import { isLocalBufferId } from "../../workspace/localBuffers";
import { notify, clearCellInsert } from "../../../store/uiSlice";
import type { UUID } from "../../../types";
import { apiUrl } from "../../../api/apiBase";
import { ACTION_ICON, IconAction } from "../../../shared/ui/IconAction";

interface Props {
  /** The open file's id — or "local:N" for a notebook that has not been saved yet. */
  fileId: UUID;
  sessionId: UUID | null;
  initialContent: string;
  /** Called with the notebook as .ipynb text after each edit, so the editor can tell it has unsaved changes. */
  onChange: (content: string) => void;
  /** Save (⌘S / the Save button): the editor owns saving, so a new notebook asks for a name like any new file. */
  onSave: () => void;
}

export function NotebookView({ fileId, sessionId, initialContent, onChange, onSave }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const connectionId = useSelector((s: RootState) => s.ui.activeConnectionId);
  const [cells, setCells] = useState<Cell[]>(() => parseCells(initialContent));
  // The file's content may arrive AFTER first mount (async fetch). Reparse when
  // it does, unless the user has already edited the cells (never clobber edits).
  const editedRef = useRef(false);
  const seedRef = useRef(initialContent);
  useEffect(() => {
    if (!editedRef.current && initialContent && initialContent !== seedRef.current) {
      seedRef.current = initialContent;
      setCells(parseCells(initialContent));
    }
  }, [initialContent]);

  // Report edits (not run results: outputs aren't part of what is saved) to the editor.
  const lastReported = useRef<string | null>(null);
  useEffect(() => {
    if (!editedRef.current) return;
    const content = toIpynb(cells);
    if (content === lastReported.current) return;
    lastReported.current = content;
    onChange(content);
  }, [cells, onChange]);

  const patch = (id: string, upd: Partial<Cell>) =>
    setCells((cs) => cs.map((c) => (c.id === id ? { ...c, ...upd } : c)));
  const markEdited = () => { editedRef.current = true; };

  // Codegen "Insert into editor" → append the generated code as a new cell.
  const pendingInsert = useSelector((s: RootState) => s.ui.pendingCellInsert);
  useEffect(() => {
    if (!pendingInsert) return;
    markEdited();
    setCells((cs) => [...cs, { id: rid(), language: pendingInsert.language, source: pendingInsert.code }]);
    dispatch(clearCellInsert());
  }, [pendingInsert, dispatch]);

  async function runCell(cell: Cell) {
    if (!sessionId) { dispatch(notify({ kind: "info", message: "Session not ready — try again in a moment." })); return; }
    if (!cell.source.trim()) return;
    patch(cell.id, { running: true, error: null, status: "running", outputs: undefined, columns: undefined, rows: undefined });
    const t0 = Date.now();
    const isPython = cell.language === "python";
    try {
      const url = apiUrl(isPython ? "/api/v1/executions/notebook-cell" : "/api/v1/executions/sql");
      // A notebook that has not been saved has no file yet; the server accepts a run without one.
      const runFileId = isLocalBufferId(fileId) ? null : fileId;
      const body = isPython
        ? { session_id: sessionId, file_id: runFileId, cell_id: cell.id, code_text: cell.source }
        : { session_id: sessionId, file_id: runFileId, code_text: cell.source, connection_id: connectionId };
      const sub = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body),
      });
      if (!sub.ok) {
        const detail = await sub.json().catch(() => ({}));
        throw new Error(detail?.error?.message ?? detail?.detail?.message ?? `Request failed (${sub.status})`);
      }
      const { execution_id } = (await sub.json()) as ExecutionAccepted;
      const start = Date.now();
      const budget = isPython ? 120000 : 20000; // kernels may take longer to warm up
      while (Date.now() - start < budget) {
        await new Promise((r) => setTimeout(r, 450));
        const r = await fetch(apiUrl(`/api/v1/executions/${execution_id}/result`), { credentials: "same-origin" });
        if (!r.ok) continue;
        const d = (await r.json()) as ExecutionResult;
        if (d.status === "completed" || d.status === "failed") {
          if (isPython) {
            patch(cell.id, { running: false, status: d.status, outputs: d.outputs || [], error: d.error_message, ms: Date.now() - t0 });
          } else {
            patch(cell.id, { running: false, status: d.status, columns: d.columns || [], rows: d.rows || [], error: d.error_message, ms: Date.now() - t0 });
          }
          return;
        }
      }
      patch(cell.id, { running: false, status: "timeout" });
    } catch (e) {
      patch(cell.id, { running: false, error: e instanceof Error ? e.message : String(e), status: "failed" });
    }
  }

  const addCell = (afterIdx?: number) => {
    markEdited();
    setCells((cs) => {
      const n: Cell = { id: rid(), language: "python", source: "" };
      if (afterIdx == null) return [...cs, n];
      const copy = [...cs];
      copy.splice(afterIdx + 1, 0, n);
      return copy;
    });
  };
  const delCell = (id: string) => { markEdited(); setCells((cs) => (cs.length > 1 ? cs.filter((c) => c.id !== id) : cs)); };
  const runAll = async () => { for (const c of cells) await runCell(c); };
  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "#f4f4f2", overflow: "hidden" }}>
      {/* Toolbar */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", borderBottom: "0.5px solid #E3E8E2", background: "#fff", flexShrink: 0 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: "#2E3D36" }}><i className="ti ti-notebook" style={{ marginRight: 5 }} />Notebook</span>
        <button onClick={runAll} style={btn("#0A7D5A", "#fff")}><i className="ti ti-player-play" style={{ fontSize: 12 }} /> Run all</button>
        <button onClick={() => addCell()} style={btn("#fff", "#2E3D36", true)}><i className="ti ti-plus" style={{ fontSize: 12 }} /> Add cell</button>
        <button onClick={onSave} title="Save (⌘S)" style={btn("#fff", "#2E3D36", true)}><i className="ti ti-device-floppy" style={{ fontSize: 12 }} /> Save</button>
        <span style={{ marginLeft: "auto", fontSize: 11, color: "#73827a" }}>Python runs in the notebook kernel</span>
      </div>

      {/* Cells */}
      <div style={{ flex: 1, overflow: "auto", padding: 12 }}>
        {cells.map((cell, i) => {
          const accent = "#f59e0b";
          return (
          <div key={cell.id} style={{ marginBottom: 14, border: "0.5px solid #E3E8E2", borderRadius: 8, background: "#fff", overflow: "hidden" }}>
            <div style={{ display: "flex", alignItems: "stretch" }}>
              <div style={{ width: 46, flexShrink: 0, background: "#fafafa", borderRight: "0.5px solid #eee", display: "flex", flexDirection: "column", alignItems: "center", paddingTop: 6, gap: 5 }}>
                <button onClick={() => runCell(cell)} title="Run cell (⌘/Ctrl+Enter)" style={{ background: "none", border: "none", cursor: "pointer", color: accent, fontSize: 16 }}>
                  <i className={`ti ${cell.running ? "ti-loader-2" : "ti-player-play"}`} />
                </button>
                <span style={{ fontSize: 9.5, color: "#73827a", fontFamily: "monospace" }}>[{i + 1}]</span>
              </div>
              <textarea
                value={cell.source}
                onChange={(e) => { markEdited(); patch(cell.id, { source: e.target.value }); }}
                onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); e.stopPropagation(); runCell(cell); } }}
                spellCheck={false}
                placeholder="# Python for this cell (⌘/Ctrl+Enter to run)"
                rows={Math.max(2, cell.source.split("\n").length)}
                style={{ flex: 1, border: "none", outline: "none", resize: "vertical", padding: "8px 10px", fontFamily: '"JetBrains Mono", Menlo, monospace', fontSize: 12.5, lineHeight: 1.5, color: "#14201B" }}
              />
              <IconAction size="sm" icon={ACTION_ICON.delete} label="Delete cell" danger
                style={{ margin: "6px 8px", alignSelf: "flex-start" }} onClick={() => delCell(cell.id)} />
            </div>
            {/* Output */}
            {cell.status && cell.status !== "running" && (
              <div style={{ borderTop: "0.5px solid #eee", background: "#F8FAF9" }}>
                <div style={{ display: "flex", gap: 8, padding: "3px 10px", fontSize: 11, color: cell.status === "failed" ? "#b23b3b" : "#2f6b14" }}>
                  {cell.status === "failed" ? "error" : cell.status === "timeout" ? "timed out" : "success"}
                  {cell.status === "completed" && cell.ms && <span style={{ color: "#73827a" }}>{cell.ms}ms</span>}
                </div>
                <PythonOutputs outputs={cell.outputs} error={cell.error} />
              </div>
            )}
          </div>
        );})}
        <button onClick={() => addCell()} style={{ ...btn("#fff", "#5b6d62", true), width: "100%", justifyContent: "center", marginTop: 2 }}>
          <i className="ti ti-plus" style={{ fontSize: 12 }} /> Add cell
        </button>
      </div>
    </div>
  );
}

function btn(bg: string, color: string, bordered = false): CSSProperties {
  return { height: 26, padding: "0 10px", fontSize: 11.5, background: bg, color, border: bordered ? "0.5px solid #cfd8d1" : "none", borderRadius: 6, cursor: "pointer", display: "flex", alignItems: "center", gap: 4 };
}
