import React, { useEffect, useMemo, useState } from "react";
import Select from "../components/Select";
import { useApp } from "../context/AppContext";
import {
  useGetQualityPoliciesQuery, useGetQualityCapabilitiesQuery, useGetQualityStreamStatsQuery, useGetFailureBreakdownQuery,
  useGetQualityPolicyQuery, useGetPipelinesQuery,
  useCreateQualityPolicyMutation, useDeleteQualityPolicyMutation, useValidateQualityPolicyMutation,
  useExecuteQualityPolicyMutation,
} from "../store/apiSlice";
import { redactedInlineJson, isSecretKey, REDACTED_PLACEHOLDER } from "../secretRedaction";
import { CreatedAt, IconSearch, PH, StatCards, Pill, Loading, ErrBox, Drawer, CodeBlock, IconView, IconDel, IconCheck, IconRun, FieldError } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import { QRULE_SPEC, RULE_TYPES } from "../constants";
import { firstFieldError } from "../errors";
import { buildRulePayload, describeRuleExpectation, parseQualityRules, ruleFields, ruleSpecFor, typedSampleValue, type QualityRule, type RuleParamValue } from "../qualityRules";
import { qualityFailuresOf } from "../qualityFailures";
import { defaultChoice, isSelectable, optionLabel, qualityCapabilitiesOf } from "../availability";
import AvailabilityNote from "../components/AvailabilityNote";
import type { FailureBreakdown, QualityExecuteResult, QualityFailureEnvelope } from "../types/api";
import { relativeTime } from "../runtimeState";
import { usePolling, useOnVisible } from "../hooks/usePolling";
import { usePersistedChoice } from "../hooks/usePersistedChoice";

const RULE_KEYS = Object.keys(QRULE_SPEC) as (keyof typeof QRULE_SPEC)[];
let ruleSeq = 0;

interface RuleRow { _id: number; field: string; type: string; params: Record<string, string> }

