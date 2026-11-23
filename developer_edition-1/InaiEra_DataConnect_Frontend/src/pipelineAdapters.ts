/*
 * FE-GAP-02: a pipeline arriving inside another payload. A 409 PIPELINE_VERSION_CONFLICT carries the server's current copy of the pipeline
 * in the error's `details.current` — free-form JSON on the wire. The conflict screen only compares a handful of fields, so that is
 * exactly what is validated: `PipelineConflictView` has them, typed, and nothing else is claimed. A malformed or absent `current`
 * gives null, and the caller falls back to the generic error path instead of rendering `undefined`s.
 */
import { isRecord, numberOf, parseJson, recordsOf, stringOf, stringsOf, type JsonRecord } from "./json";
import type { Pipeline, RuntimeFlow, RuntimeFlowConnector, RuntimeFlowSink, RuntimeFlowTopic } from "./types/api";

export type PipelineConflictView =
  Pick<Pipeline, "name" | "quality_policy_id" | "quality_mode" | "error_handling_policy" | "error_threshold_max_errors" | "error_threshold_window_seconds">;

/** How a quality policy is applied (NEW-01): OBSERVE counts failures and copies them to quarantine while the data topic still carries every
 * record; ENFORCE sends only passing records to the prepared topic. Anything that is not exactly "ENFORCE" is OBSERVE — the safe reading of a
 * missing or unknown value, and what every pipeline did before the field existed. */
export type QualityMode = "OBSERVE" | "ENFORCE";
export function qualityModeOf(value: unknown): QualityMode {
  return value === "ENFORCE" ? "ENFORCE" : "OBSERVE";
}

/** A field that may be absent or null on the wire: undefined/null stay as they are, a value of the wrong type is dropped (undefined). */
function optionalString(value: unknown): string | null | undefined {
  return value === null ? null : stringOf(value);
}
function optionalNumber(value: unknown): number | null | undefined {
  return value === null ? null : numberOf(value);
}

export function conflictFromDetails(value: unknown): PipelineConflictView | null {
  if (!isRecord(value)) return null;
  const name = stringOf(value.name);
  if (name === undefined) return null;
  return {
    name,
    quality_policy_id: optionalString(value.quality_policy_id),
    quality_mode: stringOf(value.quality_mode),
    error_handling_policy: optionalString(value.error_handling_policy),
    error_threshold_max_errors: optionalNumber(value.error_threshold_max_errors),
    error_threshold_window_seconds: optionalNumber(value.error_threshold_window_seconds),
  };
}

/**
 * A runtime-flow snapshot read back from a dynamic boundary (browser storage: another tab, an older build, hand-edited): the text is
 * parsed and checked for the fields a RuntimeFlow must have, and anything else is ignored rather than trusted.
 */
export function runtimeFlowFromStorage(raw: string | null): RuntimeFlow | undefined {
  if (!raw) return undefined;
  const parsed = parseJson(raw);
  return parsed.ok ? runtimeFlowOf(parsed.value) : undefined;
}

/** `runtimeFlowFromStorage` for a value that is already parsed. */
export function runtimeFlowOf(value: unknown): RuntimeFlow | undefined {
  if (!isRecord(value)) return undefined;
  const v = value;
  const listsOk = (["connectors", "topics", "sinks"] as const).every((key) => v[key] === undefined || Array.isArray(v[key]));
  const connectUrl = stringOf(v.connect_url);
  const bootstrap = stringOf(v.bootstrap);
  if (typeof v.reachable !== "boolean" || connectUrl === undefined || bootstrap === undefined || !listsOk) return undefined;
  // Rebuilt field by field, never cast: an entry of the wrong shape (an older build, a hand edit) is dropped rather than trusted.
  return {
    reachable: v.reachable,
    connect_url: connectUrl,
    bootstrap,
    connectors: recordsOf(v.connectors).flatMap(flowConnectorOf),
    topics: recordsOf(v.topics).flatMap(flowTopicOf),
    sinks: recordsOf(v.sinks).flatMap(flowSinkOf),
    ...(stringOf(v.message) !== undefined ? { message: stringOf(v.message) } : {}),
  };
}

function flowConnectorOf(c: JsonRecord): RuntimeFlowConnector[] {
  const name = stringOf(c.name);
  const pipelineId = stringOf(c.pipeline_id);
  const type = stringOf(c.type);
  const state = stringOf(c.state);
  if (name === undefined || pipelineId === undefined || type === undefined || state === undefined) return [];
  const tasks = Array.isArray(c.tasks) ? c.tasks.flatMap((t): (string | null)[] => (t === null || typeof t === "string" ? [t] : [])) : undefined;
  return [{ name, pipeline_id: pipelineId, type, state, ...(tasks ? { tasks } : {}), ...(Array.isArray(c.topics) ? { topics: stringsOf(c.topics) } : {}) }];
}

function flowTopicOf(t: JsonRecord): RuntimeFlowTopic[] {
  const topic = stringOf(t.topic);
  const messages = numberOf(t.messages);
  const pipelineId = stringOf(t.pipeline_id);
  return topic === undefined || messages === undefined || pipelineId === undefined ? [] : [{ topic, messages, pipeline_id: pipelineId }];
}

function flowSinkOf(s: JsonRecord): RuntimeFlowSink[] {
  const connector = stringOf(s.connector);
  if (connector === undefined) return [];
  return [{
    connector,
    ...(numberOf(s.consumed) !== undefined ? { consumed: numberOf(s.consumed) } : {}),
    ...(numberOf(s.lag) !== undefined ? { lag: numberOf(s.lag) } : {}),
    ...(Array.isArray(s.topics) ? { topics: stringsOf(s.topics) } : {}),
  }];
}

/** What the success toast says about a finished pipeline operation: its result's `status` and `runtime.topic`, when present and text. */
export function operationResultSummary(result: unknown): { status?: string; topic?: string } {
  if (!isRecord(result)) return {};
  const runtime = isRecord(result.runtime) ? result.runtime : {};
  return { status: stringOf(result.status), topic: stringOf(runtime.topic) };
}
