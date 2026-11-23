/*
 * Polls GET /discovery/{id} until it reaches a terminal status — the
 * "submit, then wait for the real outcome" half of the connection discover
 * flow (GAP-10), the same shape as pollOperation.ts but against a
 * DiscoveryRun rather than an Operation: the discovered objects themselves
 * never land here, only the summary (status/object_count) — see
 * getDiscoveryObjects (apiSlice.ts) for paging through the results once
 * this resolves.
 *
 * FE-BUG-05: see pollUntilTerminal.ts for why this returns a PollOutcome
 * instead of throwing when it gives up.
 */
import { dataConnectApi } from "./apiSlice";
import { pollUntilTerminal, type PollOutcome, type PollUntilTerminalOptions } from "./pollUntilTerminal";
import type { AppDispatch } from "./store";
import type { DiscoveryRun } from "../types/api";

export type PollDiscoveryRunOutcome = PollOutcome<DiscoveryRun>;

function isTerminal(run: DiscoveryRun): boolean {
  return run.status === "succeeded" || run.status === "failed";
}

export async function pollDiscoveryRun(
  dispatch: AppDispatch,
  runId: string,
  options?: PollUntilTerminalOptions,
): Promise<PollDiscoveryRunOutcome> {
  return pollUntilTerminal(
    async () => {
      const query = dispatch(dataConnectApi.endpoints.getDiscoveryRun.initiate(runId, { forceRefetch: true }));
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
