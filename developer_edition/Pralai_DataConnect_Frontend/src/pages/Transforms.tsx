import React, { useEffect, useState } from "react";
import { useApp } from "../context/AppContext";
import {
  useGetPipelinesQuery, useGetTransformsQuery, useCreateTransformMutation, useDeleteTransformMutation,
  useValidateTransformMutation, usePreviewTransformMutation,
} from "../store/apiSlice";
import { redactedInlineJson } from "../secretRedaction";
import { smtOriginOf, SMT_TYPE_LABEL } from "../transformOrigin";
import RowMenu from "../components/RowMenu";
import { CreatedAt, PH, PipelineNames, StatCards, Pill, Loading, ErrBox, Drawer, CodeBlock, IconView, IconPreview, IconDel, IconCheck, FieldError } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import { SMT_TYPES } from "../constants";
import type { TransformPolicy, TransformStep } from "../types/api";
import { firstFieldError } from "../errors";
import { isRecord, parseJson } from "../json";
import { parseTransformSteps } from "../transformSteps";

const DEFAULT_SMTS = JSON.stringify([
  { order: 1, name: "createKey", type: "ValueToKey", scope: "value", config: { fields: "customer_id" } },
  { order: 2, name: "maskPII", type: "MaskField", scope: "value", config: { fields: "email,phone" } },
], null, 2);

function TransformForm({ onClose }: { onClose: () => void }) {
  const { ok, err } = useApp();
  const [createTransform] = useCreateTransformMutation();
  const [name, setName] = useState("Customer basic transform");
  const [desc, setDesc] = useState("key + mask PII");
  const [smts, setSmts] = useState(DEFAULT_SMTS);
  // FE-BUG-21: see ConnectionForm's own copy of this pattern.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);

  const submit = async () => {
    setFieldError(null);
    const parsed = parseJson(smts);
    if (!parsed.ok) {
      setFieldError({ key: "smts", message: "SMT steps must be valid JSON" });
      err("SMT steps must be valid JSON");
      return;
    }
    // Valid JSON is not yet valid steps: check their shape before they are sent (see transformSteps.ts).
    const steps = parseTransformSteps(parsed.value);
    if (!steps.ok) {
      setFieldError({ key: "smts", message: steps.message });
      err(steps.message);
      return;
    }
    try {
      await createTransform({ name, description: desc, transforms: steps.steps }).unwrap();
      onClose(); ok("SMT chain created");
    } catch (e: unknown) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ key: fe.field, message: fe.message });
      err(e);
    }
  };

  return (
    <Drawer title="Custom SMT chain" onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" onClick={submit}>Save SMT chain</button></>}>
      <div className="fg">
        <div className="fld">
          <label htmlFor="transform-name">Name</label>
          <input
            id="transform-name" value={name}
            onChange={(e) => { setName(e.target.value); setFieldError((fe) => (fe?.key === "name" ? null : fe)); }}
            aria-invalid={fieldError?.key === "name"} aria-describedby={fieldError?.key === "name" ? "transform-name-error" : undefined}
          />
          {fieldError?.key === "name" && <FieldError id="transform-name-error" message={fieldError.message} />}
        </div>
        <div className="fld"><label htmlFor="transform-desc">Description</label><input id="transform-desc" value={desc} onChange={(e) => setDesc(e.target.value)} /></div>
      </div>
      <div className="fld">
        <label htmlFor="transform-smts">SMT steps (JSON array: order,name,type,scope,config)</label>
        <textarea
          id="transform-smts" style={{ height: 180 }} value={smts}
          onChange={(e) => { setSmts(e.target.value); setFieldError((fe) => (fe?.key === "smts" ? null : fe)); }}
          aria-invalid={fieldError?.key === "smts"} aria-describedby={fieldError?.key === "smts" ? "transform-smts-error" : undefined}
        />
        {fieldError?.key === "smts" && <FieldError id="transform-smts-error" message={fieldError.message} />}
      </div>
      <div className="ab">SMT types: {SMT_TYPES.join(", ")}. RegexRouter works on sink pipelines only: a source always writes to the topic its pipeline is named for.</div>
    </Drawer>
  );
}

