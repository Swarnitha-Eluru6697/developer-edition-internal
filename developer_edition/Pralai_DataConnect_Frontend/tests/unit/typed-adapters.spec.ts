/*
 * FE-GAP-02: the typed adapters that replaced `any` at the dynamic-JSON boundaries. Each one validates `unknown` into a typed view model,
 * so these tests are about what happens with BAD input (dropped, rejected with a message, never cast through) as much as good input.
 */
import { test, expect } from "@playwright/test";
import { ApiError } from "../../src/api";
import { QRULE_SPEC } from "../../src/constants";
import { abridgeMetricsResponse } from "../../src/connectorMetrics";
import {
  SECRET_MASK, buildConnectionConfig, columnsFromFields, configText, initialFieldValue, isSecretConfigured, latencySuffix,
} from "../../src/connectionConfig";
import { apiErrorShape, errorMessage, firstFieldError } from "../../src/errors";
import { isRecord, parseJson, parseJsonRecords, recordsOf, stringsOf } from "../../src/json";
import { conflictFromDetails, runtimeFlowFromStorage } from "../../src/pipelineAdapters";
import {
  buildRulePayload, describeRuleExpectation, isQualityRuleType, parseQualityRule, parseQualityRules, ruleFields, ruleSpecFor, typedSampleValue,
} from "../../src/qualityRules";
import { parseSchemaFieldsInput, schemaFieldsOf } from "../../src/schemaFields";
import { isParamRequired, missingTransformParams, modesForTemplate, parseTransformSpecs, templateCapabilities, transformsForTemplate } from "../../src/templateAdapters";
import { parseTransformSteps } from "../../src/transformSteps";
import type { ConfigFieldSchema, Connection, ConnectorMetrics, ConnectorTemplate } from "../../src/types/api";

const asTemplate = (overrides: Record<string, unknown>) => ({ capabilities: {}, ...overrides }) as unknown as ConnectorTemplate;
const field = (overrides: Partial<ConfigFieldSchema> & { key: string }): ConfigFieldSchema => ({ label: overrides.key, type: "string", ...overrides });
const connection = (config: Record<string, unknown>, extra: Partial<Connection> = {}): Connection =>
  ({ id: "c1", org_id: "o", name: "conn", source_type: "postgres", connection_config: config, status: "active", ...extra }) as Connection;

