/*
 * Domain constants ported verbatim from the vanilla DataConnect app — the
 * Quality rule builder and the badge/colour maps, which have no backend
 * equivalent, so they live here as the single source. The Connection
 * form's own field schema used to live here too (per-source-type hardcoded
 * branches); see pages/Connections.tsx's own comment for why that moved to
 * the backend (app/services/connection_config_schema.py) instead.
 */

// FE-BUG-25: deploying/pausing/resuming were missing entirely — with
// active_operation_id now surviving a page refresh, these transitional
// statuses are visible for longer (and more often) than before, so the
// badge falling back to plain gray for them is worth closing here too.
export const PIPE_BADGE = {
  draft: "bgr", deployed: "bb", running: "bg", paused: "ba", restarting: "ba", failed: "br",
  deploying: "ba", pausing: "ba", resuming: "ba", deleting: "ba",
};
export const TYPE_TAG = { string: "ts", integer: "ti", number: "ti", timestamp: "tt", boolean: "tb2", decimal: "tb2", object: "tt", array: "tn" };
export const ASSET_BADGE = { source: "bb", connector: "bp", kafka_topic: "bg", processor: "ba", sink: "bgr" };
export const ZONES = ["raw", "prepared", "curated", "quarantine", "meta"];
export const MODES = ["cdc", "snapshot", "batch", "api", "stream", "validation", "replay"];
export const SMT_TYPES = ["ValueToKey", "ExtractField", "HoistField", "Flatten", "RegexRouter", "HeaderFrom", "MaskField", "ReplaceField", "InsertField", "TimestampConverter"];
export const RULE_TYPES = ["required", "type_check", "regex", "allowed_values", "min_max_numeric", "min_max_string_length", "timestamp_freshness", "null_percentage_threshold", "duplicate_key_batch", "referential_hint"];

// The rule checks the user can build in the form (the ones the engine actually
// evaluates), with the extra parameters each one needs.
export type QualityRuleType = "required" | "regex" | "allowed_values" | "min_max_numeric" | "min_max_string_length" | "type_check";

/** One extra parameter a rule check takes. A `select` carries its choices; every other kind carries a placeholder. */
export type QRuleParam =
  | { k: string; label: string; t: "text" | "list" | "num"; ph: string }
  | { k: string; label: string; t: "select"; opts: string[] };

export interface QRuleSpec { label: string; params: QRuleParam[] }

export const QRULE_SPEC: Record<QualityRuleType, QRuleSpec> = {
  required: { label: "Field is present (not null / empty)", params: [] },
  regex: { label: "Matches a regex pattern", params: [{ k: "pattern", label: "Regex pattern", t: "text", ph: ".+@.+\\..+" }] },
  allowed_values: { label: "Value is one of an allowed set", params: [{ k: "values", label: "Allowed values (comma-separated)", t: "list", ph: "ACTIVE, INACTIVE" }] },
  min_max_numeric: { label: "Number within a range", params: [{ k: "min", label: "Min", t: "num", ph: "0" }, { k: "max", label: "Max", t: "num", ph: "100000" }] },
  min_max_string_length: { label: "Text length within a range", params: [{ k: "min", label: "Min length", t: "num", ph: "1" }, { k: "max", label: "Max length", t: "num", ph: "50" }] },
  type_check: { label: "Field is of a given type", params: [{ k: "expected_type", label: "Expected type", t: "select", opts: ["string", "integer", "number", "boolean", "object", "array"] }] },
};

// FE-BUG-10: the per-source-type connection field schema and list-table
// column choices used to live here as hardcoded per-type branches
// (connSchema/connCols) — moved to the backend's own field metadata (app/
// services/connection_config_schema.py, fetched via
// useListConnectionConfigSchemasQuery) and a generic derivation
// (columnsFromFields in pages/Connections.tsx) instead. Adding a new
// connector's connection fields is now a backend data change, never a new
// React form or a new branch in this file.
