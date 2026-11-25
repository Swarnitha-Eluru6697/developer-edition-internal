/**
 * Notebook feature package (WS-FE-014): notebook cell execution. Session
 * creation, cancellation, and execution history are shared runtime concerns
 * owned by the sql feature package (see app/api/executions.py) — import
 * them from "../sql/api" or the "../../api/client" barrel.
 */
import { workspaceApi } from "../../api/base";
import type { ExecutionAccepted, NotebookCellExecutionRequest } from "../../types";

export const notebookFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    submitNotebookExecution: builder.mutation<ExecutionAccepted, NotebookCellExecutionRequest>({
      query: (body) => ({ url: "/executions/notebook-cell", method: "POST", body }),
    }),
  }),
});

export const { useSubmitNotebookExecutionMutation } = notebookFeatureApi;
