import React, { useEffect, useState } from "react";
import Select from "../components/Select";
import { useApp } from "../context/AppContext";
import {
  useGetSchemasQuery, useGetDriftApprovalsQuery, useGetSchemaQuery, useGetSchemaDriftEventsQuery,
  useCreateSchemaMutation, useInferSchemaMutation, useDetectSchemaDriftMutation, useDecideDriftApprovalMutation,
  useGetPipelinesQuery,
} from "../store/apiSlice";
import { CutText, CreatedAt, PipelineNames, PH, StatCards, Pill, Loading, ErrBox, Drawer, CodeBlock, IconView, FieldError } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import { TYPE_TAG } from "../constants";
import type { SchemaInferResult, SchemaDriftResult, SchemaDriftDecision, SchemaDriftApproval, SchemaDriftEvent } from "../types/api";
import { firstFieldError } from "../errors";
import { parseJson, parseJsonRecords } from "../json";
import { parseSchemaFieldsInput, schemaFieldsOf } from "../schemaFields";
import { driftEventOf, type DriftField } from "../schemaDrift";

// ── FE-BUG-18: human-readable drift lines instead of a raw JSON diff ────────
// Mirrors app/services/schema_drift.py's compare_schema_fields() event shape
// exactly: {field, drift_type, action, details: {approved?, observed?}} —
// drift_type is the fixed vocabulary that function emits, so this switches
// on it rather than re-deriving anything the backend already computed.
interface DriftLine { field: string; kind: "add" | "change" | "remove"; breaking: boolean; description: string }

function fieldTypeDesc(f: DriftField | undefined): string {
  if (!f) return "?";
  return `${f.type ?? "?"}${f.nullable === false ? " required" : " nullable"}`;
}

function parseDriftLines(events: SchemaDriftEvent[] | undefined): DriftLine[] {
  return (events || []).map((raw) => {
    const e = driftEventOf(raw);
    const field = e.field || "?";
    const approved = e.approved;
    const observed = e.observed;
    switch (e.drift_type) {
      case "new_optional_field":
        return { field, kind: "add", breaking: false, description: fieldTypeDesc(observed) };
      case "new_required_field":
        // A field every new record must have, that no writer was told
        // about before this drift — old writers omitting it is exactly
        // what makes this breaking, unlike an optional addition.
        return { field, kind: "add", breaking: true, description: fieldTypeDesc(observed) };
      case "field_removed":
        return { field, kind: "remove", breaking: true, description: approved?.type ?? "?" };
      case "type_changed":
        return { field, kind: "change", breaking: true, description: `${approved?.type ?? "?"} → ${observed?.type ?? "?"}` };
      case "nested_structure_changed":
        return { field, kind: "change", breaking: true, description: "nested structure changed" };
      case "primary_key_changed":
        return { field, kind: "change", breaking: true, description: observed?.primary_key ? "now primary key" : "no longer primary key" };
      default:
        return { field, kind: "change", breaking: true, description: e.drift_type || "unrecognized change" };
    }
  });
}

const KIND_SYMBOL: Record<DriftLine["kind"], string> = { add: "+", remove: "-", change: "~" };
const KIND_COLOR: Record<DriftLine["kind"], string> = { add: "#16a34a", remove: "#b23b3b", change: "#d97706" };

