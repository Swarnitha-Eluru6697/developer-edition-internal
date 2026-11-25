import React from "react";
import { Pill } from "./ui";
import { runtimePillClass, runtimeStateLabel } from "../runtimeState";

/** A connector's state as a pill. While Kafka Connect's answer has not arrived yet it shows a neutral "…" — never UNKNOWN,
 * which is reserved for "Connect was asked and could not tell us" (see FE-BUG-16). */
export default function StatePill({ state, loading = false }: { state: string; loading?: boolean }) {
  if (loading) return <Pill cls="bgr"><span aria-hidden="true">…</span><span className="sr-only">Checking state</span></Pill>;
  return <Pill cls={runtimePillClass(state)}>{runtimeStateLabel(state)}</Pill>;
}

/**
 * A connector can be RUNNING with every task healthy and still fail every record: with errors.tolerance=all Kafka Connect skips them and
 * carries on (2026-10-28: a sink read 101,500 records, wrote 0 rows and looked healthy everywhere). `errorRate` is the summary's records/s
 * that failed over the last 5 minutes; above 0 this flag sits next to the state. A paused connector processes nothing, so it never shows.
 */
export function failingRecords(state: string, errorRate: number | null | undefined): boolean {
  return state !== "paused" && typeof errorRate === "number" && errorRate > 0;
}

export function RecordErrorsFlag({ errorRate }: { errorRate: number }) {
  const per = errorRate >= 1 ? `${Math.round(errorRate).toLocaleString()} records/s` : "some records";
  return (
    <span className="badge br" title={`${per} failed in the last 5 minutes and were skipped or sent to the dead-letter queue. Open its metrics to see where.`}>
      errors<span className="sr-only">: {per} failing</span>
    </span>
  );
}
