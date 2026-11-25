/*
 * FE-GAP-02: the ONE place the UI inspects dynamic JSON.
 *
 * The backend declares a handful of values as free-form JSON (a connection's `connection_config`, a template's
 * `supported_transforms`, a policy's `rules`, a schema's `schema_definition`, a transform step's `config`, text a user pastes). In the
 * generated types those are `unknown` / `Record<string, unknown>`, and they stay that way at the boundary. They are NEVER cast to a
 * convenient shape: each is checked here (or by an adapter built on these helpers — templateTransforms.ts, qualityRules.ts,
 * schemaFields.ts, transformSteps.ts) and only then converted to a typed view model the screens use.
 */

export type JsonRecord = Record<string, unknown>;

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The array's object entries, silently dropping anything that is not an object; [] if it is not an array. */
export function recordsOf(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function stringOf(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function numberOf(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function booleanOf(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function stringsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Text a user typed or pasted, parsed as JSON. `{ ok: false }` (never a throw, never `any`) when it is not valid JSON. */
export function parseJson(text: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false };
  }
}

/** Text a user pasted that must be a JSON ARRAY OF OBJECTS (sample records for schema inference / drift detection). */
export function parseJsonRecords(text: string): { ok: true; records: JsonRecord[] } | { ok: false; message: string } {
  const parsed = parseJson(text);
  if (!parsed.ok) return { ok: false, message: "Records must be valid JSON" };
  if (!Array.isArray(parsed.value) || !parsed.value.every(isRecord)) {
    return { ok: false, message: "Records must be a JSON array of objects" };
  }
  return { ok: true, records: parsed.value };
}
