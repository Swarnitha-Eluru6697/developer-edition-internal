/**
 * Codegen feature package (WS-FE-014): the SQL/dbt/FlinkSQL authoring chat.
 */
import { workspaceApi } from "../../api/base";
import type {
  CodegenApplyRequest,
  CodegenMessage,
  CodegenPromptAccepted,
  CodegenPromptRequest,
  CodegenSession,
  CodegenSessionCreate,
  UUID,
  WorkspaceFile,
} from "../../types";

export const codegenFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    createCodegenSession: builder.mutation<CodegenSession, CodegenSessionCreate>({
      query: (body) => ({ url: "/codegen/sessions", method: "POST", body }),
      invalidatesTags: ["CodegenSession"],
    }),
    getCodegenSession: builder.query<CodegenSession, UUID>({
      query: (id) => `/codegen/sessions/${id}`,
      providesTags: (_r, _e, id) => [{ type: "CodegenSession", id }],
    }),
    listCodegenMessages: builder.query<CodegenMessage[], UUID>({
      query: (sessionId) => `/codegen/sessions/${sessionId}/messages`,
      providesTags: (_r, _e, sessionId) => [{ type: "CodegenMessage", id: sessionId }],
    }),
    submitCodegenPrompt: builder.mutation<
      CodegenPromptAccepted,
      { sessionId: UUID; body: CodegenPromptRequest }
    >({
      query: ({ sessionId, body }) => ({
        url: `/codegen/sessions/${sessionId}/messages`,
        method: "POST",
        body,
      }),
      // The reply streams over WS, but invalidate so a refetch (e.g. after
      // reconnect) picks up the persisted transcript too.
      invalidatesTags: (_r, _e, { sessionId }) => [{ type: "CodegenMessage", id: sessionId }],
    }),
    applyCodegenArtifact: builder.mutation<
      WorkspaceFile,
      { sessionId: UUID; body: CodegenApplyRequest }
    >({
      query: ({ sessionId, body }) => ({
        url: `/codegen/sessions/${sessionId}/apply`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_r, _e, { sessionId }) => [
        { type: "File", id: `WS:${sessionId}` },
      ],
    }),
  }),
});

export const {
  useCreateCodegenSessionMutation,
  useGetCodegenSessionQuery,
  useListCodegenMessagesQuery,
  useSubmitCodegenPromptMutation,
  useApplyCodegenArtifactMutation,
} = codegenFeatureApi;
