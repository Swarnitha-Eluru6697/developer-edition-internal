import type { EngineStatus } from "../../types";

/** Header counts for the Settings page: how many services are connected, and
 *  how many need attention (not connected, or connected but missing config). */
export function summarizeEngines(engines: EngineStatus[]): { connected: number; needsSetup: number } {
  return {
    connected: engines.filter((e) => e.status === "connected").length,
    needsSetup: engines.filter((e) => e.status !== "connected" || e.missing.length > 0).length,
  };
}
