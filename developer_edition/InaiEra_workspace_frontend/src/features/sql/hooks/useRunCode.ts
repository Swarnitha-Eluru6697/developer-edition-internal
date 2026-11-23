import { useCallback } from "react";
import { useDispatch } from "react-redux";
import { useSubmitSqlExecutionMutation } from "../api";
import { useSubmitNotebookExecutionMutation } from "../../notebook/api";
import { beginExecution } from "../executionSlice";
import { apiErrorMessage } from "../../../api/errors";
import { notify, setResultsOpen } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import type { FileLanguage, UUID } from "../../../types";

/** A whole non-notebook file run through the notebook-cell endpoint is one
 *  cell; the endpoint requires a cell id (NotebookCellExecutionRequest). */
export const FILE_CELL_ID = "file";

interface RunRequest {
  /** null for an unsaved new file (the server accepts a run with no file). */
  fileId: UUID | null;
  language: FileLanguage;
  code: string;
  connectionId: UUID | null;
  /** The editor tab the results belong to (the Results pane shows them only while that tab is active). */
  ownerTab: UUID | null;
}

/**
 * Submit the editor's code: SQL against the lakehouse (or the selected
 * connection), anything else to the session's notebook kernel. Results stream
 * over the WS; `pollResult` is the REST fallback that guarantees the Results
 * pane populates even if the stream races.
 */
export function useRunCode(sessionId: UUID | null, pollResult: (executionId: UUID) => Promise<void>) {
  const dispatch = useDispatch<AppDispatch>();
  const [submitSqlExecution] = useSubmitSqlExecutionMutation();
  const [submitNotebookExecution] = useSubmitNotebookExecutionMutation();

  return useCallback(async ({ fileId, language, code, connectionId, ownerTab }: RunRequest) => {
    if (!sessionId) {
      dispatch(notify({ kind: "info", message: "Execution session not ready yet — try again in a moment." }));
      return;
    }
    try {
      const result = language === "sql"
        ? await submitSqlExecution({ session_id: sessionId, file_id: fileId, code_text: code, connection_id: connectionId }).unwrap()
        : await submitNotebookExecution({ session_id: sessionId, file_id: fileId, cell_id: FILE_CELL_ID, code_text: code }).unwrap();
      dispatch(beginExecution(result.execution_id, ownerTab));
      dispatch(setResultsOpen(true)); // a new run always shows its results
      void pollResult(result.execution_id);
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Failed to start execution.") }));
    }
  }, [dispatch, pollResult, sessionId, submitNotebookExecution, submitSqlExecution]);
}
