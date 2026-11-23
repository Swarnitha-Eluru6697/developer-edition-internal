import { formatCreatedAt } from "./dates";
/*
 * FE-GAP-02: a connection's free-form `connection_config`, typed at the edges.
 *
 * The generated `Connection` types `connection_config` as `Record<string, unknown>` — it varies by source type (the backend serves
 * one field schema per type, ConfigFieldSchema) — so it stays `unknown`-valued. Everything the Connections screen does with it goes
 * through here: reading a value for display, turning the form's text boxes back into a typed config object, and reading the one
 * `details.latency_ms` a probe may report. No `any`, no casts: each conversion checks what it has.
 */
import { isRecord, numberOf, parseJson, type JsonRecord } from "./json";
import { isSecretKey, REDACTED_PLACEHOLDER } from "./secretRedaction";
import type { ConfigFieldSchema, Connection } from "./types/api";

/** The backend's own placeholder for "a secret is set here, but this response never carries its real value" — see
 * app/services/secrets.py's MASK / _connection_to_dict. Used ONLY to derive "is something set", never as editable content (FE-BUG-11). */
export const SECRET_MASK = "********";

export function configOf(connection: Pick<Connection, "connection_config">): JsonRecord {
  return connection.connection_config ?? {};
}

/** A config value as display text: lists joined, null as "", anything else stringified. */
export function configText(value: unknown, listSeparator = ", "): string {
  if (Array.isArray(value)) return value.join(listSeparator);
  return value === null || value === undefined ? "" : String(value);
}

export function isSecretConfigured(connection: Pick<Connection, "connection_config"> | undefined, key: string): boolean {
  return connection !== undefined && configOf(connection)[key] === SECRET_MASK;
}

/** The text a form field starts with: the existing value when editing (never a secret), else the schema's default. */
export function initialFieldValue(field: ConfigFieldSchema, existing: Connection | undefined): string {
  if (existing) {
    if (field.type === "secret") return ""; // never pre-fill the mask as editable content
    return configText(configOf(existing)[field.key], ",");
  }
  return field.default !== null && field.default !== undefined ? String(field.default) : "";
}

export type ConfigBuild = { cfg: Record<string, unknown> } | { error: string; field: string };

/**
 * The typed config object for the form's text boxes, or the first problem found (with the field it belongs to).
 * FE-BUG-11: an untouched secret field on an EXISTING connection is left out entirely — not the mask, not an empty string — so the
 * backend's required-field check only ever sees a fresh create or a field the user genuinely typed into.
 */
export function buildConnectionConfig(
  fields: ConfigFieldSchema[], values: Record<string, string>, options: { existing: boolean; secretChanged: Record<string, boolean> },
): ConfigBuild {
  const cfg: Record<string, unknown> = {};
  for (const f of fields) {
    if (f.type === "secret" && options.existing && !options.secretChanged[f.key]) continue;
    const text = (values[f.key] ?? "").trim();
    if (text === "") {
      if (f.required) return { error: `${f.label} is required`, field: f.key };
      continue;
    }
    if (f.type === "number") cfg[f.key] = Number(text);
    else if (f.type === "boolean") cfg[f.key] = text === "true";
    else if (f.type === "list") cfg[f.key] = text.split(",").map((x) => x.trim()).filter(Boolean);
    else if (f.type === "json") {
      const parsed = parseJson(text);
      if (!parsed.ok) return { error: `${f.label} must be valid JSON`, field: f.key };
      cfg[f.key] = parsed.value;
    } else cfg[f.key] = text;
  }
  return { cfg };
}

/**
 * Whether saving this edit changes how the connection reaches its source — a secret re-entered, the secret reference changed, or any
 * setting (host, port, database, user, tables...) different from what is saved. Only then is it worth trying the connection before saving;
 * renaming a connection does not need a test. `cfg` is what the form would save (it leaves an untouched secret out).
 */
export function changesHowItConnects(
  existing: Connection, cfg: Record<string, unknown>, secretChanged: Record<string, boolean>, secretRef: string,
): boolean {
  if (Object.values(secretChanged).some(Boolean)) return true;
  if (secretRef.trim() !== (existing.secret_ref ?? "")) return true;
  const was = configOf(existing);
  if (Object.entries(cfg).some(([key, value]) => JSON.stringify(value) !== JSON.stringify(was[key]))) return true;
  // a setting that was saved and has been cleared now
  return Object.entries(was).some(([key, value]) => !isSecretKey(key) && !(key in cfg) && value !== null && value !== undefined && value !== "");
}

/** " · 12ms" when a probe's `details` carries a numeric `latency_ms`, else "". */
export function latencySuffix(details: Record<string, unknown> | undefined): string {
  const ms = isRecord(details) ? numberOf(details.latency_ms) : undefined;
  return ms === undefined ? "" : ` · ${ms}ms`;
}

export interface ConnColumn {
  label: string;
  get: (connection: Connection) => string;
}

/** Which columns to show in the connections list table for one source type — derived from its own field schema (up to 2 "notable"
 * fields) rather than a hardcoded per-type branch, so a new source type needs no frontend code. Name/Status wrap the middle columns. */
export function columnsFromFields(fields: ConfigFieldSchema[]): ConnColumn[] {
  const raw = (key: string) => (c: Connection): string => {
    const v = configOf(c)[key];
    if (isSecretKey(key) && v !== null && v !== undefined && v !== "") return REDACTED_PLACEHOLDER;
    return configText(v);
  };
  const host = (c: Connection): string => {
    const cfg = configOf(c);
    return cfg.hostname ? String(cfg.hostname) + (cfg.port ? ":" + String(cfg.port) : "") : "";
  };
  const mid: ConnColumn[] = [];
  if (fields.some((f) => f.key === "hostname")) mid.push({ label: "Host", get: host });
  const notable = fields.filter((f) => f.key !== "hostname" && f.key !== "port" && f.type !== "secret");
  const required = notable.filter((f) => f.required);
  for (const f of (required.length ? required : notable)) {
    if (mid.length >= 2) break;
    mid.push({ label: f.label, get: raw(f.key) });
  }
  return [{ label: "Friendly Name", get: (c) => c.name }, { label: "Created at", get: (c) => formatCreatedAt(c.created_at) }, ...mid, { label: "Status", get: (c) => c.status ?? "" }];
}