function SchemaDriftDialog({ approval, pipelineName, onClose }: { approval: SchemaDriftApproval; pipelineName?: string; onClose: () => void }) {
  const { me, ok, err } = useApp();
  const [decideDriftApproval] = useDecideDriftApprovalMutation();
  const [comment, setComment] = useState("");
  const lines = parseDriftLines(approval.drift_events);
  const breaking = lines.some((l) => l.breaking);

  const decide = async (action: SchemaDriftDecision) => {
    try {
      await decideDriftApproval({ id: approval.id, action, body: { reviewed_by: me?.user || me?.userId || "unknown", comment: comment.trim() || undefined } }).unwrap();
      onClose();
      ok(action === "approve" ? "Schema version approved" : "Schema drift rejected");
    } catch (e) { err(e); }
  };

  return (
    <Drawer title="Schema change detected" onClose={onClose}
      footer={<><button className="btn-no" onClick={() => decide("reject")}>Reject</button><button className="btn-ok" onClick={() => decide("approve")}>Approve new schema version</button></>}>
      <div className="tbl" role="table" style={{ "--cols": "0.3fr 1fr 1.4fr" } as React.CSSProperties}>
        <div className="tbody" role="rowgroup">
          {lines.length ? lines.map((l, i) => (
            <div className="tr" role="row" key={i}>
              <div className="td" role="cell" style={{ fontWeight: 700, color: KIND_COLOR[l.kind] }}>{KIND_SYMBOL[l.kind]}</div>
              <div className="td" role="cell" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{l.field}</div>
              <div className="td" role="cell" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{l.description}</div>
            </div>
          )) : <div className="empty">No field-level detail on this drift event.</div>}
        </div>
      </div>

      <div className="kv">
        <span className="kvk">Compatibility</span>
        <span className="kvv"><Pill cls={breaking ? "ba" : "bg"}>{breaking ? "⚠ Breaking change" : "✓ Compatible"}</Pill></span>
      </div>
      {pipelineName && (
        <div className="kv"><span className="kvk">Affected pipeline</span><span className="kvv">{pipelineName}</span></div>
      )}

      <div className="fld" style={{ marginTop: 12 }}><label htmlFor="drift-comment">Comment (optional)</label>
        <textarea id="drift-comment" style={{ height: 60 }} value={comment} onChange={(e) => setComment(e.target.value)} />
      </div>
    </Drawer>
  );
}

const DEFAULT_FIELDS = JSON.stringify([
  { name: "account_id", type: "string", nullable: false, primary_key: true },
  { name: "name", type: "string", nullable: false, primary_key: false },
  { name: "created_at", type: "timestamp", nullable: true, primary_key: false },
], null, 2);

function SchemaRegister({ onClose }: { onClose: () => void }) {
  const { ok, err } = useApp();
  const [createSchema] = useCreateSchemaMutation();
  const [f, setF] = useState({ dom: "crm", src: "salesforce", ent: "accounts", ver: "v1", fmt: "json_schema", comp: "BACKWARD" });
  const [fields, setFields] = useState(DEFAULT_FIELDS);
  // FE-BUG-21: see ConnectionForm's own copy of this pattern.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);
  const on = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = async () => {
    setFieldError(null);
    const parsed = parseJson(fields);
    if (!parsed.ok) {
      setFieldError({ key: "fields", message: "Fields must be valid JSON" });
      err("Fields must be valid JSON");
      return;
    }
    // Valid JSON is not yet a valid field list: check its shape before it is sent (see schemaFields.ts).
    const checked = parseSchemaFieldsInput(parsed.value);
    if (!checked.ok) {
      setFieldError({ key: "fields", message: checked.message });
      err(checked.message);
      return;
    }
    try {
      await createSchema({ domain: f.dom, source_system: f.src, entity: f.ent, version: f.ver, schema_format: f.fmt, compatibility: f.comp, status: "active", schema_definition: { fields: checked.fields } }).unwrap();
      onClose(); ok("Schema registered");
    } catch (e: unknown) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ key: fe.field, message: fe.message });
      err(e);
    }
  };
  return (
    <Drawer title="Register schema" onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" onClick={submit}>Save schema</button></>}>
      <div className="fsec">Identity</div>
      <div className="fg"><div className="fld"><label htmlFor="schema-domain">Domain</label><input id="schema-domain" value={f.dom} onChange={on("dom")} /></div><div className="fld"><label htmlFor="schema-source">Source system</label><input id="schema-source" value={f.src} onChange={on("src")} /></div></div>
      <div className="fg"><div className="fld"><label htmlFor="schema-entity">Entity</label><input id="schema-entity" value={f.ent} onChange={on("ent")} /></div><div className="fld"><label htmlFor="schema-version">Version</label><input id="schema-version" value={f.ver} onChange={on("ver")} /></div></div>
      <div className="fg">
        <div className="fld"><label htmlFor="schema-format">Format</label><Select id="schema-format" value={f.fmt} onChange={on("fmt")}><option>json_schema</option><option>avro</option><option>protobuf</option></Select></div>
        <div className="fld"><label htmlFor="schema-compat">Compatibility</label><Select id="schema-compat" value={f.comp} onChange={on("comp")}><option>BACKWARD</option><option>FORWARD</option><option>FULL</option><option>NONE</option></Select></div>
      </div>
      <div className="fld">
        <label htmlFor="schema-fields">Fields (JSON array: name,type,nullable,primary_key)</label>
        <textarea
          id="schema-fields" style={{ height: 150 }} value={fields}
          onChange={(e) => { setFields(e.target.value); setFieldError((fe) => (fe?.key === "fields" ? null : fe)); }}
          aria-invalid={fieldError?.key === "fields"} aria-describedby={fieldError?.key === "fields" ? "schema-fields-error" : undefined}
        />
        {fieldError?.key === "fields" && <FieldError id="schema-fields-error" message={fieldError.message} />}
      </div>
    </Drawer>
  );
}

