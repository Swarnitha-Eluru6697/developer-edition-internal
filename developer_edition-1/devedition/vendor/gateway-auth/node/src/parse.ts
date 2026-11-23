/**
 * Parse a list header (permissions, roles, groups) in any of the forms Envoy or
 * a client may send (contract section 4):
 *   - base64(JSON array): Envoy's jwt_authn `claim_to_headers` rendering of a
 *     multi-valued claim, e.g. `WyJyZWFkZXIiXQ==` -> ["reader"]
 *   - a raw JSON array string
 *   - a plain comma list
 * Unknown/invalid input = empty list (base64 of JSON that is not an array counts
 * as invalid). Identical to the Python package's split_multi.
 */
export function splitMulti(value: string | null | undefined): string[] {
  const v = (value || "").trim();
  if (!v) return [];
  const asList = (x: unknown): string[] | null =>
    Array.isArray(x) ? x.map((e) => String(e).trim()).filter(Boolean) : null;
  // base64(JSON) — only if it really is base64 that decodes to JSON
  if (/^[A-Za-z0-9+/_-]+={0,2}$/.test(v)) {
    try {
      const decoded = Buffer.from(v, "base64").toString("utf8").trim();
      const parsed = JSON.parse(decoded);
      return asList(parsed) ?? [];
    } catch { /* not base64 JSON */ }
  }
  // raw JSON array
  if (v.startsWith("[")) {
    try {
      const arr = asList(JSON.parse(v));
      if (arr) return arr;
    } catch { /* fall through to comma list */ }
  }
  // plain comma list
  return v.split(/[\s,[\]"']+/).map((t) => t.trim()).filter(Boolean);
}
