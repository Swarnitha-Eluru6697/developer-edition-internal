import { useMemo } from "react";
import { useGetConnectorMetricsSummaryQuery } from "../store/apiSlice";
import type { ConnectorMetricsSummary, ConnectorRate } from "../types/api";

/**
 * The current rate / lag / task counts of every connector this org owns (GET /runtime/connectors/metrics/summary),
 * `stale` = the latest poll failed: the rows are then the last real answer and must not be shown as current.
 */
export function useConnectorSummary(pollingInterval: number) {
  const query = useGetConnectorMetricsSummaryQuery(undefined, { pollingInterval });
  const data: ConnectorMetricsSummary | undefined = query.data;
  const byName = useMemo(() => {
    const map: Record<string, ConnectorRate> = {};
    (data?.connectors ?? []).forEach((c) => { map[c.connector_name] = c; });
    return map;
  }, [data]);
  const stale = Boolean(query.error);
  /** Prometheus can answer: configured and reachable on the latest poll. */
  const available = Boolean(data?.prometheus_enabled && data?.reachable) && !stale;
  return { data, byName, stale, available, error: query.error, isLoading: query.isLoading, refetch: query.refetch };
}