function QualityForm({ onClose }: { onClose: () => void }) {
  const { toast, ok, err } = useApp();
  const [createQualityPolicy] = useCreateQualityPolicyMutation();
  const [name, setName] = useState("");
  const [onFail, setOnFail] = useState("route_to_quarantine");
  const [sink, setSink] = useState("kafka");
  // NEW-09: what a policy can actually do comes from the backend. Until it answers (or if it cannot) only the options that have always worked are
  // offered, so the form never lets a user pick something the API would refuse.
  const { data: capabilities } = useGetQualityCapabilitiesQuery();
  const { quarantine_sinks: quarantineSinks, failure_actions: failureActions } = useMemo(() => qualityCapabilitiesOf(capabilities), [capabilities]);
  const sinkChoice = quarantineSinks.find((o) => o.id === sink);
  const onFailChoice = failureActions.find((o) => o.id === onFail);
  useEffect(() => {
    // If what is selected is not usable (the answer just arrived, or changed), move to one that is rather than submit a choice the API refuses.
    if (!sinkChoice || !isSelectable(sinkChoice.availability)) setSink(defaultChoice(quarantineSinks, (o) => o.availability) ?? "kafka");
    if (!onFailChoice || !isSelectable(onFailChoice.availability)) setOnFail(defaultChoice(failureActions, (o) => o.availability) ?? "route_to_quarantine");
  }, [sinkChoice, onFailChoice, quarantineSinks, failureActions]);
  const [rules, setRules] = useState<RuleRow[]>([{ _id: ++ruleSeq, field: "", type: "required", params: {} }]);
  // FE-BUG-21: which field is invalid, so the input itself carries
  // aria-invalid/aria-describedby instead of leaving "what's wrong" to the
  // toast alone.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);

  const addRule = () => setRules((r) => [...r, { _id: ++ruleSeq, field: "", type: "required", params: {} }]);
  const removeRule = (id: number) => setRules((r) => r.filter((x) => x._id !== id));
  const setRule = (id: number, patch: Partial<RuleRow>) => setRules((r) => r.map((x) => (x._id === id ? { ...x, ...patch } : x)));
  const setParam = (id: number, k: string, v: string) => setRules((r) => r.map((x) => (x._id === id ? { ...x, params: { ...x.params, [k]: v } } : x)));

  const submit = async () => {
    const built: Record<string, RuleParamValue>[] = [];
    rules.forEach((row) => {
      const field = (row.field || "").trim();
      if (!field) return;
      built.push(buildRulePayload(row.type, field, row.params));
    });
    setFieldError(null);
    if (!name.trim()) { setFieldError({ key: "name", message: "Give the quality check a name." }); toast("Give the quality check a name.", "err"); return; }
    if (!built.length) { toast("Add at least one rule with a field.", "err"); return; }
    try {
      await createQualityPolicy({ name: name.trim(), on_failure: onFail, quarantine_sink: sink, rules: built }).unwrap();
      onClose(); ok(`Quality check created (${built.length} rules)`);
    } catch (e: unknown) {
      const fe = firstFieldError(e);
      if (fe) setFieldError({ key: fe.field, message: fe.message });
      err(e);
    }
  };

  return (
    <Drawer title="New Quality Check" onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-p" onClick={submit}>Save Quality Check</button></>}>
      <div className="fg">
        <div className="fld">
          <label htmlFor="quality-name">Quality Check Name</label>
          <input
            id="quality-name" value={name} placeholder="e.g. Customer PII validation"
            onChange={(e) => { setName(e.target.value); setFieldError((fe) => (fe?.key === "name" ? null : fe)); }}
            aria-invalid={fieldError?.key === "name"} aria-describedby={fieldError?.key === "name" ? "quality-name-error" : undefined}
          />
          {fieldError?.key === "name" && <FieldError id="quality-name-error" message={fieldError.message} />}
        </div>
        <div className="fld">
          <label htmlFor="quality-on-failure">On failure</label>
          <Select id="quality-on-failure" value={onFail} onChange={(e) => setOnFail(e.target.value)}
            aria-describedby={onFailChoice && onFailChoice.availability.state !== "supported" ? "quality-on-failure-availability" : undefined}>
            {failureActions.map((o) => (
              <option key={o.id} value={o.id} disabled={!isSelectable(o.availability)} title={o.description} data-availability={o.availability.state}>
                {optionLabel(o.label, o.availability)}
              </option>
            ))}
          </Select>
        </div>
        <div className="fld">
          <label htmlFor="quality-quarantine-sink">Quarantine destination</label>
          <Select id="quality-quarantine-sink" value={sink} onChange={(e) => setSink(e.target.value)}
            aria-describedby={sinkChoice && sinkChoice.availability.state !== "supported" ? "quality-quarantine-sink-availability" : undefined}>
            {quarantineSinks.map((o) => (
              <option key={o.id} value={o.id} disabled={!isSelectable(o.availability)} title={o.description} data-availability={o.availability.state}>
                {optionLabel(o.label, o.availability)}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <AvailabilityNote id="quality-on-failure-availability" availability={onFailChoice?.availability} />
      <AvailabilityNote id="quality-quarantine-sink-availability" availability={sinkChoice?.availability} />
      <div className="fsec">Rules — one check per field</div>
      <div>
        {rules.map((row) => {
          const spec = ruleSpecFor(row.type);
          return (
            <div key={row._id} style={{ border: "1px solid var(--color-border-primary)", borderRadius: 6, padding: "9px 10px", marginBottom: 8 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
                <div className="fld" style={{ flex: 1 }}><label htmlFor={`quality-rule-field-${row._id}`}>Field</label><input id={`quality-rule-field-${row._id}`} value={row.field} placeholder="e.g. email" onChange={(e) => setRule(row._id, { field: e.target.value })} /></div>
                <div className="fld" style={{ flex: 1.4 }}><label htmlFor={`quality-rule-check-${row._id}`}>Check</label>
                  <Select id={`quality-rule-check-${row._id}`} value={row.type} onChange={(e) => setRule(row._id, { type: e.target.value, params: {} })}>
                    {RULE_KEYS.map((t) => <option key={t} value={t}>{t} — {QRULE_SPEC[t].label}</option>)}
                  </Select>
                </div>
                <button className="ai" title="Remove rule" aria-label={`Remove rule${row.field ? ` for ${row.field}` : ""}`} onClick={() => removeRule(row._id)} style={{ marginBottom: 6, cursor: "pointer" }}>✕</button>
              </div>
              <div style={{ marginTop: 6 }}>
                {spec.params.length ? (
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {spec.params.map((p) => (
                      <div className="fld" key={p.k} style={{ flex: 1, minWidth: 140 }}>
                        <label htmlFor={`quality-rule-param-${row._id}-${p.k}`}>{p.label}</label>
                        {p.t === "select"
                          ? <Select id={`quality-rule-param-${row._id}-${p.k}`} value={row.params[p.k] ?? p.opts[0] ?? ""} onChange={(e) => setParam(row._id, p.k, e.target.value)}>{p.opts.map((o) => <option key={o}>{o}</option>)}</Select>
                          : <input id={`quality-rule-param-${row._id}-${p.k}`} value={row.params[p.k] ?? ""} placeholder={p.ph} onChange={(e) => setParam(row._id, p.k, e.target.value)} />}
                      </div>
                    ))}
                  </div>
                ) : <div className="ab">No extra parameters for this check.</div>}
              </div>
            </div>
          );
        })}
      </div>
      <button className="btn-s" onClick={addRule}>+ Add rule</button>
      <div className="ab" style={{ marginTop: 6 }}>Pick a field and a check; fill only the boxes shown. The rules JSON is built for you.</div>
    </Drawer>
  );
}

function QualityExec({ id, onClose }: { id: string; onClose: () => void }) {
  const { toast, err } = useApp();
  const { data: pol, isLoading: polLoading, error: polError } = useGetQualityPolicyQuery(id);
  const { data: pipesData, isLoading: pipesLoading } = useGetPipelinesQuery();
  const [executeQualityPolicy] = useExecuteQualityPolicyMutation();
  const pipes = pipesData || [];
  const [pipe, setPipe] = useState("");
  const [rows, setRows] = useState<Record<string, string>[]>([{}, {}]);
  const [out, setOut] = useState<QualityExecuteResult | null>(null);

  if (polLoading || pipesLoading) return <Drawer title="Execute quality check" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><Loading /></Drawer>;
  if (polError || !pol) return <Drawer title="Execute quality check" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><ErrBox error={polError} /></Drawer>;

  const fields = ruleFields(parseQualityRules(pol.rules));
  const curPipe = pipe || (pipes[0] && pipes[0].id) || "";

  const setCell = (i: number, f: string, v: string) => setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, [f]: v } : r)));
  const addRow = () => setRows((rs) => [...rs, {}]);
  const removeRow = (i: number) => setRows((rs) => rs.filter((_, idx) => idx !== i));

  const run = async () => {
    if (!curPipe) { toast("Pick a pipeline first (create one under Pipelines if none exist).", "err"); return; }
    if (!rows.length) { toast("Add at least one record.", "err"); return; }
    const records = rows.map((row) => {
      const rec: Record<string, string | number | boolean> = {};
      fields.forEach((f) => {
        const v = (row[f] ?? "").trim();
        if (v === "") return;
        rec[f] = typedSampleValue(v);
      });
      return rec;
    });
    try {
      const r = await executeQualityPolicy({ id, body: { pipeline_id: curPipe, records } }).unwrap();
      setOut(r);
      toast(`Executed → ${r.status} · ${r.failed_count}/${r.total_count} failed`, r.status === "passed" ? "ok" : "err");
    } catch (e) { err(e); }
  };

  return (
    <Drawer title={`Execute quality check — ${pol.name}`} onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Close</button><button className="btn-p" onClick={run}>Execute</button></>}>
      <div className="fld" style={{ marginBottom: 12 }}><label htmlFor="quality-exec-pipeline">Pipeline</label>
        <Select id="quality-exec-pipeline" value={curPipe} onChange={(e) => setPipe(e.target.value)}>
          {pipes.length ? pipes.map((p) => <option key={p.id} value={p.id}>{p.name}</option>) : <option value="">— no pipelines —</option>}
        </Select>
      </div>
      <div className="fsec">Sample records to test (columns = fields this check tests)</div>
      <div style={{ display: "flex", gap: 6, padding: "0 2px 4px" }}>
        {fields.map((f) => <div key={f} style={{ flex: 1, fontSize: 10, color: "var(--color-text-secondary)", fontWeight: 500 }}>{f}</div>)}
        <div style={{ width: 26 }} />
      </div>
      <div>
        {rows.map((row, i) => (
          <div key={i} style={{ display: "flex", gap: 6, marginBottom: 5, alignItems: "center" }}>
            {fields.map((f) => <input key={f} value={row[f] ?? ""} placeholder={f} style={{ flex: 1 }} onChange={(e) => setCell(i, f, e.target.value)} className="" />)}
            <button className="ai" onClick={() => removeRow(i)} title="Remove record" aria-label={`Remove record ${i + 1}`} style={{ width: 26, cursor: "pointer" }}>✕</button>
          </div>
        ))}
      </div>
      <button className="btn-s" onClick={addRow}>+ Add record</button>
      <div className="ab" style={{ marginTop: 6 }}>Leave a box blank to simulate a missing value. Numbers &amp; true/false are auto-typed. The JSON is built for you.</div>
      {out && (
        <div style={{ marginTop: 10 }}>
          <div className="cs" style={{ marginBottom: 6 }}><Pill cls={out.status === "passed" ? "bg" : "br"}>{out.status}</Pill> <b>{out.failed_count}/{out.total_count}</b> failed</div>
          {out.sample_failures && out.sample_failures.length
            ? <><div className="ab" style={{ margin: "6px 0 4px" }}>Failing records &amp; which rules they broke:</div><CodeBlock value={out.sample_failures} /></>
            : <div className="ab">All records passed ✓</div>}
        </div>
      )}
    </Drawer>
  );
}

