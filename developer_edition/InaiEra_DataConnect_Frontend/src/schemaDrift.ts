/*
 * FE-GAP-02: a schema drift event. GET /schemas/{id}/drift-events is passthrough JSON; the backend builds each event in
 * app/services/schema_drift.py (compare_schema_fields) as {field, drift_type, action, details: {approved?, observed?}}. The drift dialog
 * reads exactly those, so each is checked here and an event of the wrong shape becomes an "unrecognized change" instead of a crash.
 */
import { booleanOf, isRecord, stringOf } from "./json";

export interface DriftField { type?: string; nullable?: boolean; primary_key?: boolean }
export interface DriftEventView { field?: string; drift_type?: string; approved?: DriftField; observed?: DriftField }

function driftFieldOf(value: unknown): DriftField | undefined {
  if (!isRecord(value)) return undefined;
  return { type: stringOf(value.type), nullable: booleanOf(value.nullable), primary_key: booleanOf(value.primary_key) };
}

export function driftEventOf(value: unknown): DriftEventView {
  if (!isRecord(value)) return {};
  const details = isRecord(value.details) ? value.details : {};
  return {
    field: stringOf(value.field),
    drift_type: stringOf(value.drift_type),
    approved: driftFieldOf(details.approved),
    observed: driftFieldOf(details.observed),
  };
}
