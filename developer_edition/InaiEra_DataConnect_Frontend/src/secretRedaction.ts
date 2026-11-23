/*
 * FE-BUG-09: the backend already redacts a generated Kafka Connect
 * connector config before returning it (see app/services/secrets.py's
 * redact_connector_config, an exact-key allowlist plus a credential-URI
 * scrub) — that stays authoritative. This is the frontend's OWN, INDEPENDENT
 * layer, deliberately broader and fuzzier (regex on key name, not an exact
 * list) than the backend's: defense-in-depth against a field the backend's
 * allowlist doesn't yet know about, a new connector type introducing a
 * differently-named secret, or any other arbitrary backend payload
 * (a transform preview's generated_config, a schema-infer result, ...)
 * that was never routed through redact_connector_config at all.
 *
 * Matches by KEY name, never by scanning stringified content — the same
 * shape as the backend's own approach, and the only way to redact reliably
 * without also mangling ordinary prose that happens to contain a word like
 * "token".
 */
export const SECRET_PATTERNS: RegExp[] = [
  /password/i,
  /secret/i,
  /token/i,
  /access.?key/i,
  /private.?key/i,
  /credential/i,
];

export const REDACTED_PLACEHOLDER = "[REDACTED]";

export function isSecretKey(key: string): boolean {
  return SECRET_PATTERNS.some((pattern) => pattern.test(key));
}

/** Deep-walks `value` (objects/arrays), replacing the value at any key whose
 * name matches SECRET_PATTERNS with REDACTED_PLACEHOLDER. Never throws —
 * this is itself a safety net — but fails CLOSED: an input this can't walk
 * safely (e.g. a circular structure blowing the call stack) degrades to a
 * fully redacted placeholder, never back to the original, un-redacted
 * value — a redaction layer that fails open on error isn't one. */
export function redactSecrets(value: unknown): unknown {
  try {
    if (Array.isArray(value)) return value.map(redactSecrets);
    if (value && typeof value === "object") {
      const out: Record<string, unknown> = {};
      for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
        out[key] = isSecretKey(key) ? REDACTED_PLACEHOLDER : redactSecrets(v);
      }
      return out;
    }
    return value;
  } catch {
    return REDACTED_PLACEHOLDER;
  }
}

/** Redacts a payload that arrived as a JSON *string* (e.g. a generated config
 * serialized by the backend). Parses it, redacts by key like redactSecrets, and
 * re-serializes. Anything that isn't JSON (plain prose, a log line) is returned
 * untouched — there are no keys to match on, and scanning free text would mangle
 * it. A string that looks like JSON but won't parse is NOT shown raw: it could
 * still hold a secret-shaped key=value we can't safely walk, so it fails closed. */
export function redactJsonString(text: string): string {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return text;
  try {
    return JSON.stringify(redactSecrets(JSON.parse(text)), null, 2);
  } catch {
    return REDACTED_PLACEHOLDER;
  }
}

/** Compact one-line JSON of `value` with secret-shaped keys redacted — for inline
 * config summaries that would otherwise call JSON.stringify on a raw payload. */
export function redactedInlineJson(value: unknown): string {
  return JSON.stringify(redactSecrets(value));
}
