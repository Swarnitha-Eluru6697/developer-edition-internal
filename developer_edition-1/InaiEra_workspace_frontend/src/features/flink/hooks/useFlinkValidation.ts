import { useCallback, useEffect, useState } from "react";
import { useDispatch } from "react-redux";
import { useValidateFlinkSqlMutation } from "../api";
import { stripSqlComments } from "../flinkSql";
import { apiErrorMessage } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import type { AppDispatch } from "../../../store";
import type { FlinkValidateResponse, UUID } from "../../../types";

/**
 * FlinkSQL is a streaming job definition, not a query — there is nothing to
 * "run" in the editor. ⌘↵ syntax-checks it against the FlinkSQL Runner
 * instead; deploying happens under FlinkSQL → Jobs, from Git.
 *
 * The result belongs to one file, so it is dropped whenever `fileKey` changes.
 */
export function useFlinkValidation(workspaceId: UUID | null, fileKey: UUID | null) {
  const dispatch = useDispatch<AppDispatch>();
  const [validateFlinkSql, { isLoading: validating }] = useValidateFlinkSqlMutation();
  const [result, setResult] = useState<FlinkValidateResponse | null>(null);

  useEffect(() => { setResult(null); }, [fileKey]);

  const validate = useCallback(async (code: string) => {
    const sql = code.trim();
    // A fresh file holds only the instruction header — don't send comments to
    // the runner and report "valid, 0 statements".
    if (!stripSqlComments(sql)) {
      dispatch(notify({ kind: "info", message: "Nothing to validate yet — write some FlinkSQL first (⌘K to generate it)." }));
      return;
    }
    try {
      setResult(await validateFlinkSql({ sql, workspace_id: workspaceId ?? undefined }).unwrap());
    } catch (e) {
      setResult({ valid: false, errors: [apiErrorMessage(e, "Validation request failed")] });
    }
  }, [dispatch, validateFlinkSql, workspaceId]);

  const dismiss = useCallback(() => setResult(null), []);

  return { result, validate, validating, dismiss };
}
