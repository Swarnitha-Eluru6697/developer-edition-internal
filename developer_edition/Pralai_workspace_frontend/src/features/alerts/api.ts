/**
 * Alerts feature package: /api/v1/alerts/* (failures of the jobs, the rules that raise them, the Slack / Teams channels they are posted to).
 * Every type is an alias into the generated OpenAPI schema (src/types/index.ts).
 */
import { workspaceApi } from "../../api/base";
import type {
  Alert, AlertChannel, AlertChannelCreate, AlertChannelPatch, AlertChannelTest, AlertCounts, AlertDetail, AlertRule,
  AlertRuleCreate, AlertRuleKindInfo, AlertRulePatch, AlertState, AlertsPage, AlertSweep, UUID,
} from "../../types";

export interface AlertListArgs { state: AlertState; from?: string; to?: string; limit: number; offset: number }

const qs = (a: AlertListArgs) => {
  const p = new URLSearchParams({ state: a.state, limit: String(a.limit), offset: String(a.offset) });
  if (a.from) p.set("from", a.from);
  if (a.to) p.set("to", a.to);
  return p.toString();
};

export const alertsFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    listAlerts: builder.query<AlertsPage, AlertListArgs>({
      query: (a) => `/alerts?${qs(a)}`,
      providesTags: [{ type: "Alert", id: "LIST" }],
    }),
    getAlertCounts: builder.query<AlertCounts, void>({
      query: () => "/alerts/counts",
      providesTags: [{ type: "Alert", id: "LIST" }],
    }),
    getAlert: builder.query<AlertDetail, UUID>({
      query: (id) => `/alerts/${id}`,
      providesTags: (_r, _e, id) => [{ type: "Alert", id }],
    }),
    acknowledgeAlert: builder.mutation<AlertDetail, { id: UUID; note?: string }>({
      query: ({ id, note }) => ({ url: `/alerts/${id}/acknowledge`, method: "POST", body: { note: note || null } }),
      invalidatesTags: (_r, _e, { id }) => [{ type: "Alert", id }, { type: "Alert", id: "LIST" }],
    }),
    resolveAlert: builder.mutation<AlertDetail, { id: UUID; note?: string }>({
      query: ({ id, note }) => ({ url: `/alerts/${id}/resolve`, method: "POST", body: { note: note || null } }),
      invalidatesTags: (_r, _e, { id }) => [{ type: "Alert", id }, { type: "Alert", id: "LIST" }],
    }),
    listAlertRules: builder.query<AlertRule[], void>({
      query: () => "/alerts/rules",
      providesTags: [{ type: "AlertRule", id: "LIST" }],
    }),
    listAlertRuleKinds: builder.query<AlertRuleKindInfo[], void>({ query: () => "/alerts/rules/kinds" }),
    createAlertRule: builder.mutation<AlertRule, AlertRuleCreate>({
      query: (body) => ({ url: "/alerts/rules", method: "POST", body }),
      invalidatesTags: [{ type: "AlertRule", id: "LIST" }],
    }),
    updateAlertRule: builder.mutation<AlertRule, { id: UUID; body: AlertRulePatch }>({
      query: ({ id, body }) => ({ url: `/alerts/rules/${id}`, method: "PATCH", body }),
      invalidatesTags: [{ type: "AlertRule", id: "LIST" }],
    }),
    deleteAlertRule: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/alerts/rules/${id}`, method: "DELETE" }),
      invalidatesTags: [{ type: "AlertRule", id: "LIST" }],
    }),
    runAlertRules: builder.mutation<AlertSweep, void>({
      query: () => ({ url: "/alerts/rules/run", method: "POST" }),
      invalidatesTags: [{ type: "Alert", id: "LIST" }, { type: "AlertRule", id: "LIST" }, { type: "AlertChannel", id: "LIST" }],
    }),
    listAlertChannels: builder.query<AlertChannel[], void>({
      query: () => "/alerts/channels",
      providesTags: [{ type: "AlertChannel", id: "LIST" }],
    }),
    createAlertChannel: builder.mutation<AlertChannel, AlertChannelCreate>({
      query: (body) => ({ url: "/alerts/channels", method: "POST", body }),
      invalidatesTags: [{ type: "AlertChannel", id: "LIST" }, { type: "Alert", id: "LIST" }],
    }),
    updateAlertChannel: builder.mutation<AlertChannel, { id: UUID; body: AlertChannelPatch }>({
      query: ({ id, body }) => ({ url: `/alerts/channels/${id}`, method: "PATCH", body }),
      invalidatesTags: [{ type: "AlertChannel", id: "LIST" }],
    }),
    deleteAlertChannel: builder.mutation<void, UUID>({
      query: (id) => ({ url: `/alerts/channels/${id}`, method: "DELETE" }),
      invalidatesTags: [{ type: "AlertChannel", id: "LIST" }],
    }),
    testAlertChannel: builder.mutation<AlertChannelTest, UUID>({
      query: (id) => ({ url: `/alerts/channels/${id}/test`, method: "POST" }),
      invalidatesTags: [{ type: "AlertChannel", id: "LIST" }],
    }),
  }),
});

export const {
  useListAlertsQuery, useGetAlertCountsQuery, useGetAlertQuery, useAcknowledgeAlertMutation, useResolveAlertMutation,
  useListAlertRulesQuery, useListAlertRuleKindsQuery, useCreateAlertRuleMutation, useUpdateAlertRuleMutation,
  useDeleteAlertRuleMutation, useRunAlertRulesMutation, useListAlertChannelsQuery, useCreateAlertChannelMutation,
  useUpdateAlertChannelMutation, useDeleteAlertChannelMutation, useTestAlertChannelMutation,
} = alertsFeatureApi;

export type { Alert };
