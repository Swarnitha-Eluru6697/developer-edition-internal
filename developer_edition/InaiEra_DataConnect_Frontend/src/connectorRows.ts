/*
 * Shared, pure helpers for the Dashboard, Monitoring and Pipelines pages: which pipelines count as deployed, which
 * way their connector faces, and what state Kafka Connect reports for them right now.
 */
import { deriveFlowConnectorState } from "./runtimeState";
import type { Pipeline, RuntimeFlow } from "./types/api";

/** A pipeline whose connector exists (or should): drafts and pipelines being deleted never appear in the connector views. */
export function isDeployed(p: Pipeline): boolean {
  return Boolean(p.connector_name) && !["draft", "deleting"].includes(String(p.status ?? "").toLowerCase());
}

export function directionOf(p: Pipeline): "source" | "sink" {
  return String(p.direction ?? "").toUpperCase() === "SINK" ? "sink" : "source";
}

type FlowConnector = NonNullable<RuntimeFlow["connectors"]>[number];

export function flowIndex(flow: RuntimeFlow | undefined): Record<string, FlowConnector> {
  const byName: Record<string, FlowConnector> = {};
  (flow?.connectors ?? []).forEach((c) => { byName[c.name] = c; });
  return byName;
}

/**
 * The connector's state, honestly: "unknown" whenever the flow call failed (stale) or Kafka Connect itself couldn't be
 * reached — a state we couldn't read is never shown as healthy (FE-BUG-16).
 */
export function connectorStateOf(p: Pipeline, byName: Record<string, FlowConnector>, flowUnhealthy: boolean): string {
  if (flowUnhealthy) return "unknown";
  return deriveFlowConnectorState(byName[p.connector_name]);
}

/** Connect's own failed-task count for a connector (tasks whose state is FAILED), 0 if it isn't in the flow. */
export function failedTaskCount(c: FlowConnector | undefined): number {
  return (c?.tasks ?? []).filter((t) => String(t ?? "").toUpperCase() === "FAILED").length;
}
