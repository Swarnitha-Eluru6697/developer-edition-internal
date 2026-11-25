import { useCallback } from "react";
import { useDispatch, useSelector } from "react-redux";
import { sqlFeatureApi } from "../api";
import { setExecutionResult, showHistoricalExecution } from "../executionSlice";
import { useVisibleExecution } from "./useVisibleExecution";
import { historySeed, isTerminal, resultSets, resultSetsFromWire, resultSheetName, resultsFileName, resultsToSheetRows } from "../results";
import { copyText, downloadPlan } from "../outputs";
import { notify, setResultsOpen } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { ExecutionHistoryItem, UUID } from "../../../types";

// Long enough for a cold notebook kernel to start (a .py run); polling stops as soon as the run ends.
const POLL_BUDGET_MS = 120_000;
const POLL_INTERVAL_MS = 500;

/** Actions behind the Results pane: REST result polling, Excel download, and
 *  loading a past query from the history panel. */
export function useResultsActions() {
  const dispatch = useDispatch<AppDispatch>();
  const exec = useVisibleExecution();
  const activeTab = useSelector((s: RootState) => s.ui.activeTab);
  // One-shot fetches (no hook subscription): polling must not re-render the
  // host component every 500ms, and each poll must hit the server.
  const fetchResult = useCallback(
    (id: UUID) => dispatch(sqlFeatureApi.endpoints.getExecutionResult.initiate(id, { subscribe: false, forceRefetch: true })),
    [dispatch],
  );

  // Reliable REST fallback: poll the persisted result until terminal. The WS
  // stream may race, so this guarantees the Results pane populates every run.
  const pollResult = useCallback(async (executionId: UUID) => {
    const started = Date.now();
    while (Date.now() - started < POLL_BUDGET_MS) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      try {
        const data = await fetchResult(executionId).unwrap();
        if (isTerminal(data)) {
          dispatch(setExecutionResult(data));
          return;
        }
      } catch { /* keep polling */ }
    }
  }, [dispatch, fetchResult]);

  // Download what the run produced, in the format its data calls for (see downloadPlan):
  //  - tables (SQL rows, or a DataFrame a .py run returned) → a real Excel workbook, one sheet per
  //    table. bookType "biff8" writes an actual OLE/BIFF8 .xls binary, so the extension matches the
  //    contents — Excel opens it without the "format and extension don't match" warning.
  //  - a lone chart/image → .png / .jpg
  //  - anything else a .py run printed → .txt
  const downloadResults = useCallback(async (filePath: string | null | undefined) => {
    const plan = downloadPlan(resultSets(exec), exec.outputs);
    if (!plan) {
      dispatch(notify({ kind: "info", message: "Nothing to download — run the file first." }));
      return;
    }
    try {
      if (plan.kind === "image") {
        const bytes = Uint8Array.from(atob(plan.base64), (c) => c.charCodeAt(0));
        saveBlob(new Blob([bytes], { type: plan.mime }), resultsFileName(filePath, plan.ext));
        dispatch(notify({ kind: "success", message: `Downloaded the image (.${plan.ext}).` }));
        return;
      }
      if (plan.kind === "text") {
        saveBlob(new Blob([plan.text], { type: "text/plain;charset=utf-8" }), resultsFileName(filePath, "txt"));
        dispatch(notify({ kind: "success", message: "Downloaded the output (.txt)." }));
        return;
      }
      const sets = plan.sets;
      // Loaded on demand: SheetJS is ~400 kB and only matters when exporting.
      const XLSX = await import("xlsx");
      const book = XLSX.utils.book_new();
      sets.forEach((r, i) => {
        XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet(resultsToSheetRows(r.columns, r.rows)), resultSheetName(i, sets.length));
      });
      XLSX.writeFile(book, resultsFileName(filePath, "xls"), { bookType: "biff8" });
      const rowCount = sets.reduce((n, r) => n + r.rows.length, 0);
      const where = sets.length > 1 ? ` in ${sets.length} sheets` : "";
      dispatch(notify({
        kind: "success",
        message: sets.some((r) => r.truncated)
          ? `Downloaded ${rowCount.toLocaleString()} rows${where} — a result was truncated, so this is not the full query output.`
          : `Downloaded ${rowCount.toLocaleString()} rows${where}.`,
      }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: e instanceof Error ? e.message : "Could not build the download." }));
    }
  }, [dispatch, exec]);

  // Copy the result: tables as tab-separated text (pastes into Excel / Sheets as cells), output as text.
  const copyResults = useCallback(async () => {
    const text = copyText(resultSets(exec), exec.outputs);
    if (!text) {
      dispatch(notify({ kind: "info", message: "Nothing to copy — run the file first." }));
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      dispatch(notify({ kind: "success", message: "Results copied to the clipboard." }));
    } catch {
      dispatch(notify({ kind: "error", message: "Couldn't copy — the browser blocked clipboard access." }));
    }
  }, [dispatch, exec]);

  // Load a past query (from the history panel) into the Results pane: seed
  // immediately with what history already knows, then fill in the rows.
  const selectHistory = useCallback(async (item: ExecutionHistoryItem) => {
    dispatch(setResultsOpen(true));
    dispatch(showHistoricalExecution({ ...historySeed(item), ownerTab: activeTab }));   // it belongs to the tab it was opened in
    try {
      const data = await fetchResult(item.id).unwrap();
      dispatch(showHistoricalExecution({
        execution_id: item.id, status: data.status, code_text: item.code_text,
        columns: data.columns ?? [], rows: data.rows ?? [], row_count: data.row_count,
        duration_ms: item.duration_ms, error_message: data.error_message,
        results: resultSetsFromWire(data.results), outputs: data.outputs ?? [], ownerTab: activeTab,
      }));
    } catch { /* keep the seeded summary */ }
  }, [dispatch, fetchResult, activeTab]);

  return { pollResult, downloadResults, copyResults, selectHistory };
}

/** Save a Blob as a file via a temporary object URL. */
function saveBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
