/**
 * Flink feature package (WS-FE-014): FlinkSQL Runner endpoints, proxied
 * through the workspace backend at /api/v1/flink/*. The proxy declares
 * response models (backend app/schemas/proxy.py), so every type here is an
 * alias into the generated OpenAPI schema — see src/types/index.ts.
 */
import { workspaceApi } from "../../api/base";
import type {
  FlinkDeployment,
  FlinkJob,
  FlinkJobCreate,
  FlinkJobFromGit,
  FlinkJobLiveStatus,
  FlinkJobSync,
  FlinkSqlValidate,
  FlinkUi,
  FlinkValidateResponse,
  UUID,
} from "../../types";

export const flinkFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    listFlinkJobs: builder.query<FlinkJob[], void>({
      query: () => "/flink/jobs",
      providesTags: ["FlinkJob"],
    }),
    createFlinkJob: builder.mutation<FlinkJob, FlinkJobCreate>({
      query: (body) => ({ url: "/flink/jobs", method: "POST", body }),
      invalidatesTags: ["FlinkJob"],
    }),
    // Register a job from a Git repo — the workspace service clones it and
    // submits the assembled SQL to the runner (same shape as dbt registration).
    createFlinkJobFromGit: builder.mutation<FlinkJob, FlinkJobFromGit>({
      query: (body) => ({ url: "/flink/jobs/from-git", method: "POST", body }),
      invalidatesTags: ["FlinkJob"],
    }),
    syncFlinkJob: builder.mutation<FlinkJob, { id: UUID; body?: FlinkJobSync }>({
      query: ({ id, body }) => ({ url: `/flink/jobs/${id}/sync`, method: "POST", body: body ?? {} }),
      invalidatesTags: ["FlinkJob"],
    }),
    // Ad-hoc syntax check for the FlinkSQL editor — creates no job.
    validateFlinkSql: builder.mutation<FlinkValidateResponse, FlinkSqlValidate>({
      query: (body) => ({ url: "/flink/validate", method: "POST", body }),
    }),
    // Browser-reachable Flink Web UI base ("" when not configured).
    getFlinkUiUrl: builder.query<FlinkUi, void>({
      query: () => "/flink/ui",
    }),
    // Live status — the only place the Flink cluster's JobID is exposed, which
    // the Web UI deep link needs. Fetched on demand, not polled.
    getFlinkJobStatus: builder.query<FlinkJobLiveStatus, UUID>({
      query: (id) => `/flink/jobs/${id}/status`,
    }),
    deleteFlinkJob: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/flink/jobs/${id}`, method: "DELETE" }),
      invalidatesTags: ["FlinkJob"],
    }),
    validateFlinkJob: builder.mutation<FlinkJob, UUID>({
      query: (id) => ({ url: `/flink/jobs/${id}/validate`, method: "POST" }),
      invalidatesTags: ["FlinkJob"],
    }),
    deployFlinkJob: builder.mutation<FlinkDeployment, UUID>({
      // deploy/stop take a JSON body (DeployRequest/StopRequest, all optional) —
      // send {} so FastAPI doesn't 422 on a missing body.
      query: (id) => ({ url: `/flink/jobs/${id}/deploy`, method: "POST", body: {} }),
      invalidatesTags: ["FlinkJob"],
    }),
    stopFlinkJob: builder.mutation<FlinkDeployment, UUID>({
      query: (id) => ({ url: `/flink/jobs/${id}/stop`, method: "POST", body: {} }),
      invalidatesTags: ["FlinkJob"],
    }),
  }),
});

export const {
  useListFlinkJobsQuery,
  useCreateFlinkJobMutation,
  useCreateFlinkJobFromGitMutation,
  useSyncFlinkJobMutation,
  useValidateFlinkSqlMutation,
  useGetFlinkUiUrlQuery,
  useLazyGetFlinkJobStatusQuery,
  useDeleteFlinkJobMutation,
  useValidateFlinkJobMutation,
  useDeployFlinkJobMutation,
  useStopFlinkJobMutation,
} = flinkFeatureApi;
