/*
 * Which server-side Operation statuses mean "stop polling". Kept apart from
 * pollOperation.ts (which pulls in the RTK Query slice) so it can be unit
 * tested on its own. `cancelled` is in the contract even though the backend
 * has no cancel action yet — a poll must stop on it the day one exists,
 * rather than spin until polling_timeout.
 */
import type { OperationStatus } from "../types/api";

const TERMINAL_STATUSES: readonly OperationStatus[] = ["succeeded", "failed", "cancelled", "stale"];

export function isTerminalOperationStatus(status: string): boolean {
  return TERMINAL_STATUSES.includes(status as OperationStatus);
}
