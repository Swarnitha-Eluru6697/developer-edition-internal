/**
 * Workspace feature package (WS-FE-014): workspace CRUD, files, and the
 * execution-session lifecycle (a session is a workspace-scoped runtime
 * concept — see app/api/executions.py's create_session/terminate_session —
 * shared by both the sql and notebook feature packages).
 */
import { workspaceApi } from "../../api/base";
import type {
  CurrentUser,
  FileCreate,
  FileTreeNode,
  FileUpdate,
  FileWithContent,
  MeResponse,
  UUID,
  Workspace,
  WorkspaceCreate,
  WorkspaceFile,
  WorkspaceUpdate,
} from "../../types";
import { flattenMe } from "./me";

export const workspaceFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    // -------- Auth / identity --------
    getMe: builder.query<CurrentUser, void>({
      query: () => "/auth/me",
      // The backend nests identity under "claims" and "user" (MeOut) — see
      // ./me.ts for the flattening into CurrentUser.
      transformResponse: (raw: MeResponse): CurrentUser => flattenMe(raw),
      providesTags: ["Me"],
    }),

    // -------- Workspaces --------
    listWorkspaces: builder.query<Workspace[], void>({
      query: () => "/workspaces",
      providesTags: ["Workspace"],
    }),
    getWorkspace: builder.query<Workspace, UUID>({
      query: (id) => `/workspaces/${id}`,
      providesTags: (_r, _e, id) => [{ type: "Workspace", id }],
    }),
    createWorkspace: builder.mutation<Workspace, WorkspaceCreate>({
      query: (body) => ({ url: "/workspaces", method: "POST", body }),
      invalidatesTags: ["Workspace"],
    }),
    // PATCH /workspaces/{id} (app/api/workspaces.py update_workspace) — rename. The checked-in OpenAPI
    // snapshot predates this route; the body is the backend's WorkspaceUpdate.
    updateWorkspace: builder.mutation<Workspace, { id: UUID; body: WorkspaceUpdate }>({
      query: ({ id, body }) => ({ url: `/workspaces/${id}`, method: "PATCH", body }),
      invalidatesTags: ["Workspace"],
    }),
    deleteWorkspace: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/workspaces/${id}`, method: "DELETE" }),
      invalidatesTags: ["Workspace"],
    }),

    // -------- Files --------
    getFileTree: builder.query<FileTreeNode, { workspaceId: UUID; branch?: string }>({
      query: ({ workspaceId, branch }) =>
        `/workspaces/${workspaceId}/files/tree${branch ? `?branch=${branch}` : ""}`,
      providesTags: (_r, _e, { workspaceId }) => [{ type: "File", id: `TREE:${workspaceId}` }],
    }),
    listFiles: builder.query<WorkspaceFile[], { workspaceId: UUID; branch?: string }>({
      query: ({ workspaceId, branch }) =>
        `/workspaces/${workspaceId}/files${branch ? `?branch=${branch}` : ""}`,
      providesTags: (result, _e, { workspaceId }) =>
        result
          ? [
              ...result.map((f) => ({ type: "File" as const, id: f.id })),
              { type: "File", id: `WS:${workspaceId}` },
            ]
          : [{ type: "File", id: `WS:${workspaceId}` }],
    }),
    getFile: builder.query<FileWithContent, { workspaceId: UUID; fileId: UUID }>({
      query: ({ workspaceId, fileId }) =>
        `/workspaces/${workspaceId}/files/${fileId}`,
      providesTags: (_r, _e, { fileId }) => [{ type: "File", id: fileId }],
    }),
    createFile: builder.mutation<
      WorkspaceFile,
      { workspaceId: UUID; body: FileCreate }
    >({
      query: ({ workspaceId, body }) => ({
        url: `/workspaces/${workspaceId}/files`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_r, _e, { workspaceId }) => [
        { type: "File", id: `WS:${workspaceId}` },
        { type: "File", id: `TREE:${workspaceId}` },
      ],
    }),
    // The optimistic-concurrency field is `if_match_sha256` and belongs in
    // the JSON body — the backend never reads an `If-Match` header (see
    // app/services/file_service.py). The previous version of this mutation
    // sent `headers: { "If-Match": body.expected_hash }`, a field name the
    // backend has never recognized under any transport, so a stale-write
    // conflict was never actually caught (WS-FE-014 drift).
    updateFile: builder.mutation<
      WorkspaceFile,
      { workspaceId: UUID; fileId: UUID; body: FileUpdate }
    >({
      query: ({ workspaceId, fileId, body }) => ({
        url: `/workspaces/${workspaceId}/files/${fileId}`,
        method: "PUT",
        body,
      }),
      invalidatesTags: (_r, _e, { fileId }) => [{ type: "File", id: fileId }],
    }),
    deleteFile: builder.mutation<void, { workspaceId: UUID; fileId: UUID }>({
      query: ({ workspaceId, fileId }) => ({
        url: `/workspaces/${workspaceId}/files/${fileId}`,
        method: "DELETE",
      }),
      invalidatesTags: (_r, _e, { workspaceId, fileId }) => [
        { type: "File", id: fileId },
        { type: "File", id: `WS:${workspaceId}` },
        { type: "File", id: `TREE:${workspaceId}` },
      ],
    }),
  }),
});

export const {
  useGetMeQuery,
  useListWorkspacesQuery,
  useGetWorkspaceQuery,
  useCreateWorkspaceMutation,
  useDeleteWorkspaceMutation,
  useUpdateWorkspaceMutation,
  useGetFileTreeQuery,
  useListFilesQuery,
  useGetFileQuery,
  useCreateFileMutation,
  useUpdateFileMutation,
  useDeleteFileMutation,
} = workspaceFeatureApi;