// FE-BUG-19: a genuine read-only view of a policy's SMT chain — no API
// call at all, since the full step list (order/name/type/scope/config) is
// already in the list this was opened from. Distinct from TransformPreview
// below, which is an interactive "type a sample record, POST it" tool that
// only dataconnect:transforms:edit can use. This is what a transforms:view-only
// user gets instead of either a hidden action or a preview tool that would
// just 403 on them.
function TransformViewModal({ t, onClose }: { t: TransformPolicy; onClose: () => void }) {
  const steps: TransformStep[] = t.transforms ?? [];
  return (
    <Drawer title={t.name} onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      {t.description && <div className="ab" style={{ marginBottom: 10 }}>{t.description}</div>}
      <div className="fsec">SMT chain ({steps.length})</div>
      {steps.length ? steps.map((s, i) => (
        <div className="smt" key={i} style={{ gridTemplateColumns: "22px 1fr 1.3fr" }}>
          <div className="smt-n">{s.order ?? i + 1}</div>
          <div><div className="smt-name">{s.name || s.type}</div><div className="smt-type">{s.type}{s.scope ? " · " + s.scope : ""}</div></div>
          <span className="smt-cfg">{redactedInlineJson(s.config || {})}</span>
        </div>
      )) : <div className="empty">No SMT steps</div>}
    </Drawer>
  );
}

function TransformPreview({ id, onClose }: { id: string; onClose: () => void }) {
  const { err } = useApp();
  const [previewTransform] = usePreviewTransformMutation();
  const [rec, setRec] = useState(JSON.stringify({ customer_id: "c1", email: "a@b.com", phone: "555" }, null, 2));
  const [out, setOut] = useState<Record<string, unknown> | null>(null);
  const run = async () => {
    try {
      const record = parseJson(rec);
      if (!record.ok || !isRecord(record.value)) { err("Input record must be a JSON object"); return; }
      const r = await previewTransform({ id, record: record.value }).unwrap();
      setOut(r.generated_config);
    } catch (e) { err(e); }
  };
  return (
    <Drawer title="Preview transform" onClose={onClose}
      footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div className="fld"><label htmlFor="transform-preview-record">Input record (JSON)</label><textarea id="transform-preview-record" style={{ height: 90 }} value={rec} onChange={(e) => setRec(e.target.value)} /></div>
      <button className="btn-s" style={{ marginTop: 8 }} onClick={run}>Generate config</button>
      {out != null && (
        <div style={{ marginTop: 10 }}>
          <div className="fsec">Generated Kafka Connect config</div>
          <CodeBlock value={out} />
        </div>
      )}
    </Drawer>
  );
}

