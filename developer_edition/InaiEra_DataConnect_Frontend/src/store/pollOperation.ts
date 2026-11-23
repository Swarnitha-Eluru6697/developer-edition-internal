/*
 * Polls GET /operations/{id} until it reaches a terminal status — the
 * "submit, then wait for the real outcome" half of the pipeline
 * deploy/pause/resume/restart flow (see api.ts's own header comment for why:
 * it waits on a live Kafka Connect round-trip, and a production API
 * shouldn't tie the HTTP request's lifetime to how long that takes).
 *
 * Goes through `dataConnectApi.endpoints.getOperation.initiate(...)` — the
 * same cache every other page reads operations through — rather than a
 * bare `fetch`, so this benefits from api.ts's existing retry-on-transient-
 * failure behavior for GETs. Each poll's subscription is torn down right
 * after it resolves (`keepUnusedDataFor: 0` would do this anyway on the
 * next tick, but not any sooner) — this loop never wants a stale cached
 * "queued" served back to it.
 *
 * FE-BUG-05: see pollUntilTerminal.ts for why this returns a PollOutcome
 * instead of throwing when it gives up.
 */
import { dataConnectApi } from "./apiSlice";
import { pollUntilTerminal, type PollOutcome, type PollUntilTerminalOptions } from "./pollUntilTerminal";
import type { AppDispatch } from "./store";
import { isTerminalOperationStatus } from "./operationStatus";
import type { Operation } from "../types/api";

export type PollOperationOutcome = PollOutcome<Operation>;

function isTerminal(operation: Operation): boolean {
  return isTerminalOperationStatus(operation.status);
}

export async function pollOperation(
  dispatch: AppDispatch,
  operationId: string,
  options?: PollUntilTerminalOptions,
): Promise<PollOperationOutcome> {
  return pollUntilTerminal(
    async () => {
      const query = dispatch(dataConnectApi.endpoints.getOperation.initiate(operationId, { forceRefetch: true }));
      try {
        return await query.unwrap();
      } finally {
        query.unsubscribe();
      }
    },
    isTerminal,
    options,
  );
}
