/**
 * dbt feature package (WS-FE-014): dbt Runner endpoints, proxied through the
 * workspace backend at /api/v1/dbt/*. The proxy declares response models
 * (backend app/schemas/proxy.py), so every type here is an alias into the
 * generated OpenAPI schema — see src/types/index.ts.
 */
import { workspaceApi } from "../../api/base";
import type {
  DbtLineage,
  DbtProject,
  DbtProjectRegister,
  DbtProjectUpdate,
  DbtRun,
  DbtRunEvent,
  UUID,
} from "../../types";

export const dbtFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    listDbtProjects: builder.query<DbtProject[], { workspaceId?: UUID }>({
      query: ({ workspaceId } = {}) =>
        workspaceId ? `/dbt/projects?workspace_id=${workspaceId}` : "/dbt/projects",
      // One tag per project (plus a list tag): triggering a run or editing a project
      // invalidates { DbtProject, id }, which only reaches queries that provide that id —
      // without these the list's "Last run" column stayed stale until a page refresh.
      providesTags: (result) => [
        { type: "DbtProject" as const, id: "LIST" },
        ...(result ?? []).map((p) => ({ type: "DbtProject" as const, id: p.id })),
      ],
    }),
    registerDbtProject: builder.mutation<DbtProject, DbtProjectRegister>({
      query: (body) => ({ url: "/dbt/projects/register", method: "POST", body }),
      invalidatesTags: ["DbtProject"],
    }),
    getDbtProject: builder.query<DbtProject, UUID>({
      query: (id) => `/dbt/projects/${id}`,
      providesTags: (_r, _e, id) => [{ type: "DbtProject", id }],
    }),
    updateDbtProject: builder.mutation<DbtProject, { id: UUID; body: DbtProjectUpdate }>({
      query: ({ id, body }) => ({ url: `/dbt/projects/${id}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, { id }) => [{ type: "DbtProject", id }],
    }),
    deleteDbtProject: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/dbt/projects/${id}`, method: "DELETE" }),
      invalidatesTags: ["DbtProject"],
    }),
    triggerDbtRun: builder.mutation<DbtRun, { projectId: UUID; select?: string }>({
      query: ({ projectId, select }) => ({ url: `/dbt/projects/${projectId}/run`, method: "POST", body: { select } }),
      invalidatesTags: (_r, _e, { projectId }) => [{ type: "DbtProject", id: projectId }],
    }),
    listDbtRuns: builder.query<DbtRun[], UUID>({
      query: (projectId) => `/dbt/projects/${projectId}/runs`,
      providesTags: (_r, _e, projectId) => [{ type: "DbtProject", id: projectId }],
    }),
    getDbtRun: builder.query<DbtRun, { projectId: UUID; runId: UUID }>({
      query: ({ projectId, runId }) => `/dbt/projects/${projectId}/runs/${runId}`,
    }),
    listDbtRunEvents: builder.query<DbtRunEvent[], { projectId: UUID; runId: UUID }>({
      query: ({ projectId, runId }) => `/dbt/projects/${projectId}/runs/${runId}/events`,
    }),
    getDbtLineage: builder.query<DbtLineage, UUID>({
      query: (projectId) => `/dbt/projects/${projectId}/lineage`,
    }),
  }),
});

export const {
  useListDbtProjectsQuery,
  useRegisterDbtProjectMutation,
  useGetDbtProjectQuery,
  useUpdateDbtProjectMutation,
  useDeleteDbtProjectMutation,
  useTriggerDbtRunMutation,
  useListDbtRunsQuery,
  useGetDbtRunQuery,
  useListDbtRunEventsQuery,
  useGetDbtLineageQuery,
} = dbtFeatureApi;