function QualityFailuresModal({ pipelineName, policyRules, sampleFailures, onClose, description, pipelineOf }: {
  /** What the drawer is titled after: one pipeline, or one quality check. */
  pipelineName: string; policyRules: QualityRule[]; sampleFailures: QualityFailureEnvelope[]; onClose: () => void;
  /** Replaces the default one-pipeline explanation (used when the records come from several pipelines). */
  description?: string;
  /** Given, each record says which pipeline it came from — for the records of a check that several pipelines share. */
  pipelineOf?: (failure: QualityFailureEnvelope) => string | undefined;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);
  // Newest first — sample_failures is appended to in arrival order.
  const rows = [...sampleFailures].map((f, i) => ({ f, i })).reverse();
  const ruleByName: Record<string, QualityRule> = Object.fromEntries(policyRules.map((r) => [r.name, r]));

  return (
    <Drawer title={`Failed records — ${pipelineName}`} onClose={onClose}
      footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div className="ab" style={{ marginBottom: 8 }}>
        {description ?? "The most recent failing records the live enforcement worker has seen for this pipeline (bounded sample — older ones roll off). Click a record to see exactly what each rule expected vs. what it actually got."}
      </div>
      {rows.length ? rows.map(({ f, i }) => {
        const isOpen = expanded === i;
        return (
          <div key={i} style={{ border: "1px solid var(--color-border-primary)", borderRadius: 8, marginBottom: 8, overflow: "hidden" }}>
            <div
              className="click"
              style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, padding: "9px 12px", cursor: "pointer" }}
              onClick={() => setExpanded(isOpen ? null : i)}
            >
              <div style={{ minWidth: 0 }}>
                {pipelineOf && <div style={{ fontSize: 11, fontWeight: 600, color: "var(--color-primary)", marginBottom: 1 }}>{pipelineOf(f) ?? "unknown pipeline"}</div>}
                <div style={{ fontSize: 12, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis" }}>{f.original_topic}</div>
                {f.original_partition !== undefined && f.original_offset !== undefined && (
                  <div className="ab" style={{ marginTop: 2 }}>partition {f.original_partition} · offset {f.original_offset}</div>
                )}
                <div className="ab" style={{ marginTop: 2 }}>{new Date(f.received_at).toLocaleString()}</div>
              </div>
              <Pill cls="br">{f.failed_rules.length} rule{f.failed_rules.length === 1 ? "" : "s"} broken</Pill>
            </div>
            {isOpen && (
              <div style={{ padding: "0 12px 12px" }}>
                <div className="ab" style={{ margin: "2px 0 6px" }}>Why it failed:</div>
                {f.failed_rules.map((ruleName) => {
                  const rule = ruleByName[ruleName];
                  const field = rule?.field;
                  const actual = field ? f.original_record[field] : undefined;
                  return (
                    <div key={ruleName} style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 12, padding: "5px 0", borderTop: "1px solid var(--color-border-tertiary)" }}>
                      <span><b>{field || ruleName}</b> — {describeRuleExpectation(rule)}</span>
                      <span className="mono" style={{ color: "var(--color-text-secondary)", flexShrink: 0 }}>
                        actual: {field ? (isSecretKey(field) ? REDACTED_PLACEHOLDER : JSON.stringify(actual)) : "—"}
                      </span>
                    </div>
                  );
                })}
                <div className="ab" style={{ margin: "10px 0 4px" }}>Full record:</div>
                <CodeBlock value={f} />
              </div>
            )}
          </div>
        );
      }) : <div className="empty">No failures recorded yet</div>}
    </Drawer>
  );
}

