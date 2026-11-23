/*
 * FE-BUG-06: the one place the polling cadence matrix lives. Every page/
 * helper that polls imports its interval from here instead of hardcoding
 * its own number — the whole point is that these can never quietly drift
 * out of sync with each other (or with what was actually agreed) again.
 *
 *   Pipeline List          15–30s   PIPELINE_LIST_POLL_MS  (Pipelines.tsx)
 *   Pipeline Detail         5–10s   PIPELINE_DETAIL_POLL_MS (Pipelines.tsx's
 *                                     PipelineRuntimeDrawer, while open+visible)
 *   Monitoring              5s      MONITORING_POLL_MS     (Monitoring.tsx)
 *   Connector metrics       15s     CONNECTOR_METRICS_POLL_MS (ConnectorMetricsPanel.tsx —
 *                                     Prometheus scrapes every 15s and the backend
 *                                     caches 10s, so polling faster shows nothing new)
 *   Operation in progress   0.15–3s OPERATION_POLL_MS       (pollUntilTerminal.ts: quick first checks,
 *                                     doubling up to this)
 *   Browser hidden          paused  — see hooks/usePolling.ts's visibility gating,
 *                                     which every *page-level* poll above already
 *                                     goes through. OPERATION_POLL_MS is the one
 *                                     exception: it drives a single in-flight
 *                                     watch (submit an action, wait for its real
 *                                     outcome), not a standing subscription, so
 *                                     it isn't gated the same way.
 *
 */
export const PIPELINE_LIST_POLL_MS = 20_000;
export const PIPELINE_DETAIL_POLL_MS = 5_000;
export const MONITORING_POLL_MS = 5_000;
export const CONNECTOR_METRICS_POLL_MS = 15_000;
export const OPERATION_POLL_MS = 3_000;
// Alerts (pages/Alerts.tsx and the sidebar badge): the sweep itself runs once a minute, so asking more often shows nothing new.
export const ALERT_COUNTS_POLL_MS = 60_000;
