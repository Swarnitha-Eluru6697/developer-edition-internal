/**
 * Data Quality feature package (WS-FE-014): the rule library, rulesets, and
 * the bindings that attach a ruleset to a running job, plus the render. Proxied
 * through the workspace backend at /api/v1/dq/*, whose response models
 * (backend app/schemas/proxy.py) make every type here an alias into the
 * generated OpenAPI schema — see src/types/index.ts.
 */
import { workspaceApi } from "../../api/base";
import type {
  DqApplyResult,
  DqBinding,
  DqBindingCreate,
  DqRender,
  DqRule,
  DqRulesetItem,
  DqRulesetSummary,
  DqUcsSyncReport,
  UUID,
} from "../../types";

export const dqFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    // Rule library. System rules ship with the service and are read-only (409 on any change).
    listDqRules: builder.query<DqRule[], void>({
      query: () => "/dq/rules",
      transformResponse: (r: DqRule[] | { items: DqRule[] }) => (Array.isArray(r) ? r : r?.items ?? []),
      providesTags: ["DqRule"],
    }),
    createDqRule: builder.mutation<DqRule, Record<string, unknown>>({
      query: (body) => ({ url: "/dq/rules", method: "POST", body }),
      invalidatesTags: ["DqRule"],
    }),
    // Name, type and dimension are fixed once a rule exists; the rest propagates to every ruleset using it.
    updateDqRule: builder.mutation<DqRule, { id: UUID; body: Record<string, unknown> }>({
      query: ({ id, body }) => ({ url: `/dq/rules/${id}`, method: "PATCH", body }),
      invalidatesTags: ["DqRule", "DqRuleset"],
    }),
    deleteDqRule: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/dq/rules/${id}`, method: "DELETE" }),
      invalidatesTags: ["DqRule"],
    }),

    // Rulesets: rules pinned to columns of one table — what a binding attaches to a job.
    listDqRulesets: builder.query<DqRulesetSummary[], void>({
      query: () => "/dq/rulesets",
      providesTags: ["DqRuleset"],
    }),
    createDqRuleset: builder.mutation<DqRulesetSummary, { name: string; description: string | null }>({
      query: (body) => ({ url: "/dq/rulesets", method: "POST", body }),
      invalidatesTags: ["DqRuleset"],
    }),
    deleteDqRuleset: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/dq/rulesets/${id}`, method: "DELETE" }),
      invalidatesTags: ["DqRuleset", "DqBinding"],
    }),
    listDqRulesetItems: builder.query<DqRulesetItem[], UUID>({
      query: (rulesetId) => `/dq/rulesets/${rulesetId}/items`,
      providesTags: (_r, _e, id) => [{ type: "DqRuleset", id }],
    }),
    addDqRulesetItem: builder.mutation<DqRulesetItem, { rulesetId: UUID; body: Record<string, unknown> }>({
      query: ({ rulesetId, body }) => ({ url: `/dq/rulesets/${rulesetId}/items`, method: "POST", body }),
      invalidatesTags: ["DqRuleset"],
    }),
    updateDqRulesetItem: builder.mutation<DqRulesetItem, { rulesetId: UUID; itemId: UUID; body: Record<string, unknown> }>({
      query: ({ rulesetId, itemId, body }) => ({ url: `/dq/rulesets/${rulesetId}/items/${itemId}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, a) => [{ type: "DqRuleset", id: a.rulesetId }],
    }),
    deleteDqRulesetItem: builder.mutation<void, { rulesetId: UUID; itemId: UUID }>({
      query: ({ rulesetId, itemId }) => ({ url: `/dq/rulesets/${rulesetId}/items/${itemId}`, method: "DELETE" }),
      invalidatesTags: ["DqRuleset"],
    }),
    listDqBindings: builder.query<DqBinding[], { jobType?: DqBinding["job_type"] } | void>({
      query: (arg) => (arg && arg.jobType ? `/dq/bindings?job_type=${arg.jobType}` : "/dq/bindings"),
      providesTags: ["DqBinding"],
    }),
    createDqBinding: builder.mutation<DqBinding, DqBindingCreate>({
      query: (body) => ({ url: "/dq/bindings", method: "POST", body }),
      invalidatesTags: ["DqBinding"],
    }),
    // DQ-BIND-003: resolve the binding's job (runner) + dataset (UCS) before apply.
    resolveDqBinding: builder.mutation<DqBinding, UUID>({
      query: (id) => ({ url: `/dq/bindings/${id}/resolve`, method: "POST" }),
      invalidatesTags: ["DqBinding"],
    }),
    deleteDqBinding: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/dq/bindings/${id}`, method: "DELETE" }),
      invalidatesTags: ["DqBinding"],
    }),
    // Cached server-side against a hash of the ruleset — cheap to call repeatedly.
    getDqRender: builder.query<DqRender, UUID>({
      query: (bindingId) => `/dq/render/${bindingId}`,
      providesTags: (_r, _e, id) => [{ type: "DqRender", id }],
    }),
    // Forces a re-render when the ruleset changed but the cache hasn't rolled over.
    refreshDqRender: builder.mutation<DqRender, UUID>({
      query: (bindingId) => ({ url: `/dq/render/${bindingId}/refresh`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "DqRender", id }],
    }),
    // Pushes the rendered artifact to the runner that will execute it. Until
    // this runs, a binding records intent and changes nothing about the job.
    applyDqBinding: builder.mutation<DqApplyResult, UUID>({
      query: (bindingId) => ({ url: `/dq/bindings/${bindingId}/apply`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "DqRender", id }],
    }),
    // Registers bound rules as quality checks in the catalog. Answers 200 even
    // on partial failure — read checks_failed, don't rely on the HTTP status.
    syncDqToUcs: builder.mutation<DqUcsSyncReport, { bindingId?: UUID } | void>({
      query: (arg) => ({
        url: arg && arg.bindingId ? `/dq/sync/ucs?binding_id=${arg.bindingId}` : "/dq/sync/ucs",
        method: "POST",
      }),
    }),
  }),
});

export const {
  useListDqRulesQuery,
  useCreateDqRuleMutation,
  useUpdateDqRuleMutation,
  useDeleteDqRuleMutation,
  useCreateDqRulesetMutation,
  useDeleteDqRulesetMutation,
  useListDqRulesetItemsQuery,
  useAddDqRulesetItemMutation,
  useUpdateDqRulesetItemMutation,
  useDeleteDqRulesetItemMutation,
  useListDqRulesetsQuery,
  useListDqBindingsQuery,
  useCreateDqBindingMutation,
  useDeleteDqBindingMutation,
  useGetDqRenderQuery,
  useLazyGetDqRenderQuery,
  useRefreshDqRenderMutation,
  useApplyDqBindingMutation,
  useSyncDqToUcsMutation,
  useResolveDqBindingMutation,
} = dqFeatureApi;
