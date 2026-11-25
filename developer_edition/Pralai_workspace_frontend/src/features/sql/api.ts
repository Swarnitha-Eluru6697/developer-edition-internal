/**
 * SQL feature package (WS-FE-014): SQL execution submission plus the
 * session/cancel/history endpoints shared with the notebook feature package
 * (see app/api/executions.py, which implements both from one router).
 */
import { workspaceApi } from "../../api/base";
import type {
  ExecutionAccepted,
  ExecutionHistoryItem,
  ExecutionResult,
  ExecutionSession,
  SqlExecutionRequest,
  UUID,
} from "../../types";

export const sqlFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    // Create an execution session (the WS channel + owner of executions).
    createExecutionSession: builder.mutation<ExecutionSession, { workspace_id: UUID }>({
      query: (body) => ({ url: "/sessions", method: "POST", body }),
    }),
    // Submit a SQL execution against a session. Results stream over the WS.
    submitSqlExecution: builder.mutation<ExecutionAccepted, SqlExecutionRequest>({
      query: (body) => ({ url: "/executions/sql", method: "POST", body }),
      invalidatesTags: ["Execution"],
    }),
    // Persisted result of one execution — the REST fallback the editor polls
    // (and the history panel loads) when the WS stream may have raced.
    getExecutionResult: builder.query<ExecutionResult, UUID>({
      query: (id) => `/executions/${id}/result`,
    }),
    cancelExecution: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/executions/${id}/cancel`, method: "POST" }),
    }),
    // Query-history panel: the user's past executions, most recent first.
    listExecutions: builder.query<
      ExecutionHistoryItem[],
      { workspace_id?: UUID; language?: string; limit?: number }
    >({
      query: ({ workspace_id, language, limit = 50 }) => ({
        url: "/executions",
        params: { workspace_id, language, limit },
      }),
      providesTags: ["Execution"],
    }),
  }),
});

export const {
  useCreateExecutionSessionMutation,
  useSubmitSqlExecutionMutation,
  useCancelExecutionMutation,
  useListExecutionsQuery,
} = sqlFeatureApi;