function SchemaInfer({ onClose }: { onClose: () => void }) {
  const { err } = useApp();
  const [inferSchema] = useInferSchemaMutation();
  const [rec, setRec] = useState(JSON.stringify([{ id: "1", amount: 9.99, active: true, created_at: "2026-01-01T00:00:00Z" }], null, 2));
  const [out, setOut] = useState<SchemaInferResult | null>(null);
  const run = async () => {
    const records = parseJsonRecords(rec);
    if (!records.ok) { err(records.message); return; }
    try { setOut(await inferSchema(records.records).unwrap()); } catch (e) { err(e); }
  };
  return (
    <Drawer title="Infer schema from records" onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Close</button><button className="btn-p" onClick={run}>Infer</button></>}>
      <div className="fld"><label htmlFor="schema-infer-records">Sample records (JSON array)</label><textarea id="schema-infer-records" style={{ height: 140 }} value={rec} onChange={(e) => setRec(e.target.value)} /></div>
      {out && <div style={{ marginTop: 12 }}><div className="cs" style={{ marginBottom: 6 }}>confidence {out.confidence}</div><CodeBlock value={out.fields} /></div>}
    </Drawer>
  );
}

function SchemaViewModal({ id, onClose }: { id: string; onClose: () => void }) {
  const { ok, err, can } = useApp();
  const canManage = can("dataconnect:schemas:edit");
  const { data: s, isLoading: sLoading, error: sError } = useGetSchemaQuery(id);
  const { data: driftData } = useGetSchemaDriftEventsQuery(id);
  const drift = driftData || [];
  const [detectSchemaDrift] = useDetectSchemaDriftMutation();
  const [rec, setRec] = useState(JSON.stringify([{ account_id: "1", name: "Acme", extra_field: "x" }], null, 2));
  const [out, setOut] = useState<SchemaDriftResult | null>(null);

  const detect = async () => {
    const records = parseJsonRecords(rec);
    if (!records.ok) { err(records.message); return; }
    try {
      const r = await detectSchemaDrift({ id, records: records.records }).unwrap();
      setOut(r);
      if (r.approval_required) ok("Drift needs approval — see Schemas list");
    } catch (e) { err(e); }
  };

  if (sLoading) return <Drawer title="Schema" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><Loading /></Drawer>;
  if (sError || !s) return <Drawer title="Schema" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><ErrBox error={sError} /></Drawer>;

  // schema_definition is free-form JSON on the wire; schemaFields.ts validates it into typed fields (bad entries are dropped).
  const fields = schemaFieldsOf(s.schema_definition);
  return (
    <Drawer title={`${s.domain}.${s.source_system}.${s.entity} · ${s.version}`} onClose={onClose}
      footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div className="fsec">Fields</div>
      <div className="tbl" role="table">
        <div className="thead" role="row" style={{ gridTemplateColumns: "1.2fr 0.7fr 0.5fr 0.9fr" }}><div className="th" role="columnheader">Field</div><div className="th" role="columnheader">Type</div><div className="th" role="columnheader">Null</div><div className="th" role="columnheader">Constraint</div></div>
        <div className="tbody" role="rowgroup">
          {fields.length ? fields.map((fl, i) => (
            <div className="tr" role="row" key={i} style={{ gridTemplateColumns: "1.2fr 0.7fr 0.5fr 0.9fr" }}>
              <div className="td" role="cell" style={{ fontFamily: "var(--font-mono)", fontSize: 11, fontWeight: 500 }}>{fl.name}</div>
              <div className="td" role="cell"><span className={`tag ${TYPE_TAG[fl.type as keyof typeof TYPE_TAG] || "tn"}`}>{fl.type}</span></div>
              <div className="tdm" role="cell">{fl.nullable ? "yes" : "no"}</div>
              <div className="td" role="cell"><span className={`tag ${fl.primary_key ? "tb2" : "tn"}`}>{fl.primary_key ? "primary key" : "—"}</span></div>
            </div>
          )) : <div className="empty">No fields</div>}
        </div>
      </div>
      {canManage && (
        <>
          <div className="fsec">Detect drift (paste observed records)</div>
          <div className="fld"><textarea style={{ height: 90 }} value={rec} onChange={(e) => setRec(e.target.value)} /></div>
          <button className="btn-s" style={{ marginTop: 8 }} onClick={detect}>Run drift detection</button>
        </>
      )}
      <div style={{ marginTop: 10 }}>
        {out ? (
          <>
            <div className="cs" style={{ marginBottom: 6 }}>compatible: <b>{String(out.compatible)}</b> · approval_required: <b>{String(out.approval_required)}</b>{out.approval_request_id ? " · approval " + out.approval_request_id : ""}</div>
            <CodeBlock value={out.events} />
          </>
        ) : (drift.length ? <CodeBlock value={drift} /> : null)}
      </div>
    </Drawer>
  );
}

