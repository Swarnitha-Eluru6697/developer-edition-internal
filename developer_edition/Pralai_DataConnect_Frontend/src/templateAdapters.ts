/*
 * FE-GAP-02: what a connector template says it can do, as typed view models.
 *
 * `ConnectorTemplate` (generated) types the stable fields — `connector_type`, `supported_modes`, `capabilities`, ... — but
 * `supported_transforms` is a list of free-form JSON objects (the backend's seed catalogue, app/db/seed_templates.py:
 * `{ type, description, params: [{ key, label, type, required, required_when, default, placeholder, options }] }`). That list is the dynamic boundary:
 * it is validated and normalized HERE into `TransformSpec`, and the wizard only ever sees the typed result. An entry without a string
 * `type` is dropped; a malformed param is dropped; one bad entry never takes down the form.
 */
import { MODES } from "./constants";
import { recordsOf, stringOf, stringsOf, type JsonRecord } from "./json";
import type { ConnectorTemplate } from "./types/api";

export type TransformParamType = "text" | "list" | "select";

export interface TransformParamSpec {
  key: string;
  label: string;
  type: TransformParamType;
  required: boolean;
  /** The initial text for this param; "" when the template declares none. */
  defaultValue: string;
  placeholder: string;
  options: string[];
  /** Required only while another param of the same transform has this value (TimestampConverter's format: only for a
   *  string target). Undefined when the template declares no condition. */
  requiredWhen?: { key: string; value: string };
}

export interface TransformSpec {
  type: string;
  description: string;
  params: TransformParamSpec[];
}

function paramType(raw: unknown): TransformParamType {
  return raw === "list" || raw === "select" ? raw : "text";
}

function toParam(entry: JsonRecord): TransformParamSpec | null {
  const key = stringOf(entry.key);
  if (!key) return null;
  const rawDefault = entry.default;
  const when = recordsOf([entry.required_when])[0];
  const whenKey = when ? stringOf(when.key) : undefined;
  const whenValue = when ? stringOf(when.value) : undefined;
  return {
    key,
    label: stringOf(entry.label) ?? key,
    type: paramType(entry.type),
    required: entry.required === true,
    defaultValue: rawDefault === null || rawDefault === undefined ? "" : String(rawDefault),
    placeholder: stringOf(entry.placeholder) ?? "",
    options: stringsOf(entry.options),
    ...(whenKey && whenValue !== undefined ? { requiredWhen: { key: whenKey, value: whenValue } } : {}),
  };
}

/** Is `param` required given the transform's current `values`? (`required`, or its `requiredWhen` condition holds.) */
export function isParamRequired(param: TransformParamSpec, values: Record<string, string>): boolean {
  if (param.required) return true;
  return param.requiredWhen ? (values[param.requiredWhen.key] ?? "") === param.requiredWhen.value : false;
}

/** The required params of `spec` left blank in `values` — the New pipeline form refuses to create the pipeline until there are none:
 *  Kafka Connect only notices a missing setting when the task starts, so the pipeline would otherwise go straight to FAILED. */
export function missingTransformParams(spec: TransformSpec, values: Record<string, string>): TransformParamSpec[] {
  return spec.params.filter((p) => isParamRequired(p, values) && !(values[p.key] ?? "").trim());
}

/** Validates a template's `supported_transforms` (dynamic JSON) into typed specs. */
export function parseTransformSpecs(raw: unknown): TransformSpec[] {
  const specs: TransformSpec[] = [];
  for (const entry of recordsOf(raw)) {
    const type = stringOf(entry.type);
    if (!type) continue;
    specs.push({
      type,
      description: stringOf(entry.description) ?? "",
      params: recordsOf(entry.params).map(toParam).filter((p): p is TransformParamSpec => p !== null),
    });
  }
  return specs;
}

/** FE-BUG-23: what the selected connector template says it can do, read straight from the backend's `capabilities` block
 * (app/schemas/catalog.py). A template with no `capabilities` at all (or a flag missing from it) is unrestricted — the same
 * "undeclared means supported" convention the backend's own validators use — so an uncurated template keeps every option. */
export interface TemplateCapabilities { cdc: boolean; snapshot: boolean; schemaDiscovery: boolean; supportsSMT: boolean; supportsDLQ: boolean }

export function templateCapabilities(tpl: ConnectorTemplate | undefined): TemplateCapabilities {
  const caps: Record<string, boolean> = tpl?.capabilities ?? {};
  const flag = (value: boolean | undefined) => value !== false;
  return {
    cdc: flag(caps.cdc), snapshot: flag(caps.snapshot), schemaDiscovery: flag(caps.schemaDiscovery),
    supportsSMT: flag(caps.supportsSMT), supportsDLQ: flag(caps.supportsDLQ),
  };
}

/** The transformations a template offers: none at all when it says supportsSMT:false, whatever its list holds. */
export function transformsForTemplate(tpl: ConnectorTemplate | undefined): TransformSpec[] {
  if (!templateCapabilities(tpl).supportsSMT) return [];
  const specs = parseTransformSpecs(tpl?.supported_transforms);
  // A source always writes to the topic its pipeline is named for (the platform routes it there itself), so a RegexRouter would have no effect
  // or send records where nothing looks — the backend refuses it on a source (SOURCE_REFUSED_TRANSFORMS); it is not offered here either.
  return tpl?.connector_type === "source" ? specs.filter((spec) => !SOURCE_REFUSED_TRANSFORMS.has(spec.type)) : specs;
}

/** Transform types a source connector cannot use (mirrors the backend's SOURCE_REFUSED_TRANSFORMS). */
export const SOURCE_REFUSED_TRANSFORMS: ReadonlySet<string> = new Set(["RegexRouter"]);

/** The ingestion modes a template allows. Empty `supported_modes` means unrestricted/undeclared (the convention the backend's own
 * create-time and deploy-time checks use — app/services/pipeline_spec_validator.py), not "supports nothing". The capability flags
 * are read directly too: a template that says cdc:false / snapshot:false must not offer those modes even if its list is empty. */
export function modesForTemplate(tpl: ConnectorTemplate | undefined): string[] {
  const supported = tpl?.supported_modes ?? [];
  const declared = supported.length ? MODES.filter((m) => supported.includes(m)) : [...MODES];
  const caps = templateCapabilities(tpl);
  return declared.filter((m) => !(m === "cdc" && !caps.cdc) && !(m === "snapshot" && !caps.snapshot));
}