export default function Transforms({ focus, clearFocus }: PageFocusProps) {
  const { openModal, closeModal, toast, ok, err, can } = useApp();
  const { data: tfsData, isLoading: loading, error } = useGetTransformsQuery();
  const { data: pipesData } = useGetPipelinesQuery();
  const [validateTransform] = useValidateTransformMutation();
  const [deleteTransform] = useDeleteTransformMutation();
  const canManage = can("dataconnect:transforms:edit");

  // A transform policy picked in the top bar's search: open its details once the list is here.
  useEffect(() => {
    if (focus?.kind !== "transform" || !tfsData) return;
    const picked = tfsData.find((t) => t.id === focus.id);
    if (picked) openModal(<TransformViewModal t={picked} onClose={closeModal} />);
    clearFocus?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, tfsData, clearFocus]);

  if (loading) return <Loading />;
  if (error) return <ErrBox error={error} />;

  const tfs = tfsData || [];
  // The pipelines that use each policy (a pipeline records its policy in transform_policy_id).
  const pipelinesByPolicy: Record<string, string[]> = {};
  (pipesData || []).forEach((p) => { if (p.transform_policy_id) (pipelinesByPolicy[p.transform_policy_id] ??= []).push(p.name); });
  const validate = async (id: string) => { try { const r = await validateTransform(id).unwrap(); toast(r.valid ? "Valid ✓" : "Invalid: " + (r.errors || []).join(", "), r.valid ? "ok" : "err"); } catch (e) { err(e); } };
  const remove = async (id: string, name: string) => { if (!window.confirm(`Delete "${name}"?`)) return; try { await deleteTransform(id).unwrap(); ok("SMT chain deleted"); } catch (e) { err(e); } };

  return (
    // Title and cards stay put; the table's column header stays put and only its rows scroll (same as Pipelines).
    <div className="page-fill">
      <PH title="Transforms" sub="Compose and manage Kafka Connect SMT chains"
        right={canManage && <button className="btn-p" onClick={() => openModal(<TransformForm onClose={closeModal} />)}>+ New SMT chain</button>} />
      <StatCards cards={[
        { k: "bl", l: "SMT chains", v: tfs.length },
        { k: "gn", l: "Total SMTs", v: tfs.reduce((a, t) => a + (t.transforms || []).length, 0) },
        { k: "am", l: "SMT types", v: SMT_TYPES.length },
      ]} />
      <div className="tbl tbl-fill" role="table" style={{ "--cols": "1.6fr 1fr 0.6fr 1.3fr 0.7fr 1.6fr 1fr" }}>
        <div className="thead" role="row"><div className="th" role="columnheader">Chain Name</div><div className="th" role="columnheader">Created at</div><div className="th" role="columnheader">Type</div><div className="th" role="columnheader">Pipeline Name</div><div className="th" role="columnheader">SMTs</div><div className="th" role="columnheader">Description</div><div className="th th-actions" role="columnheader">Actions</div></div>
        <div className="tbody" role="rowgroup">
          {tfs.length ? tfs.map((t) => (
            <div className="tr" role="row" key={t.id}>
              <div className="td" role="cell" style={{ fontWeight: 500 }}>{t.name}</div>
              <div className="tdm" role="cell"><CreatedAt value={t.created_at} /></div>
              <div className="td" role="cell"><Pill cls={smtOriginOf(t) === "auto" ? "bp" : "bb"}>{SMT_TYPE_LABEL[smtOriginOf(t)]}</Pill></div>
              <div className="td" role="cell"><PipelineNames names={pipelinesByPolicy[t.id] ?? []} /></div>
              <div className="td" role="cell"><Pill cls="bb">{(t.transforms || []).length} SMTs</Pill></div>
              <div className="tdm" role="cell">{t.description || ""}</div>
              <div className="acts" role="cell" style={{ justifyContent: "center" }}>
                {/* The row's actions behind the three dots (same menu as Pipelines). FE-BUG-19: viewing is ungated — anyone who can open this page
                    (dataconnect:transforms:view) can see the SMT chain, matching the read-only-view pattern Quality/Schemas follow. */}
                <RowMenu
                  label={`Actions for ${t.name}`}
                  items={[
                    { key: "view", label: "View details", ariaLabel: `View ${t.name}`, icon: <IconView />, onSelect: () => openModal(<TransformViewModal t={t} onClose={closeModal} />) },
                    ...(canManage ? [
                      { key: "validate", label: "Validate", ariaLabel: `Validate ${t.name}`, icon: <IconCheck />, onSelect: () => validate(t.id) },
                      { key: "preview", label: "Preview config", ariaLabel: `Preview ${t.name}`, icon: <IconPreview />, onSelect: () => openModal(<TransformPreview id={t.id} onClose={closeModal} />) },
                      { key: "delete", label: "Delete", ariaLabel: `Delete ${t.name}`, icon: <IconDel />, danger: true, onSelect: () => remove(t.id, t.name) },
                    ] : []),
                  ]}
                />
              </div>
            </div>
          )) : <div className="empty">No SMT chains yet</div>}
        </div>
      </div>
    </div>
  );
}
