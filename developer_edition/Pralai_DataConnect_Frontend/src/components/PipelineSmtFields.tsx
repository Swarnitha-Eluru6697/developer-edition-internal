/*
 * The pipeline form's two ways to pick a pipeline's transformations, shared by New pipeline and Edit pipeline:
 *   - TransformationsSection: the connector's own transformations as tick-boxes, each with its settings;
 *   - CustomChainSection: instead of the boxes, one ready-made Custom SMT chain from the Transforms page.
 * Either the boxes or a chain, never both. A chain with a step the connector can't run is listed but can't be chosen.
 * The helpers turn ticked boxes into the steps a chain stores, and a stored chain back into ticked boxes (for Edit).
 */
import React from "react";
import Select from "./Select";
import { FieldError } from "./ui";
import { isParamRequired, templateCapabilities, transformsForTemplate } from "../templateAdapters";
import { smtOriginOf } from "../transformOrigin";
import type { ConnectorTemplate, Pipeline, TransformPolicy } from "../types/api";

export interface TransformSelection { checked: boolean; params: Record<string, string> }

/** The steps a chain stores for the ticked boxes, in the order they are listed. */
export function stepsFromSelection(tpl: ConnectorTemplate | undefined, tf: Record<string, TransformSelection>) {
  const picked = Object.entries(tf).filter(([, v]) => v.checked);
  return picked.map(([type, v], i) => {
    const meta = transformsForTemplate(tpl).find((x) => x.type === type);
    const config: Record<string, string> = {};
    (meta?.params ?? []).forEach((p) => {
      let val = (v.params[p.key] || "").trim();
      if (p.type === "list") val = val.split(",").map((s) => s.trim()).filter(Boolean).join(",");
      if (val) config[p.key] = val;
    });
    if (type === "HeaderFrom") {
      if (!config.headers && config.fields) config.headers = config.fields;
      if (!config.operation) config.operation = "copy";
    }
    return { order: i + 1, name: type, type, scope: "value", config };
  });
}

/** The boxes for a connector with nothing ticked, each with its settings' defaults. */
export function emptySelection(tpl: ConnectorTemplate | undefined): Record<string, TransformSelection> {
  const st: Record<string, TransformSelection> = {};
  transformsForTemplate(tpl).forEach((x) => {
    const params: Record<string, string> = {};
    x.params.forEach((p) => { params[p.key] = p.defaultValue; });
    st[x.type] = { checked: false, params };
  });
  return st;
}

/** A stored chain shown as ticked boxes: a box is ticked for each step of its type, with that step's settings filled in. */
export function selectionFromSteps(tpl: ConnectorTemplate | undefined, steps: { type: string; config?: Record<string, unknown> | null }[]): Record<string, TransformSelection> {
  const st = emptySelection(tpl);
  for (const step of steps) {
    const box = st[step.type];
    if (!box || box.checked) continue;
    box.checked = true;
    for (const [key, value] of Object.entries(step.config ?? {})) box.params[key] = Array.isArray(value) ? value.join(",") : String(value ?? "");
  }
  return st;
}

/** The steps of a stored chain this connector has no box for (they could not be shown, or kept, as boxes). */
export function stepsWithoutBox(tpl: ConnectorTemplate | undefined, steps: { type: string }[]): string[] {
  const known = new Set(transformsForTemplate(tpl).map((x) => x.type));
  return [...new Set(steps.map((s) => s.type).filter((t) => !known.has(t)))];
}

/** What the Custom chain choice looks like for this connector: the chains on offer, which steps each lacks, and what is picked. */
export function chainChoice(tpl: ConnectorTemplate | undefined, smts: TransformPolicy[] | undefined, customId: string) {
  const supportedTypes = new Set(transformsForTemplate(tpl).map((x) => x.type));
  const unsupportedSteps = (t: { transforms?: { type: string }[] }) => [...new Set((t.transforms ?? []).map((st) => st.type).filter((type) => !supportedTypes.has(type)))];
  const customSmts = (smts ?? []).filter((t) => smtOriginOf(t) === "custom");
  const chosenSmt = customSmts.find((t) => t.id === customId);
  return { unsupportedSteps, customSmts, chosenSmt, chosenUnsupported: chosenSmt ? unsupportedSteps(chosenSmt) : [] };
}

