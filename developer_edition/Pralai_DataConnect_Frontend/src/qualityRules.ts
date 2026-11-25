/*
 * FE-GAP-02: quality rules, typed.
 *
 * A policy's `rules` are free-form JSON objects on the wire (the generated type is `Record<string, unknown>[]`). Two directions cross
 * that boundary here, and both are typed:
 *   - OUT: the rule builder form -> `buildRulePayload`, driven by the typed QRULE_SPEC table in constants.ts.
 *   - IN:  a stored rule object -> `parseQualityRule`, a validated `QualityRule` view model the screens render. A field of the wrong
 *          type is simply absent from the view model; nothing is cast.
 * Pure (no React) so tests/unit/quality-rules.spec.ts can exercise it directly.
 */
import { QRULE_SPEC, type QRuleSpec, type QualityRuleType } from "./constants";
import { numberOf, recordsOf, stringOf, stringsOf, type JsonRecord } from "./json";

export function isQualityRuleType(type: string): type is QualityRuleType {
  return Object.prototype.hasOwnProperty.call(QRULE_SPEC, type);
}

/** The form spec for a rule type; a type the form does not offer has no extra parameters. */
export function ruleSpecFor(type: string): QRuleSpec {
  return isQualityRuleType(type) ? QRULE_SPEC[type] : { label: type, params: [] };
}

export type RuleParamValue = string | number | string[];

/** The rule object the API stores for one form row: name, type, field, plus whichever parameters were filled in. */
export function buildRulePayload(type: string, field: string, params: Record<string, string>): Record<string, RuleParamValue> {
  const rule: Record<string, RuleParamValue> = { name: `${field}_${type}`, type, field };
  for (const p of ruleSpecFor(type).params) {
    const v = (params[p.k] ?? "").trim();
    if (v === "") continue;
    if (p.k === "values") rule.values = v.split(",").map((s) => s.trim()).filter(Boolean);
    else if (p.k === "min" || p.k === "max") rule[p.k] = Number(v);
    else rule[p.k] = v;
  }
  return rule;
}

/** A stored rule as the screens need it. Anything not of the expected type is left out (never coerced). */
export interface QualityRule {
  name: string;
  type: string;
  field?: string;
  expectedType?: string;
  pattern?: string;
  values: string[];
  min?: number;
  max?: number;
  /** Every key other than name/type/field, as stored — for display. */
  extra: JsonRecord;
}

export function parseQualityRule(raw: JsonRecord): QualityRule {
  const { name: _name, type: _type, field: _field, ...extra } = raw;
  return {
    name: stringOf(raw.name) ?? "",
    type: stringOf(raw.type) ?? "",
    field: stringOf(raw.field),
    expectedType: stringOf(raw.expected_type),
    pattern: stringOf(raw.pattern),
    values: stringsOf(raw.values),
    min: numberOf(raw.min),
    max: numberOf(raw.max),
    extra,
  };
}

export function parseQualityRules(raw: unknown): QualityRule[] {
  return recordsOf(raw).map(parseQualityRule);
}

/** Human-readable statement of what a rule required, from its own stored parameters — the same fields the builder form wrote. */
export function describeRuleExpectation(rule: QualityRule | undefined): string {
  if (!rule) return "rule definition not found (policy may have changed since)";
  switch (rule.type) {
    case "required": return "must be present (not null / empty)";
    case "type_check": return `must be of type "${rule.expectedType ?? ""}"`;
    case "regex": return `must match pattern /${rule.pattern ?? ""}/`;
    case "allowed_values": return `must be one of: ${rule.values.join(", ")}`;
    case "min_max_numeric": return `must be between ${rule.min ?? "−∞"} and ${rule.max ?? "∞"}`;
    case "min_max_string_length": return `length must be between ${rule.min ?? 0} and ${rule.max ?? "∞"}`;
    default: return `must satisfy ${rule.type}`;
  }
}

/** The distinct, non-empty field names a set of rules checks — the columns of the "execute" sample grid. */
export function ruleFields(rules: QualityRule[]): string[] {
  return [...new Set(rules.map((r) => r.field).filter((f): f is string => !!f))];
}

/** Sample-grid cells are typed in as text; "true"/"false" and numbers become booleans and numbers, anything else stays text. */
export function typedSampleValue(text: string): string | number | boolean {
  if (text === "true") return true;
  if (text === "false") return false;
  return Number.isNaN(Number(text)) ? text : Number(text);
}
