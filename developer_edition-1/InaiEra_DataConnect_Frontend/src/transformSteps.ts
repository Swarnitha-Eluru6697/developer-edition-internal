/*
 * FE-GAP-02: SMT steps typed into the "New SMT" form are free text. This validates that JSON and converts it to the
 * generated `TransformStep[]` the API expects — mirroring the backend's TransformStep (app/schemas/transform.py): `order` int,
 * `name` and `type` required, `scope` defaults to "value", `config` an object. A bad shape is reported (with where), never cast.
 */
import { isRecord, numberOf, stringOf, type JsonRecord } from "./json";
import type { TransformStep } from "./types/api";

export type ParsedSteps = { ok: true; steps: TransformStep[] } | { ok: false; message: string };

function toStep(entry: unknown, index: number): { step: TransformStep } | { message: string } {
  const where = `Step ${index + 1}`;
  if (!isRecord(entry)) return { message: `${where} must be an object.` };
  const name = stringOf(entry.name)?.trim();
  if (!name) return { message: `${where} needs a "name".` };
  const type = stringOf(entry.type)?.trim();
  if (!type) return { message: `${where} ("${name}") needs a "type".` };
  const rawConfig = entry.config;
  if (rawConfig !== undefined && !isRecord(rawConfig)) return { message: `${where} ("${name}"): "config" must be an object.` };
  const rawScope = entry.scope;
  if (rawScope !== undefined && typeof rawScope !== "string") return { message: `${where} ("${name}"): "scope" must be a string.` };
  const rawOrder = entry.order;
  if (rawOrder !== undefined && numberOf(rawOrder) === undefined) return { message: `${where} ("${name}"): "order" must be a number.` };
  const config: JsonRecord = rawConfig ?? {};
  return { step: { order: numberOf(rawOrder) ?? index + 1, name, type, scope: rawScope ?? "value", config } };
}

export function parseTransformSteps(value: unknown): ParsedSteps {
  if (!Array.isArray(value)) return { ok: false, message: "SMT steps must be a JSON array." };
  const steps: TransformStep[] = [];
  for (const [index, entry] of value.entries()) {
    const result = toStep(entry, index);
    if ("message" in result) return { ok: false, message: result.message };
    steps.push(result.step);
  }
  return { ok: true, steps };
}