export default function Schemas({ focus, clearFocus }: PageFocusProps) {
  const { openModal, closeModal, can } = useApp();
  const canManage = can("dataconnect:schemas:edit");
  const canApprove = can("dataconnect:schemas:approve-drift");
  const { data: schemasData, isLoading: loading, error } = useGetSchemasQuery();
  const { data: approvalsData } = useGetDriftApprovalsQuery();
  const approvals = approvalsData || [];
  const { data: pipesData } = useGetPipelinesQuery();

  // A schema picked in the top bar's search: open its details.
  useEffect(() => {
    if (focus?.kind !== "schema") return;
    openModal(<SchemaViewModal id={focus.id} onClose={closeModal} />);
    clearFocus?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, clearFocus]);

  if (loading) return <Loading />;
  if (error) return <ErrBox error={error} />;

  const schemas = schemasData || [];
  const pending = approvals.filter((a) => a.status === "pending");
  const pipelineNameById: Record<string, string> = {};
  (pipesData || []).forEach((p) => { pipelineNameById[p.id] = p.name; });
  // The pipelines that use each contract (a pipeline records the contract it registered or was given in schema_version_id).
  const pipelinesBySchema: Record<string, string[]> = {};
  (pipesData || []).forEach((p) => { if (p.schema_version_id) (pipelinesBySchema[p.schema_version_id] ??= []).push(p.name); });
  const contractById: Record<string, string> = {};
  schemas.forEach((s) => { contractById[s.id] = `${s.domain}.${s.source_system}.${s.entity} ${s.version}`; });

  // FE-BUG-18: the banner is just a one-line summary — clicking it (or
  // Review changes) opens SchemaDriftDialog for the actual field-by-field
  // diff, compatibility verdict, and affected pipeline before anyone
  // approves/rejects. Replaces both the raw drift_events JSON dump and the
  // window.prompt()-based approve/reject flow.
  const openDrift = (a: SchemaDriftApproval) => {
    openModal(<SchemaDriftDialog approval={a} pipelineName={a.pipeline_id ? pipelineNameById[a.pipeline_id] : undefined} onClose={closeModal} />);
  };

  return (
    <div className="page-fill">
      <PH title="Schemas" sub="Schema contracts, versions and drift approvals"
        right={canManage && <><button className="btn-s" onClick={() => openModal(<SchemaInfer onClose={closeModal} />)}>Infer</button><button className="btn-p" onClick={() => openModal(<SchemaRegister onClose={closeModal} />)}>+ Register schema</button></>} />
      <StatCards cards={[
        { k: "bl", l: "Total", v: schemas.length },
        { k: "gn", l: "Active", v: schemas.filter((s) => s.status === "active").length },
        { k: "am", l: "Drift pending", v: pending.length },
        { k: "bl", l: "Approvals", v: approvals.length },
      ]} />
      {/* One banner for every pending change, one compact row each, scrolling inside after about three rows: a banner per
          approval used to take the whole page, squeezing the schema table below to nothing. */}
      {pending.length > 0 && (
        <div className="alert alert-w">
          <div className="at" style={{ color: "#a8470a" }}>
            {pending.length === 1 ? "Schema change detected" : `${pending.length} schema changes detected`}
          </div>
          <div className="drift-list" role="list" aria-label="Pending schema changes">
            {pending.map((a) => {
              const lines = parseDriftLines(a.drift_events);
              const breaking = lines.some((l) => l.breaking);
              const pname = a.pipeline_id ? pipelineNameById[a.pipeline_id] : undefined;
              const contract = contractById[a.schema_version_id];
              return (
                <div className="drift-row" role="listitem" key={a.id}>
                  <div className="ab">
                    {contract && <span style={{ fontWeight: 500 }}>{contract}{pname ? ` — ${pname}` : ""} · </span>}
                    {!contract && pname && <span style={{ fontWeight: 500 }}>{pname} · </span>}
                    {lines.length} field{lines.length === 1 ? "" : "s"} changed · {breaking ? "⚠ Breaking change" : "✓ Compatible"}
                  </div>
                  {canApprove && (
                    <button className="btn-p" aria-label={`Review changes${contract ? ` to ${contract}` : ""}`} onClick={() => openDrift(a)}>Review changes</button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div className="tbl scroll" role="table" style={{ "--cols": "1.7fr 1fr 1.2fr 0.7fr 0.6fr 0.9fr 0.8fr 0.7fr" }}>
        <div className="thead" role="row"><div className="th" role="columnheader">Contract</div><div className="th" role="columnheader">Created at</div><div className="th" role="columnheader">Pipeline Name</div><div className="th" role="columnheader">Status</div><div className="th" role="columnheader">Version</div><div className="th" role="columnheader">Compatibility</div><div className="th" role="columnheader">Format</div><div className="th th-actions" role="columnheader">Actions</div></div>
        <div className="tbody" role="rowgroup">
          {schemas.length ? schemas.map((s) => (
            <div className={`tr ${s.status !== "active" ? "hl" : ""}`} role="row" key={s.id}>
              <div className="td" role="cell">
                {/* A long contract name (or id) is cut at the column edge with "…"; only a cut one shows its full text on hover. */}
                <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0, maxWidth: "100%" }}>
                  <CutText text={`${s.domain}.${s.source_system}.${s.entity}`} style={{ fontWeight: 500 }} />
                  <CutText text={s.id} style={{ fontSize: 10, color: "var(--color-text-secondary)" }} />
                </div>
              </div>
              <div className="tdm" role="cell"><CreatedAt value={s.created_at} /></div>
              <div className="td" role="cell"><PipelineNames names={pipelinesBySchema[s.id] ?? []} /></div>
              <div className="td" role="cell"><Pill cls={s.status === "active" ? "bg" : "ba"}>{s.status}</Pill></div>
              <div className="td" role="cell"><Pill cls="bgr">{s.version}</Pill></div>
              <div className="tdm" role="cell">{s.compatibility}</div>
              <div className="tdm" role="cell">{s.schema_format}</div>
              <div className="acts" role="cell"><button className="ai" title="View" aria-label="View" onClick={() => openModal(<SchemaViewModal id={s.id} onClose={closeModal} />)}><IconView /></button></div>
            </div>
          )) : <div className="empty">No schemas — register one</div>}
        </div>
      </div>
    </div>
  );
}