export function TransformationsSection({ tpl, tf, customChosen, setChecked, setParam, fieldError }: {
  tpl: ConnectorTemplate | undefined; tf: Record<string, TransformSelection>; customChosen: boolean;
  setChecked: (type: string, checked: boolean) => void; setParam: (type: string, key: string, val: string) => void;
  fieldError: { key: string; message: string } | null;
}) {
  const caps = templateCapabilities(tpl);
  const supported = transformsForTemplate(tpl);
  return (
    <>
      <div className="fsec">{caps.supportsSMT ? "Transformations (supported by this source)" : "Transformations"}</div>
      <div style={{ marginBottom: 12 }}>
        {!caps.supportsSMT ? <div className="ab">This connector doesn't support transformations (SMTs), so none can be applied to its records.</div> : supported.length ? supported.map((x) => { const sel = tf[x.type]; return (
          <div key={x.type} style={{ border: "1px solid var(--color-border-primary)", borderRadius: 6, padding: "8px 10px", marginBottom: 6 }}>
            <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12, cursor: "pointer" }}>
              <input type="checkbox" checked={sel?.checked ?? false} disabled={customChosen} onChange={(e) => setChecked(x.type, e.target.checked)} style={{ marginTop: 2, width: "auto", height: "auto" }} />
              <span><span style={{ fontWeight: 500 }}>{x.type}</span><br /><span style={{ fontSize: 10, color: "var(--color-text-secondary)" }}>{x.description}</span></span>
            </label>
            {sel?.checked && (
              <div style={{ marginTop: 8, paddingLeft: 26 }}>
                {x.params.length ? x.params.map((p) => (
                  <div className="fld" key={p.key} style={{ marginBottom: 6 }}>
                    <label htmlFor={`pipe-tf-${x.type}-${p.key}`}>{p.label}{p.type === "list" ? " (comma-separated)" : ""}{isParamRequired(p, sel.params) ? " *" : ""}</label>
                    {p.type === "select"
                      ? <Select id={`pipe-tf-${x.type}-${p.key}`} value={sel.params[p.key] ?? ""} onChange={(e) => setParam(x.type, p.key, e.target.value)}>{p.options.map((o) => <option key={o}>{o}</option>)}</Select>
                      // "e.g." so an empty box never reads as one already filled in — a placeholder is not sent.
                      : <input id={`pipe-tf-${x.type}-${p.key}`} value={sel.params[p.key] ?? ""} placeholder={p.placeholder ? `e.g. ${p.placeholder}` : undefined}
                          aria-invalid={fieldError?.key === `tf:${x.type}:${p.key}`}
                          aria-describedby={fieldError?.key === `tf:${x.type}:${p.key}` ? `pipe-tf-${x.type}-${p.key}-error` : undefined}
                          onChange={(e) => setParam(x.type, p.key, e.target.value)} />}
                    {fieldError?.key === `tf:${x.type}:${p.key}` && <FieldError id={`pipe-tf-${x.type}-${p.key}-error`} message={fieldError.message} />}
                  </div>
                )) : <div className="ab">Applies to the whole record — no field to configure.</div>}
              </div>
            )}
          </div>
        ); }) : <div className="ab">No transformations listed for this source.</div>}
      </div>

    </>
  );
}

export function CustomChainSection({ tpl, smts, pipelines, tf, customId, setCustomId, selfId }: {
  tpl: ConnectorTemplate | undefined; smts: TransformPolicy[] | undefined; pipelines: Pipeline[] | undefined; tf: Record<string, TransformSelection>;
  customId: string; setCustomId: (id: string) => void;
  /** The pipeline being edited, left out of "Also used by". */
  selfId?: string;
}) {
  const caps = templateCapabilities(tpl);
  const { unsupportedSteps, customSmts, chosenSmt, chosenUnsupported } = chainChoice(tpl, smts, customId);
  const anyTicked = Object.values(tf).some((v) => v.checked);
  const chosenUsedBy = (pipelines ?? []).filter((p) => p.id !== selfId && p.transform_policy_id === customId).map((p) => p.name);
  return (
      <>
          <div className="fld" style={{ marginBottom: 6 }}><label htmlFor="pipe-custom-smt">Custom SMT chain (optional)</label>
            <Select id="pipe-custom-smt" value={customId} disabled={anyTicked || !caps.supportsSMT} aria-describedby="pipe-custom-smt-help" aria-invalid={chosenUnsupported.length > 0}
              onChange={(e) => setCustomId(e.target.value)}>
              <option value="">— none —</option>
              {customSmts.map((t) => {
                const bad = unsupportedSteps(t);
                return <option key={t.id} value={t.id} disabled={bad.length > 0}>
                  {t.name} ({(t.transforms ?? []).length} SMTs){bad.length ? ` — not supported by this connector: ${bad.join(", ")}` : ""}
                </option>;
              })}
            </Select>
          </div>
          <div className="ab" id="pipe-custom-smt-help" style={{ marginBottom: 6 }}>
            {!caps.supportsSMT
              ? "This connector doesn't support transformations, so no Custom SMT chain can be used with it."
              : chosenUnsupported.length
              ? <FieldError id="pipe-custom-smt-error" message={`${chosenSmt?.name} has ${chosenUnsupported.join(", ")}, which ${tpl?.name ?? "this connector"} doesn't support. Choose another Custom SMT chain or none.`} />
              : customId
                ? "Using this Custom SMT chain as it is, so the transformations above are turned off. Choose none to pick them one by one."
                : anyTicked
                  ? "Untick the transformations above to use a Custom SMT chain instead."
                  : "Or use a ready-made SMT chain from the Transforms page (created with + New SMT chain) instead of picking transformations one by one."}
          </div>
          {chosenSmt && (
            <div style={{ border: "1px solid var(--color-border-primary)", borderRadius: 6, padding: "8px 10px", marginBottom: 12, fontSize: 12 }}>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>{chosenSmt.name} — {(chosenSmt.transforms ?? []).length} {(chosenSmt.transforms ?? []).length === 1 ? "step" : "steps"}</div>
              <ol aria-label="Steps of the chosen Custom SMT chain" style={{ margin: 0, paddingLeft: 18, color: "var(--color-text-secondary)" }}>
                {[...(chosenSmt.transforms ?? [])].sort((a, b) => a.order - b.order).map((st) => <li key={`${st.order}-${st.name}`}>{st.name} — {st.type} ({st.scope})</li>)}
              </ol>
              {chosenUsedBy.length > 0 && (
                <div style={{ marginTop: 6, color: "var(--color-text-secondary)" }}>
                  Also used by {chosenUsedBy.slice(0, 3).join(", ")}{chosenUsedBy.length > 3 ? ` and ${chosenUsedBy.length - 3} more` : ""} — editing this SMT chain later changes all of them.
                </div>
              )}
            </div>
          )}
        </>
  );
}
