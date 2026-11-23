/*
 * FE-GAP-02: the sample failures a quality worker keeps are the backend's quarantine envelopes, declared as free-form JSON on the wire
 * (`{ [key: string]: unknown }[]`). The "Failed records" drawer reads a handful of their fields, so exactly those are checked here and
 * nothing else is claimed: an entry that is not an object, or has no topic or arrival time to show, is dropped; a missing optional field
 * gets a neutral value instead of rendering `undefined`.
 */
import { isRecord, numberOf, stringOf, stringsOf } from "./json";
import type { QualityFailureEnvelope } from "./types/api";

export function qualityFailuresOf(value: unknown): QualityFailureEnvelope[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): QualityFailureEnvelope[] => {
    if (!isRecord(entry)) return [];
    const originalTopic = stringOf(entry.original_topic);
    const receivedAt = stringOf(entry.received_at);
    if (originalTopic === undefined || receivedAt === undefined) return [];
    const partition = numberOf(entry.original_partition);
    const offset = numberOf(entry.original_offset);
    return [{
      original_topic: originalTopic,
      ...(partition !== undefined ? { original_partition: partition } : {}),
      ...(offset !== undefined ? { original_offset: offset } : {}),
      pipeline_id: stringOf(entry.pipeline_id) ?? "",
      schema_id: stringOf(entry.schema_id) ?? null,
      quality_policy_id: stringOf(entry.quality_policy_id) ?? null,
      failure_type: stringOf(entry.failure_type) ?? "unknown",
      failed_rules: stringsOf(entry.failed_rules),
      received_at: receivedAt,
      original_record: isRecord(entry.original_record) ? entry.original_record : {},
    }];
  });
}