function QualityViewModal({ id, onClose }: { id: string; onClose: () => void }) {
  const { data: q, isLoading: loading, error } = useGetQualityPolicyQuery(id);
  if (loading) return <Drawer title="Quality check" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><Loading /></Drawer>;
  if (error || !q) return <Drawer title="Quality check" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}><ErrBox error={error} /></Drawer>;
  const rules = parseQualityRules(q.rules);
  return (
    <Drawer title={q.name} onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div className="fsec">On failure: {q.on_failure}</div>
      {rules.length ? rules.map((r, i) => {
        return (
          <div className="smt" key={i} style={{ gridTemplateColumns: "22px 1fr 1.3fr" }}>
            <div className="smt-n">{i + 1}</div>
            <div><div className="smt-name">{r.name || r.type}</div><div className="smt-type">{r.type}{r.field ? " · " + r.field : ""}</div></div>
            <span className="smt-cfg">{redactedInlineJson(r.extra)}</span>
          </div>
        );
      }) : <div className="empty">No rules</div>}
    </Drawer>
  );
}

// FE-BUG-17: two separate groups, never one "Quarantine" number — a validation
// failure is a DataConnect quality rule rejecting a record (fix the data or the
// rule); an ingestion failure is Kafka Connect not being able to ingest it at
// all (fix the format, schema or connector). Same time window for both.
function FailureBreakdownPanels({ data, error, onOpenCheck }: {
  data: FailureBreakdown | undefined; error: unknown;
  /** Opens the failed records of a quality check; returns false when there are none to show (then the row is not a button). */
  onOpenCheck?: (check: { quality_policy_id?: string | null; name: string; pipelines?: string[] }) => (() => void) | undefined;
}) {
  // The generated schema marks defaulted fields optional; the backend always sends them.
  const v = { total: data?.validation?.total ?? 0, by_check: data?.validation?.by_check ?? [] };
  const ing = {
    total: data?.ingestion?.total ?? 0, unclassified: data?.ingestion?.unclassified ?? 0,
    by_category: data?.ingestion?.by_category ?? [], recent: data?.ingestion?.recent ?? [],
  };
  return (
    <>
        {error ? <ErrBox error={error} /> : !data ? <Loading /> : (
          <div className="qpanels">
            <section className="qpanel" aria-label="Validation failures">
              <div className="fsec">Validation failures <Pill cls={v.total > 0 ? "br" : "bg"}>{v.total}</Pill></div>
              <div className="ab" style={{ marginBottom: 6 }}>A quality check rejected the record. Counted per check, each record once, however many of its rules it broke.</div>
              <div className="qpanel-body">
              {v.by_check.length ? v.by_check.map((c) => {
                const open = onOpenCheck?.(c);
                return (
                <div
                  className="hrow" key={c.quality_policy_id ?? c.name}
                  title={open ? "View failed records" : c.pipelines?.length ? `Pipelines: ${c.pipelines.join(", ")}` : undefined}
                  style={open ? { cursor: "pointer" } : undefined}
                  role={open ? "button" : undefined} tabIndex={open ? 0 : undefined} onClick={open}
                  onKeyDown={open ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } } : undefined}
                >
                  <span style={{ fontSize: 12, minWidth: 0 }}>
                    <span style={{ fontWeight: 500 }}>{c.name}</span>
                    {c.pipelines && c.pipelines.length > 0 && (
                      <span className="tdm" style={{ padding: 0, display: "block", fontSize: 10.5 }}>
                        {c.pipelines.slice(0, 2).join(", ")}{c.pipelines.length > 2 ? ` +${c.pipelines.length - 2}` : ""}
                      </span>
                    )}
                  </span>
                  <Pill cls="br">{c.count}</Pill>
                </div>
                );
              }) : <div className="empty">No validation failures</div>}
              </div>
            </section>
            <section className="qpanel" aria-label="Ingestion failures">
              <div className="fsec">Ingestion failures <Pill cls={ing.total > 0 ? "br" : "bg"}>{ing.total}</Pill></div>
              <div className="ab" style={{ marginBottom: 6 }}>Kafka Connect could not ingest the record (its dead-letter queue).</div>
              <div className="qpanel-body">
              {ing.by_category.length ? ing.by_category.map((c) => (
                <div className="hrow" key={c.category}><span style={{ fontSize: 12 }}>{c.label}</span><Pill cls="br">{c.count}</Pill></div>
              )) : <div className="empty">No ingestion failures</div>}
              {ing.unclassified > 0 && (
                <div className="ab" style={{ marginTop: 4 }}>{ing.unclassified} of {ing.total} records are still being classified — causes will catch up shortly.</div>
              )}
              {ing.recent.length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary style={{ cursor: "pointer", fontSize: 12 }}>Recent ingestion errors ({ing.recent.length})</summary>
                  {ing.recent.map((r, i) => (
                    <div key={i} style={{ fontSize: 11, padding: "5px 0", borderTop: "1px solid var(--color-border-tertiary)" }}>
                      <b>{r.pipeline_name}</b> · {r.stage ?? "unknown stage"} · {relativeTime(r.at)}
                      <div className="mono" style={{ color: "var(--color-text-secondary)", wordBreak: "break-word" }}>
                        {r.exception_class ?? ""}{r.message ? ` — ${r.message}` : ""}
                      </div>
                    </div>
                  ))}
                </details>
              )}
              </div>
            </section>
          </div>
        )}
    </>
  );
}

