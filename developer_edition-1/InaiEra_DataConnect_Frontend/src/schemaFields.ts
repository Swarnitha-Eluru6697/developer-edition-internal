/*
 * FE-GAP-02: a schema's field list. The backend stores `schema_definition` as free-form JSON, and the Register-schema form accepts
 * fields as pasted JSON. Both are checked here and converted to the generated `SchemaField` (mirroring the backend's SchemaField,
 * app/schemas/schema.py: `name` and `type` required, the rest optional). A field that is not a usable object is dropped when READING
 * stored data (so one bad row never blanks the drawer) and rejected, with where, when VALIDATING what a user typed.
 */
import { booleanOf, isRecord, numberOf, stringOf } from "./json";
import type { SchemaField } from "./types/api";

function toField(entry: unknown): SchemaField | null {
  if (!isRecord(entry)) return null;
  const name = stringOf(entry.name);
  const type = stringOf(entry.type);
  if (!name || !type) return null;
  const field: SchemaField = { name, type };
  const nullable = booleanOf(entry.nullable);
  if (nullable !== undefined) field.nullable = nullable;
  const primaryKey = booleanOf(entry.primary_key);
  if (primaryKey !== undefined) field.primary_key = primaryKey;
  const nullOnly = booleanOf(entry.null_only);
  if (nullOnly !== undefined) field.null_only = nullOnly;
  const confidence = numberOf(entry.confidence);
  if (confidence !== undefined) field.confidence = confidence;
  const precision = numberOf(entry.precision);
  if (precision !== undefined) field.precision = precision;
  const scale = numberOf(entry.scale);
  if (scale !== undefined) field.scale = scale;
  if (Array.isArray(entry.fields)) field.fields = entry.fields.map(toField).filter((f): f is SchemaField => f !== null);
  const items = toField(entry.items);
  if (items) field.items = items;
  return field;
}

/** The fields of a stored `schema_definition` (`{ fields: [...] }`), for display. Anything unusable is dropped; never throws. */
export function schemaFieldsOf(definition: unknown): SchemaField[] {
  if (!isRecord(definition) || !Array.isArray(definition.fields)) return [];
  return definition.fields.map(toField).filter((f): f is SchemaField => f !== null);
}

export type ParsedFields = { ok: true; fields: SchemaField[] } | { ok: false; message: string };

/** The fields a user pasted into the Register-schema form, validated for submission. */
export function parseSchemaFieldsInput(value: unknown): ParsedFields {
  if (!Array.isArray(value)) return { ok: false, message: "Fields must be a JSON array." };
  const fields: SchemaField[] = [];
  for (const [index, entry] of value.entries()) {
    const field = toField(entry);
    if (!field) return { ok: false, message: `Field ${index + 1} must be an object with a string "name" and "type".` };
    fields.push(field);
  }
  return { ok: true, fields };
}