test.describe("json.ts", () => {
  test("isRecord is true only for plain objects", () => {
    expect([{}, { a: 1 }].every(isRecord)).toBe(true);
    expect([null, undefined, [], "x", 1, true].some(isRecord)).toBe(false);
  });
  test("recordsOf / stringsOf drop what is not the expected kind and never throw", () => {
    expect(recordsOf([{ a: 1 }, null, "x", [], { b: 2 }])).toEqual([{ a: 1 }, { b: 2 }]);
    expect(recordsOf("nope")).toEqual([]);
    expect(stringsOf(["a", 1, null, "b"])).toEqual(["a", "b"]);
    expect(stringsOf(undefined)).toEqual([]);
  });
  test("parseJson reports failure instead of throwing", () => {
    expect(parseJson('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseJson("{oops")).toEqual({ ok: false });
  });
  test("parseJsonRecords accepts only an array of objects", () => {
    expect(parseJsonRecords('[{"a":1},{"b":2}]')).toEqual({ ok: true, records: [{ a: 1 }, { b: 2 }] });
    expect(parseJsonRecords("[]")).toEqual({ ok: true, records: [] });
    expect(parseJsonRecords("{oops")).toEqual({ ok: false, message: "Records must be valid JSON" });
    for (const bad of ['{"a":1}', "[1,2]", '[{"a":1}, null]', '"x"', "null"]) {
      expect(parseJsonRecords(bad), bad).toEqual({ ok: false, message: "Records must be a JSON array of objects" });
    }
  });
});

test.describe("errors.ts", () => {
  test("errorMessage reads an Error, a string, or any object with a string message, and never throws", () => {
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage(new ApiError(422, "bad input"))).toBe("bad input");
    expect(errorMessage("plain")).toBe("plain");
    expect(errorMessage({ message: "from rtk" })).toBe("from rtk");
    expect(errorMessage({ message: 5 })).toBe("[object Object]");
    expect(errorMessage(undefined)).toBe("undefined");
    expect(errorMessage(null)).toBe("null");
  });
  test("firstFieldError reads an ApiError AND the plain object the store rejects with (the store never throws an ApiError instance)", () => {
    const fieldErrors = [{ field: "name", message: "required" }, { field: "b", message: "c" }];
    expect(firstFieldError(new ApiError(422, "x", { fieldErrors }))).toEqual({ field: "name", message: "required" });
    expect(firstFieldError({ status: 422, message: "x", retryable: false, fieldErrors })).toEqual({ field: "name", message: "required" });   // toQueryError's shape
    expect(firstFieldError(new ApiError(500, "x"))).toBeUndefined();
    expect(firstFieldError(new Error("x"))).toBeUndefined();
    expect(firstFieldError({ fieldErrors })).toBeUndefined();                       // no status/message: not an API error
    expect(firstFieldError({ status: 422, message: "x", fieldErrors: [{ field: 1 }, null, { field: "ok", message: "m" }] })).toEqual({ field: "ok", message: "m" });
    expect(firstFieldError(undefined)).toBeUndefined();
  });
  test("apiErrorShape: status and message are required; the rest is validated, never trusted", () => {
    expect(apiErrorShape({ status: 409, message: "m", code: "C", requestId: "r", retryable: true, details: { current: { name: "p" } } }))
      .toEqual({ status: 409, message: "m", code: "C", fieldErrors: undefined, requestId: "r", retryable: true, details: { current: { name: "p" } } });
    expect(apiErrorShape({ status: 0, message: "m" })).toMatchObject({ status: 0, retryable: false, code: undefined, details: undefined });
    expect(apiErrorShape({ status: 400, message: "m", code: 5, details: [1], retryable: "yes" })).toMatchObject({ code: undefined, details: undefined, retryable: false });
    expect(apiErrorShape(new ApiError(404, "gone", { code: "NOT_FOUND" }))).toMatchObject({ status: 404, code: "NOT_FOUND" });
    for (const bad of [null, undefined, "x", 7, {}, { status: "404", message: "m" }, { status: 404 }, { message: "m" }]) expect(apiErrorShape(bad), JSON.stringify(bad)).toBeUndefined();
  });
});

test.describe("templateAdapters.ts", () => {
  const seedLike = [
    { type: "ValueToKey", description: "Copy fields into the key", params: [{ key: "fields", label: "Field(s)", type: "list", required: true, placeholder: "id" }] },
    { type: "Flatten", description: "Flatten", params: [{ key: "delimiter", label: "Delimiter", type: "text", default: ".", placeholder: "." }] },
    { type: "Pick", params: [{ key: "mode", type: "select", options: ["a", "b", 3] }] },
  ];

  test("a seed-shaped catalogue becomes typed specs with defaults filled in", () => {
    const specs = parseTransformSpecs(seedLike);
    expect(specs.map((s) => s.type)).toEqual(["ValueToKey", "Flatten", "Pick"]);
    expect(specs[0]?.params[0]).toEqual({ key: "fields", label: "Field(s)", type: "list", required: true, defaultValue: "", placeholder: "id", options: [] });
    expect(specs[1]?.params[0]?.defaultValue).toBe(".");
    expect(specs[2]?.description).toBe("");
    expect(specs[2]?.params[0]).toMatchObject({ label: "mode", type: "select", required: false, options: ["a", "b"] });   // label falls back to key; non-string option dropped
  });

  test("a malformed catalogue never breaks the form: bad entries and params are dropped", () => {
    expect(parseTransformSpecs([{ type: "" }, { description: "no type" }, null, "x", 5, { type: "Ok", params: [{ label: "no key" }, null, { key: "k" }] }]))
      .toEqual([{ type: "Ok", description: "", params: [{ key: "k", label: "k", type: "text", required: false, defaultValue: "", placeholder: "", options: [] }] }]);
    expect(parseTransformSpecs(undefined)).toEqual([]);
    expect(parseTransformSpecs({ type: "NotAnArray" })).toEqual([]);
  });

  test("a numeric or boolean default becomes text, a null one becomes empty", () => {
    const [spec] = parseTransformSpecs([{ type: "T", params: [{ key: "a", default: 5 }, { key: "b", default: false }, { key: "c", default: null }] }]);
    expect(spec?.params.map((p) => p.defaultValue)).toEqual(["5", "false", ""]);
  });

  test("capabilities: undeclared means supported; only an explicit false turns something off", () => {
    expect(templateCapabilities(undefined)).toEqual({ cdc: true, snapshot: true, schemaDiscovery: true, supportsSMT: true, supportsDLQ: true });
    expect(templateCapabilities(asTemplate({ capabilities: { cdc: false, supportsSMT: false } }))).toMatchObject({ cdc: false, supportsSMT: false, snapshot: true });
  });

  test("a template with supportsSMT:false offers no transformations whatever its list holds", () => {
    expect(transformsForTemplate(asTemplate({ capabilities: { supportsSMT: false }, supported_transforms: seedLike }))).toEqual([]);
    expect(transformsForTemplate(asTemplate({ supported_transforms: seedLike }))).toHaveLength(3);
  });

  test("modes: empty or undeclared supported_modes is unrestricted; capability flags still remove cdc/snapshot", () => {
    const all = modesForTemplate(undefined);
    expect(all).toContain("cdc");
    expect(modesForTemplate(asTemplate({ supported_modes: ["batch", "api", "bogus"] }))).toEqual(["api", "batch"].sort((a, b) => all.indexOf(a) - all.indexOf(b)));
    expect(modesForTemplate(asTemplate({ capabilities: { cdc: false, snapshot: false } }))).not.toContain("cdc");
    expect(modesForTemplate(asTemplate({ capabilities: { cdc: false } }))).not.toContain("cdc");
    expect(modesForTemplate(asTemplate({ supported_modes: ["cdc", "batch"], capabilities: { cdc: false } }))).toEqual(["batch"]);
  });
});

test.describe("transformSteps.ts", () => {
  test("valid steps convert to TransformStep with defaults for order, scope and config", () => {
    expect(parseTransformSteps([{ name: "mask", type: "MaskField", config: { fields: "email" } }, { order: 7, name: "k", type: "ValueToKey", scope: "key" }])).toEqual({
      ok: true,
      steps: [
        { order: 1, name: "mask", type: "MaskField", scope: "value", config: { fields: "email" } },
        { order: 7, name: "k", type: "ValueToKey", scope: "key", config: {} },
      ],
    });
  });
  test("every kind of bad input is rejected with where it went wrong, never cast through", () => {
    const bad = (value: unknown) => { const r = parseTransformSteps(value); return r.ok ? "OK" : r.message; };
    expect(bad({ name: "x" })).toBe("SMT steps must be a JSON array.");
    expect(bad(["x"])).toBe("Step 1 must be an object.");
    expect(bad([{ type: "T" }])).toBe('Step 1 needs a "name".');
    expect(bad([{ name: "n" }])).toBe('Step 1 ("n") needs a "type".');
    expect(bad([{ name: "n", type: "T", config: [] }])).toBe('Step 1 ("n"): "config" must be an object.');
    expect(bad([{ name: "n", type: "T", scope: 3 }])).toBe('Step 1 ("n"): "scope" must be a string.');
    expect(bad([{ name: "n", type: "T", order: "1" }])).toBe('Step 1 ("n"): "order" must be a number.');
    expect(bad([{ name: "ok", type: "T" }, { name: "second" }])).toBe('Step 2 ("second") needs a "type".');
    expect(bad([])).toBe("OK");
  });
});

test.describe("schemaFields.ts", () => {
  test("stored schema_definition fields are read, nested ones included, and unusable ones dropped", () => {
    const fields = schemaFieldsOf({
      fields: [
        { name: "id", type: "string", nullable: false, primary_key: true },
        { name: "addr", type: "object", fields: [{ name: "city", type: "string" }, { name: 1, type: "string" }] },
        { name: "tags", type: "array", items: { name: "t", type: "string" } },
        { type: "string" }, null, "x",
        { name: "amt", type: "decimal", precision: 10, scale: 2, nullable: "yes" },
      ],
    });
    expect(fields.map((f) => f.name)).toEqual(["id", "addr", "tags", "amt"]);
    expect(fields[0]).toEqual({ name: "id", type: "string", nullable: false, primary_key: true });
    expect(fields[1]?.fields).toEqual([{ name: "city", type: "string" }]);
    expect(fields[2]?.items).toEqual({ name: "t", type: "string" });
    expect(fields[3]).toEqual({ name: "amt", type: "decimal", precision: 10, scale: 2 });   // a non-boolean "nullable" is not trusted
  });
  test("a missing, null or malformed definition is an empty list, not an error", () => {
    for (const bad of [undefined, null, {}, { fields: "x" }, [], "x"]) expect(schemaFieldsOf(bad), JSON.stringify(bad)).toEqual([]);
  });
  test("pasted fields are validated for submission, with the index of the first bad one", () => {
    expect(parseSchemaFieldsInput([{ name: "a", type: "string" }])).toEqual({ ok: true, fields: [{ name: "a", type: "string" }] });
    expect(parseSchemaFieldsInput({ name: "a" })).toEqual({ ok: false, message: "Fields must be a JSON array." });
    expect(parseSchemaFieldsInput([{ name: "a", type: "string" }, { name: "b" }])).toEqual({ ok: false, message: 'Field 2 must be an object with a string "name" and "type".' });
  });
});

test.describe("qualityRules.ts", () => {
  test("buildRulePayload: only filled parameters, numbers for min/max, a list for values", () => {
    expect(buildRulePayload("required", "email", {})).toEqual({ name: "email_required", type: "required", field: "email" });
    expect(buildRulePayload("min_max_numeric", "age", { min: " 0 ", max: "120" })).toEqual({ name: "age_min_max_numeric", type: "min_max_numeric", field: "age", min: 0, max: 120 });
    expect(buildRulePayload("min_max_numeric", "age", { min: "", max: "5" })).toEqual({ name: "age_min_max_numeric", type: "min_max_numeric", field: "age", max: 5 });
    expect(buildRulePayload("allowed_values", "s", { values: "A, B,,C " })).toMatchObject({ values: ["A", "B", "C"] });
    expect(buildRulePayload("regex", "e", { pattern: ".+@.+" })).toMatchObject({ pattern: ".+@.+" });
    expect(buildRulePayload("type_check", "x", { expected_type: "integer" })).toMatchObject({ expected_type: "integer" });
    expect(buildRulePayload("regex", "e", { unrelated: "x" })).toEqual({ name: "e_regex", type: "regex", field: "e" });   // only the spec's own params
  });
  test("a rule type the form does not offer has no parameters; prototype keys are not rule types", () => {
    expect(ruleSpecFor("nope").params).toEqual([]);
    for (const t of Object.keys(QRULE_SPEC)) expect(isQualityRuleType(t)).toBe(true);
    for (const t of ["constructor", "__proto__", "toString", "", "REQUIRED"]) expect(isQualityRuleType(t), t).toBe(false);
  });
  test("parseQualityRule keeps only values of the right type and puts everything else in `extra`", () => {
    expect(parseQualityRule({ name: "n", type: "min_max_numeric", field: "f", min: 1, max: "9", note: "x" })).toEqual({
      name: "n", type: "min_max_numeric", field: "f", expectedType: undefined, pattern: undefined, values: [], min: 1, max: undefined, extra: { min: 1, max: "9", note: "x" },
    });
    expect(parseQualityRule({})).toMatchObject({ name: "", type: "", field: undefined, values: [] });
    expect(parseQualityRule({ values: ["a", 2, "b"] }).values).toEqual(["a", "b"]);
    expect(parseQualityRules([{ type: "required" }, null, "x"])).toHaveLength(1);
    expect(parseQualityRules(undefined)).toEqual([]);
  });
  test("describeRuleExpectation reads every rule type, and says so when the rule is gone", () => {
    const d = (raw: Record<string, unknown>) => describeRuleExpectation(parseQualityRule(raw));
    expect(d({ type: "required" })).toBe("must be present (not null / empty)");
    expect(d({ type: "type_check", expected_type: "integer" })).toBe('must be of type "integer"');
    expect(d({ type: "regex", pattern: "^a" })).toBe("must match pattern /^a/");
    expect(d({ type: "allowed_values", values: ["A", "B"] })).toBe("must be one of: A, B");
    expect(d({ type: "min_max_numeric", min: 1 })).toBe("must be between 1 and ∞");
    expect(d({ type: "min_max_numeric" })).toBe("must be between −∞ and ∞");
    expect(d({ type: "min_max_string_length", max: 5 })).toBe("length must be between 0 and 5");
    expect(d({ type: "timestamp_freshness" })).toBe("must satisfy timestamp_freshness");
    expect(describeRuleExpectation(undefined)).toBe("rule definition not found (policy may have changed since)");
  });
  test("ruleFields is the distinct non-empty field names", () => {
    expect(ruleFields(parseQualityRules([{ field: "a" }, { field: "b" }, { field: "a" }, {}, { field: "" }]))).toEqual(["a", "b"]);
  });
  test("typedSampleValue behaves exactly as the inline expression it replaced", () => {
    // The original, over strings: v === "true" ? true : v === "false" ? false : (!isNaN(v) ? Number(v) : v)
    const original = (v: string) => (v === "true" ? true : v === "false" ? false : !isNaN(v as unknown as number) ? Number(v) : v);
    for (const v of ["true", "false", "1", "-3.5", "1e3", "0x10", "abc", "12abc", "True", "Infinity", "NaN", "0", " 5", "5 "]) {
      expect(typedSampleValue(v), v).toBe(original(v));
    }
  });
});

test.describe("connectionConfig.ts", () => {
  const fields = [
    field({ key: "hostname", required: true }), field({ key: "port", type: "number" }), field({ key: "ssl", type: "boolean" }),
    field({ key: "tables", type: "list" }), field({ key: "options", type: "json" }), field({ key: "password", type: "secret", required: true }),
    field({ key: "note" }),
  ];
  const filled = { hostname: " db ", port: "5432", ssl: "true", tables: "a, b,,c", options: '{"x":1}', password: "pw", note: "" };

  test("form text becomes a typed config: numbers, booleans, lists and JSON; blank optional fields are omitted", () => {
    expect(buildConnectionConfig(fields, filled, { existing: false, secretChanged: {} })).toEqual({
      cfg: { hostname: "db", port: 5432, ssl: true, tables: ["a", "b", "c"], options: { x: 1 }, password: "pw" },
    });
    expect(buildConnectionConfig(fields, { ...filled, ssl: "false" }, { existing: false, secretChanged: {} })).toMatchObject({ cfg: { ssl: false } });
  });
  test("a missing required field, or invalid JSON, is reported with the field it belongs to", () => {
    expect(buildConnectionConfig(fields, { ...filled, hostname: "  " }, { existing: false, secretChanged: {} })).toEqual({ error: "hostname is required", field: "hostname" });
    expect(buildConnectionConfig(fields, { ...filled, options: "{bad" }, { existing: false, secretChanged: {} })).toEqual({ error: "options must be valid JSON", field: "options" });
    expect(buildConnectionConfig(fields, { ...filled, password: "" }, { existing: false, secretChanged: {} })).toEqual({ error: "password is required", field: "password" });
  });
  test("FE-BUG-11: an untouched secret on an existing connection is left out entirely, and not required", () => {
    const untouched = buildConnectionConfig(fields, { ...filled, password: SECRET_MASK }, { existing: true, secretChanged: {} });
    expect(untouched).toMatchObject({ cfg: expect.not.objectContaining({ password: expect.anything() }) });
    expect("error" in untouched).toBe(false);
    expect(buildConnectionConfig(fields, { ...filled, password: "new" }, { existing: true, secretChanged: { password: true } })).toMatchObject({ cfg: { password: "new" } });
  });
  test("initial values: existing config (lists joined, secret never pre-filled), else the schema default", () => {
    const existing = connection({ hostname: "h", tables: ["a", "b"], password: SECRET_MASK, port: 5432 });
    expect(initialFieldValue(field({ key: "tables", type: "list" }), existing)).toBe("a,b");
    expect(initialFieldValue(field({ key: "password", type: "secret" }), existing)).toBe("");
    expect(initialFieldValue(field({ key: "missing" }), existing)).toBe("");
    expect(initialFieldValue(field({ key: "port", default: 5432 }), undefined)).toBe("5432");
    expect(initialFieldValue(field({ key: "x" }), undefined)).toBe("");
  });
  test("secret configured and display helpers", () => {
    expect(isSecretConfigured(connection({ password: SECRET_MASK }), "password")).toBe(true);
    expect(isSecretConfigured(connection({ password: "x" }), "password")).toBe(false);
    expect(isSecretConfigured(undefined, "password")).toBe(false);
    expect(configText(["a", "b"])).toBe("a, b");
    expect(configText(null)).toBe("");
    expect(configText(7)).toBe("7");
  });
  test("latencySuffix reads a numeric latency_ms and nothing else", () => {
    expect(latencySuffix({ latency_ms: 12 })).toBe(" · 12ms");
    expect(latencySuffix({ latency_ms: "12" })).toBe("");
    expect(latencySuffix({})).toBe("");
    expect(latencySuffix(undefined)).toBe("");
  });
  test("list columns: Name, up to two notable schema columns (secrets redacted), Status", () => {
    const cols = columnsFromFields(fields);
    expect(cols.map((c) => c.label)).toEqual(["Friendly Name", "Created at", "Host", "ssl", "Status"]);   // Host, then the first notable non-secret field, capped at two
    const c = connection({ hostname: "db", port: 5432, password: "x" }, { name: "prod", status: "active" });
    expect(cols[0]?.get(c)).toBe("prod");
    expect(cols[1]?.get(connection({}, { name: "x", created_at: "2026-10-22T07:30:00Z" } as never))).toMatch(/2026/);
    expect(cols[1]?.get(c)).toBe("—");                                  // no created_at: a dash
    expect(cols[2]?.get(c)).toBe("db:5432");
    expect(cols[cols.length - 1]?.get(connection({}, { status: undefined }))).toBe("");
    const withSecretKeyColumn = columnsFromFields([field({ key: "api_token", required: true })]);
    expect(withSecretKeyColumn[2]?.get(connection({ api_token: "s3cret" }))).not.toContain("s3cret");
  });
});

test.describe("pipelineAdapters.ts", () => {
  test("a conflict's current pipeline is read for exactly the fields the conflict screen compares", () => {
    expect(conflictFromDetails({ name: "p", quality_policy_id: "q", error_handling_policy: "THRESHOLD", error_threshold_max_errors: 5, error_threshold_window_seconds: 60, unrelated: 1 }))
      .toEqual({ name: "p", quality_policy_id: "q", error_handling_policy: "THRESHOLD", error_threshold_max_errors: 5, error_threshold_window_seconds: 60 });
    expect(conflictFromDetails({ name: "p", quality_policy_id: null })).toMatchObject({ name: "p", quality_policy_id: null, error_handling_policy: undefined });
    // a partial pipeline is fine (only these fields are used); a value of the wrong type is dropped, not trusted
    expect(conflictFromDetails({ name: "p", error_threshold_max_errors: "5", quality_policy_id: 3 })).toMatchObject({ error_threshold_max_errors: undefined, quality_policy_id: undefined });
  });
  test("no name, or not an object at all, is not a usable conflict", () => {
    for (const bad of [null, undefined, "x", 7, [], {}, { name: 5 }]) expect(conflictFromDetails(bad), JSON.stringify(bad)).toBeNull();
  });
  test("a stored runtime-flow snapshot is trusted only if it has the fields a RuntimeFlow must", () => {
    const flow = { reachable: true, connect_url: "u", bootstrap: "b", connectors: [], topics: [], sinks: [] };
    expect(runtimeFlowFromStorage(JSON.stringify(flow))).toEqual(flow);
    expect(runtimeFlowFromStorage(JSON.stringify({ reachable: true, connect_url: "u", bootstrap: "b" }))).toBeDefined();
    for (const bad of [null, "", "{oops", "[]", "null", JSON.stringify({ ...flow, reachable: "yes" }), JSON.stringify({ ...flow, connectors: {} }), JSON.stringify({ connectors: [] })]) {
      expect(runtimeFlowFromStorage(bad), String(bad)).toBeUndefined();
    }
  });
});

test.describe("abridgeMetricsResponse (differential against the implementation it replaced)", () => {
  // The original, verbatim in behaviour: deep-copy through JSON, then edit the copy.
  function original(data: ConnectorMetrics): unknown {
    const copy = JSON.parse(JSON.stringify(data)) as Record<string, Record<string, unknown>[] | undefined>;
    const round = (v: unknown) => (typeof v === "number" ? +v.toFixed(3) : v);
    (copy.metrics ?? []).forEach((m) => {
      const series = m.series as unknown[] | undefined;
      if (Array.isArray(series) && series.length > 3) {
        m.series = [series[0], series[1], `… ${series.length - 3} more points …`, series[series.length - 1]];
      }
      m.current = round(m.current);
      ((m.series as unknown[] | undefined) ?? []).forEach((p) => { if (p && typeof p === "object") (p as Record<string, unknown>).v = round((p as Record<string, unknown>).v); });
    });
    (copy.tasks ?? []).forEach((t) => {
      const values = t.values as Record<string, unknown> | undefined;
      Object.keys(values ?? {}).forEach((k) => { if (values) values[k] = round(values[k]); });
    });
    return copy;
  }
  const base = { connector_name: "c", pipeline_id: "p", direction: "SOURCE", range: "15m", step_seconds: 15, observed_at: "t", prometheus_enabled: true, reachable: true };
  const point = (t: number, v: number | null) => ({ t, v });
  const sample = (extra: Record<string, unknown>) => ({ ...base, ...extra }) as unknown as ConnectorMetrics;
  const json = (v: unknown) => JSON.parse(JSON.stringify(v));

  test("matches the original on long series, short series, no series, null values, tasks and a missing metrics list", () => {
    const cases = [
      sample({ metrics: [{ id: "a", label: "A", unit: "n", current: 1.23456, series: [point(1, 1.11111), point(2, 2.22222), point(3, null), point(4, 4.44444), point(5, 5.55555)] }] }),
      sample({ metrics: [{ id: "b", label: "B", unit: "n", current: 0.0004, series: [point(1, 1.5), point(2, 2.5)] }] }),
      sample({ metrics: [{ id: "c", label: "C", unit: "n", current: null }, { id: "d", label: "D", unit: "n" }] }),
      sample({ tasks: [{ task: "0", values: { rate: 3.14159, lag: null, other: 7 } }, { task: "1" }] }),
      sample({}),
      sample({ metrics: [], tasks: [] }),
    ];
    for (const [i, data] of cases.entries()) expect(json(abridgeMetricsResponse(data)), `case ${i}`).toEqual(json(original(data)));
  });
  test("does not mutate its input", () => {
    const data = sample({ metrics: [{ id: "a", label: "A", unit: "n", current: 1.23456, series: [point(1, 1.11111), point(2, 2), point(3, 3), point(4, 4)] }] });
    const before = JSON.stringify(data);
    abridgeMetricsResponse(data);
    expect(JSON.stringify(data)).toBe(before);
  });
});

test.describe("templateAdapters.ts — required transform settings", () => {
  const [spec] = parseTransformSpecs([{
    type: "TimestampConverter", params: [
      { key: "field", label: "Timestamp field", required: true },
      { key: "target.type", label: "Target type", type: "select", required: true, default: "string", options: ["string", "unix"] },
      { key: "format", label: "Format", default: "yyyy-MM-dd HH:mm:ss", required_when: { key: "target.type", value: "string" } },
      { key: "note", label: "Note", required_when: { key: "", value: "x" } },
    ],
  }]);

  test("required_when is read into requiredWhen; a malformed one is dropped", () => {
    expect(spec!.params[2]!.requiredWhen).toEqual({ key: "target.type", value: "string" });
    expect(spec!.params[2]!.defaultValue).toBe("yyyy-MM-dd HH:mm:ss");
    expect(spec!.params[3]!.requiredWhen).toBeUndefined();
  });

  test("a conditional param is required only while its condition holds", () => {
    const format = spec!.params[2]!;
    expect(isParamRequired(format, { "target.type": "string" })).toBe(true);
    expect(isParamRequired(format, { "target.type": "unix" })).toBe(false);
    expect(isParamRequired(spec!.params[0]!, {})).toBe(true);
  });

  test("missingTransformParams lists the blank required ones, whitespace counting as blank", () => {
    const keys = (values: Record<string, string>) => missingTransformParams(spec!, values).map((p) => p.key);
    expect(keys({ field: "", "target.type": "string", format: "  " })).toEqual(["field", "format"]);
    expect(keys({ field: "created_at", "target.type": "unix", format: "" })).toEqual([]);
    expect(keys({ field: "created_at", "target.type": "string", format: "yyyy" })).toEqual([]);
  });
});

// ── RegexRouter is not offered on a source ─────────────────────────────────────────────────────────────────────────────────

test("a source is not offered RegexRouter (it always writes to its pipeline's topic); a sink is", () => {
  const supported_transforms = [
    { type: "MaskField", description: "Mask", params: [] },
    { type: "RegexRouter", description: "Route", params: [] },
  ];
  const source = { connector_type: "source", supported_transforms } as never;
  const sink = { connector_type: "sink", supported_transforms } as never;
  expect(transformsForTemplate(source).map((t) => t.type)).toEqual(["MaskField"]);
  expect(transformsForTemplate(sink).map((t) => t.type)).toEqual(["MaskField", "RegexRouter"]);
});