// The results' time range: how far back the failure counts go (sent to the backend as seconds).
const FAILURE_RANGES = [
  { id: "15m", seconds: 15 * 60 }, { id: "1h", seconds: 3600 }, { id: "6h", seconds: 6 * 3600 }, { id: "24h", seconds: 24 * 3600 },
] as const;
type FailureRange = (typeof FAILURE_RANGES)[number]["id"];

const IconFilter = () => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ display: "block" }}>
    <path d="M3 5h18l-7 8v6l-4 2v-8z" />
  </svg>
);

/** The policy list's filter: a button that opens a small panel to narrow the list to one "on failure" action. Lit while a filter is on. */
function PolicyFilter({ actions, value, onChange }: { actions: string[]; value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <div className="qfilter" ref={ref}>
      <button type="button" className={`ai${value ? " on" : ""}`} aria-label="Filter quality checks" aria-haspopup="true" aria-expanded={open}
        title={value ? `Filtered: on failure ${value}` : "Filter quality checks"} onClick={() => setOpen((o) => !o)}><IconFilter /></button>
      {open && (
        <div className="qfilter-pop" role="group" aria-label="Filter by what happens on failure">
          <div className="qfilter-h">On failure</div>
          {["", ...actions].map((a) => (
            <label key={a || "all"} className="qfilter-opt">
              <input type="radio" name="quality-on-failure-filter" checked={value === a} onChange={() => { onChange(a); setOpen(false); }} />
              <span>{a || "All"}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Quality({ focus, clearFocus }: PageFocusProps) {
  const { openModal, closeModal, toast, ok, err, can } = useApp();
  const canManage = can("dataconnect:quality:edit");
  const { data: polsData, isLoading: polsLoading, error: polsError } = useGetQualityPoliciesQuery();
  const { data: pipesData } = useGetPipelinesQuery();
  const pipes = pipesData || [];

  // A policy picked in the top bar's search: open its details.
  useEffect(() => {
    if (focus?.kind !== "policy") return;
    setTab("policies");
    openModal(<QualityViewModal id={focus.id} onClose={closeModal} />);
    clearFocus?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, clearFocus]);

  // Live per-pipeline totals from the continuous enforcement worker — the
  // real, ongoing per-record evaluation, not a one-off Execute-button run.
  // Polled the same way Pipelines.tsx polls pipeline status: paused while
  // the tab is hidden, backed off while the backend is failing.
  // Which tab, which view inside Results and which range are chosen survives a refresh (hooks/usePersistedChoice.ts).
  const [tab, setTab] = usePersistedChoice<"policies" | "results">("dc_quality_tab", ["policies", "results"], "policies");
  const [q, setQ] = useState("");                          // the policy search
  const [onFail, setOnFail] = useState("");                // the policy filter: "" = every "on failure" action
  const [sub, setSub] = usePersistedChoice<"validation" | "history">("dc_quality_view", ["validation", "history"], "validation");   // the two views inside the Results tab
  const [range, setRange] = usePersistedChoice<FailureRange>("dc_quality_range", FAILURE_RANGES.map((r) => r.id), "1h");  // the results' "Last 15m / 1h / 6h / 24h"
  const [pollingInterval, reportPollResult] = usePolling({ baseMs: 5000 });
  const { data: streamStatsData, error: streamStatsError, refetch: refetchStreamStats } =
    useGetQualityStreamStatsQuery(undefined, { pollingInterval });
  const streamStats = streamStatsData || [];
  useEffect(() => { reportPollResult(streamStatsError); }, [streamStatsError, reportPollResult]);
  const { data: breakdown, error: breakdownError, refetch: refetchBreakdown } =
    useGetFailureBreakdownQuery(FAILURE_RANGES.find((r) => r.id === range)!.seconds, { pollingInterval });
  useOnVisible(() => { refetchStreamStats(); refetchBreakdown(); });

  const [validateQualityPolicy] = useValidateQualityPolicyMutation();
  const [deleteQualityPolicy] = useDeleteQualityPolicyMutation();

  if (polsLoading) return <Loading />;
  if (polsError) return <ErrBox error={polsError} />;

  const pols = polsData || [];
  const validate = async (id: string) => { try { const r = await validateQualityPolicy(id).unwrap(); toast(r.valid ? "Quality check valid ✓" : "Invalid: " + (r.errors || []).join(", "), r.valid ? "ok" : "err"); } catch (e) { err(e); } };
  const remove = async (id: string, name: string) => { if (!window.confirm(`Delete "${name}"?`)) return; try { await deleteQualityPolicy(id).unwrap(); ok("Quality check deleted"); } catch (e) { err(e); } };

  const needle = q.trim().toLowerCase();
  const shown = pols.filter((x) => (!onFail || x.on_failure === onFail) && (!needle || `${x.name} ${x.on_failure}`.toLowerCase().includes(needle)));
  const actions = [...new Set(pols.map((x) => x.on_failure).filter((a): a is string => Boolean(a)))].sort();
  const newPolicy = () => openModal(<QualityForm onClose={closeModal} />);

  // A quality check's failed records: those of every pipeline that runs it, inside the range chosen above (the same window the count is of), newest
  // first — what History shows for one pipeline, put together for the check. Nothing to show means the row is not clickable.
  const openCheck = (check: { quality_policy_id?: string | null; name: string; pipelines?: string[] }) => {
    const names = new Set(check.pipelines ?? []);
    const owners = pipes.filter((p) => (check.quality_policy_id ? p.quality_policy_id === check.quality_policy_id : names.has(p.name)));
    const ownerIds = new Set(owners.map((p) => p.id));
    const since = Date.now() - FAILURE_RANGES.find((r) => r.id === range)!.seconds * 1000;
    const pipelineName = (id: string) => pipes.find((p) => p.id === id)?.name;
    const failures = streamStats
      .filter((s) => ownerIds.has(s.pipeline_id))
      .flatMap((s) => qualityFailuresOf(s.sample_failures).map((f) => ({ ...f, pipeline_id: s.pipeline_id })))
      .filter((f) => new Date(f.received_at).getTime() >= since)
      .sort((a, b) => new Date(a.received_at).getTime() - new Date(b.received_at).getTime());      // oldest first: the drawer shows newest first
    if (!failures.length) return undefined;
    const contributing = owners.filter((p) => failures.some((f) => f.pipeline_id === p.id));      // only those that actually had failures in the range
    const rules = parseQualityRules(pols.find((p) => p.id === check.quality_policy_id)?.rules);
    return () => openModal(
      <QualityFailuresModal
        pipelineName={check.name} policyRules={rules} sampleFailures={failures} onClose={closeModal} pipelineOf={(f) => pipelineName(f.pipeline_id)}
        description={`The most recent failing records in the last ${range} from ${contributing.length === 1 ? "the pipeline" : `the ${contributing.length} pipelines`} that run${contributing.length === 1 ? "s" : ""} this quality check (${contributing.map((p) => p.name).join(", ")}) — a bounded sample, older ones roll off. Click a record to see exactly what each rule expected vs. what it actually got.`}
      />,
    );
  };
  // History: the pipelines checked inside the chosen range (a result with no date is kept — nothing says it is old), newest first.
  const historySince = Date.now() - FAILURE_RANGES.find((r) => r.id === range)!.seconds * 1000;
  const historyRows = [...streamStats]
    .filter((s) => !(Date.parse(s.updated_at ?? "") < historySince))
    .sort((a, b) => Date.parse(b.updated_at ?? "") - Date.parse(a.updated_at ?? ""));
  const subKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const next = e.key === "ArrowRight" || e.key === "End" ? "history" : "validation";
    setSub(next);
    document.getElementById(`quality-sub-${next}`)?.focus();
  };
  const tabKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const next = e.key === "ArrowRight" || e.key === "End" ? "results" : "policies";
    setTab(next);
    document.getElementById(`quality-tab-${next}`)?.focus();
  };

  return (
    // Title, cards and tabs stay put; the active tab's content scrolls below them. The policies table keeps its column header in place and
    // scrolls its own rows after about five.
    <div className="page-fill">
      <PH title="Quality" sub="Define rules, run validation, review results"
        right={canManage && <button className="btn-p" onClick={newPolicy}>+ New Quality Check</button>} />
      <StatCards cards={[
        { k: "bl", l: "Quality Checks", v: pols.length },
        { k: "gn", l: "Results logged", v: streamStats.length },
        { k: "rd", l: "Validation failures (total)", v: streamStats.reduce((a, s) => a + (s.failed_count ?? 0), 0) },
        { k: "bl", l: "Rule types", v: RULE_TYPES.length },
      ]} />
      {/* One row: the sections on the left and, only while Results is open, its two views (Validation / History) on the right, like the model catalog's
          category filter next to its tabs. Quality Checks has no views, so nothing is on the right. */}
      <div className="qtabrow">
        <div className="dc-tabs qtabs" role="tablist" aria-label="Quality sections" onKeyDown={tabKey}>
          {([["policies", "Quality Checks"], ["results", "Results"]] as const).map(([id, label]) => (
            <button
              key={id} id={`quality-tab-${id}`} type="button" role="tab" aria-selected={tab === id} aria-controls={`quality-panel-${id}`}
              tabIndex={tab === id ? 0 : -1} onClick={() => setTab(id)}
            >{label}</button>
          ))}
        </div>
        {tab === "results" && (
          <div className="dc-tabs qtabs qsub" role="tablist" aria-label="Results views" onKeyDown={subKey}>
            {([["validation", "Validation"], ["history", "History"]] as const).map(([id, label]) => (
              <button
                key={id} id={`quality-sub-${id}`} type="button" role="tab" aria-selected={sub === id} aria-controls={`quality-subpanel-${id}`}
                tabIndex={sub === id ? 0 : -1} onClick={() => setSub(id)}
              >{label}</button>
            ))}
          </div>
        )}
      </div>
      <div className={`page-scroll${tab === "results" ? " page-fixed" : ""}`}>
        {tab === "policies" ? (
          <div className="card qcard" role="tabpanel" id="quality-panel-policies" aria-labelledby="quality-tab-policies">
            <div className="ch qh">
              <div style={{ minWidth: 0 }}>
                <div className="ct qh-t">Quality Checks</div>
                <div className="cs">Manage your data quality checks, define rules and configure validations.</div>
              </div>
              <div className="qh-r">
                <div className="srch"><IconSearch /><input aria-label="Search quality checks" placeholder="Search quality checks..." value={q} onChange={(e) => setQ(e.target.value)} /></div>
                <PolicyFilter actions={actions} value={onFail} onChange={setOnFail} />
              </div>
            </div>
            <div className="tbl tbl-capped flat" role="table" style={{ "--cols": "1.6fr 1fr 0.7fr 1fr 1.2fr" }}>
              <div className="thead" role="row"><div className="th" role="columnheader">Quality Check Name</div><div className="th" role="columnheader">Created at</div><div className="th" role="columnheader">Rules</div><div className="th" role="columnheader">On failure</div><div className="th th-actions" role="columnheader">Actions</div></div>
              <div className="tbody" role="rowgroup">
            {shown.length ? shown.map((q) => (
              <div className="tr" role="row" key={q.id}>
                <div className="td" role="cell" style={{ fontWeight: 500 }}>{q.name}</div>
                <div className="tdm" role="cell"><CreatedAt value={q.created_at} /></div>
                <div className="td" role="cell"><Pill cls="bb">{(q.rules || []).length} rules</Pill></div>
                <div className="td" role="cell"><Pill cls="ba">{q.on_failure}</Pill></div>
                <div className="acts" role="cell">
                  {canManage && <button className="ai" title="Validate" aria-label={`Validate ${q.name}`} onClick={() => validate(q.id)}><IconCheck /></button>}
                  {canManage && <button className="ai" title="Execute" aria-label={`Execute ${q.name}`} onClick={() => openModal(<QualityExec id={q.id} onClose={closeModal} />)}><IconRun /></button>}
                  <button className="ai" title="View" aria-label={`View ${q.name}`} onClick={() => openModal(<QualityViewModal id={q.id} onClose={closeModal} />)}><IconView /></button>
                  {canManage && <button className="ai" title="Delete" aria-label={`Delete ${q.name}`} onClick={() => remove(q.id, q.name)}><IconDel /></button>}
                </div>
              </div>
            )) : <div className="empty">{pols.length ? "No quality checks match" : "No quality checks yet"}</div>}
              </div>
            </div>
          </div>
        ) : (
          <div role="tabpanel" id="quality-panel-results" aria-labelledby="quality-tab-results" className="q-results-fill">
            {sub === "validation" && (
            <div className="card card-fill" id="quality-subpanel-validation" role="tabpanel" aria-labelledby="quality-sub-validation">
              <div className="ch qh">
                <div style={{ minWidth: 0 }}>
                  <div className="ct qh-t">Results</div>
                  <div className="cs">View and analyze recent quality validation results.</div>
                </div>
                <div className="qh-r">
                  <label htmlFor="quality-range" className="sr-only">Time range</label>
                  <Select id="quality-range" value={range} onChange={(e) => setRange(e.target.value as FailureRange)} className="qrange">
                    {FAILURE_RANGES.map((r) => <option key={r.id} value={r.id}>Last {r.id}</option>)}
                  </Select>
                </div>
              </div>
              <div className="cb cb-fill"><FailureBreakdownPanels data={breakdown} error={breakdownError} onOpenCheck={openCheck} /></div>
            </div>
            )}
        {sub === "history" && (
        <div className="card card-fill" id="quality-subpanel-history" role="tabpanel" aria-labelledby="quality-sub-history">
          <div className="ch qh">
            <div style={{ minWidth: 0 }}>
              <div className="ct qh-t">Recent quality results</div>
              <div className="cs">Pipelines that were checked in the chosen range; click one to see its failed records from that range.</div>
            </div>
            <div className="qh-r">
              <label htmlFor="quality-history-range" className="sr-only">Time range</label>
              {/* The same range as Validation's (one choice, remembered): both views always talk about the same window. */}
              <Select id="quality-history-range" value={range} onChange={(e) => setRange(e.target.value as FailureRange)} className="qrange">
                {FAILURE_RANGES.map((r) => <option key={r.id} value={r.id}>Last {r.id}</option>)}
              </Select>
            </div>
          </div>
          <div className="cb cb-fill cb-list">
            {/* Live, ongoing per-record totals from the real enforcement
                worker — not the Execute button's own history (that tests
                arbitrary, often hand-typed sample records against a policy
                and has no relationship to what's actually flowing through a
                pipeline); see QualityStreamStats's doc comment. */}
            {historyRows.length ? historyRows.map((s) => {   // newest first: the pipelines that failed just now are on top
              const pipe = pipes.find((p) => p.id === s.pipeline_id);
              const pipeName = pipe ? pipe.name : s.pipeline_id;
              const policy = pols.find((p) => p.id === s.quality_policy_id);
              const failed = s.failed_count ?? 0;
              const total = s.total_count ?? 0;
              const sampleFailures = qualityFailuresOf(s.sample_failures).filter((f) => !(new Date(f.received_at).getTime() < historySince));   // only the range's failed records
              const clickable = failed > 0 && sampleFailures.length > 0;
              const openFailures = clickable
                ? () => openModal(<QualityFailuresModal pipelineName={pipeName} policyRules={parseQualityRules(policy?.rules)} sampleFailures={sampleFailures} onClose={closeModal} />)
                : undefined;
              return (
                <div
                  className="hrow"
                  key={s.pipeline_id}
                  style={clickable ? { cursor: "pointer" } : undefined}
                  title={clickable ? "View failed records" : undefined}
                  // A clickable row must also be reachable and operable from the keyboard.
                  role={clickable ? "button" : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  onClick={openFailures}
                  onKeyDown={clickable ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openFailures?.(); } } : undefined}
                >
                  <span style={{ fontSize: 12 }}>{pipeName}</span>
                  <span><Pill cls={failed > 0 ? "br" : "bg"}>{failed > 0 ? "failed" : "passed"}</Pill> <span className="tdm" style={{ padding: 0 }}>{failed}/{total} failed</span></span>
                </div>
              );
            }) : <div className="empty">{streamStats.length ? `No pipeline was checked in the last ${range} — pick a longer range` : "No results yet — running pipeline hasn't evaluated any records"}</div>}
          </div>
        </div>
        )}
          </div>
        )}
      </div>
    </div>
  );
}
