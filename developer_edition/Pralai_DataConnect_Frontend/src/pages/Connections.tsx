import { ConnectorLogo } from "../components/ConnectorLogo";
import Select from "../components/Select";
import React, { useState, useEffect } from "react";
import { useApp } from "../context/AppContext";
import {
  useListConnectionConfigSchemasQuery, useGetConnectionsQuery, useLazyGetConnectionQuery,
  useCreateConnectionMutation, useUpdateConnectionMutation, useDeleteConnectionMutation,
  useTestConnectionLiveMutation, useTestConnectionPayloadMutation, useDiscoverConnectionMutation,
  useGetDiscoveryRunQuery, useGetDiscoveryObjectsQuery, useGetDiscoveryFacetsQuery, useGetPipelinesQuery,
} from "../store/apiSlice";
import { CreatedAt, PH, StatCards, Pill, Loading, ErrBox, Drawer, IconView, IconEdit, IconDel, IconSearch, IconTest, IconDiscover, FieldError } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import FailureDialog from "../components/FailureDialog";
import StillProcessingDialog from "../components/StillProcessingDialog";
import type { ConfigFieldSchema, Connection, ConnectionCreate } from "../types/api";
import { SECRET_MASK, buildConnectionConfig, changesHowItConnects, columnsFromFields, configOf, initialFieldValue, isSecretConfigured, latencySuffix } from "../connectionConfig";
import { errorMessage } from "../errors";
import { PIPE_BADGE } from "../constants";
import { useAppDispatch } from "../store/hooks";
import { pollDiscoveryRun } from "../store/pollDiscoveryRun";
import { explainCode, toFailureInfo } from "../errorCopy";
import { isSecretKey, REDACTED_PLACEHOLDER } from "../secretRedaction";

interface TestOut { spin?: boolean; cls?: string; badge?: string; text?: string; at?: number }
interface ConnListState { q: string; page: number }


// ── Create / edit form (schema-driven — see app/services/connection_config_schema.py) ──
//
// FE-BUG-11: a secret field's value is NEVER round-tripped through this
// form. On GET, the backend synthesizes SECRET_MASK for a configured secret
// field purely so this form can show *that something is set*, not what it
// is (secrets.py's own MASK/_connection_to_dict never expose the real
// value). Pre-filling an editable input with that literal string invited
// exactly the bug this ticket describes: a user editing an unrelated field
// (hostname) who never meant to touch the secret could still end up
// submitting SECRET_MASK back as if it were a real new value, or worse,
// partially edit it (e.g. append a character) into a corrupted secret that
// is neither the placeholder nor a real password.
//
// Instead, each secret field tracks its own explicit `secretChanged` state:
// until the user clicks "Change"/"Set", it renders as a read-only
// Configured/Not set indicator with no input at all — nothing to
// accidentally edit — and buildCfg() below omits that key from the
// submitted connection_config entirely unless secretChanged is true. That
// exactly matches the ticket's own model:
//   secretConfigured: existing has SECRET_MASK at this key
//   secretChanged:    the user opened this field's editor
//   newSecret:        vals[key], only meaningful (and only ever sent) when
//                     secretChanged is true
function ConnectionForm({ st, fields, existing, onClose }: { st: string; fields: ConfigFieldSchema[]; existing?: Connection; onClose: () => void }) {
  const { toast, ok, err } = useApp();
  const [createConnection] = useCreateConnectionMutation();
  const [updateConnection] = useUpdateConnectionMutation();
  const [testConnectionPayload] = useTestConnectionPayloadMutation();
  const [testConnectionLive] = useTestConnectionLiveMutation();
  const [name, setName] = useState(existing ? existing.name : "");
  const [secret, setSecret] = useState(existing?.secret_ref ?? "");
  const [vals, setVals] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f.key, initialFieldValue(f, existing)])));
  const [secretChanged, setSecretChanged] = useState<Record<string, boolean>>({});
  const [testOut, setTestOut] = useState<TestOut | null>(null);
  // Set when Save tried the connection with the changed settings and the source refused it: the reason, shown above the form with a
  // "Save anyway". Editing anything clears it, so the next Save tries the connection again.
  const [rejected, setRejected] = useState<string | null>(null);
  useEffect(() => { setRejected(null); }, [name, secret, vals, secretChanged]);
  // FE-BUG-21: which field (if any) is currently invalid, keyed so the
  // exact input can carry aria-invalid/aria-describedby instead of leaving
  // "what's wrong" to a toast alone — see submit()/buildCfg() below.
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null);
  const setV = (k: string, v: string) => {
    setVals((s) => ({ ...s, [k]: v }));
    setFieldError((fe) => (fe?.key === k ? null : fe));
  };
  const onNameChange = (v: string) => {
    setName(v);
    setFieldError((fe) => (fe?.key === "name" ? null : fe));
  };
  const secretConfigured = (f: ConfigFieldSchema) => isSecretConfigured(existing, f.key);
  const startChangingSecret = (key: string) => setSecretChanged((s) => ({ ...s, [key]: true }));
  const cancelChangingSecret = (key: string) => { setSecretChanged((s) => ({ ...s, [key]: false })); setV(key, ""); };

  const buildCfg = () => buildConnectionConfig(fields, vals, { existing: Boolean(existing), secretChanged });

  // What goes to the source to try the connection with: the current form values (see testForm for why a saved secret is passed by reference).
  const probeBody = (cfgValues: Record<string, unknown>): ConnectionCreate => {
    const body: ConnectionCreate = { name: name.trim() || "probe", source_type: st, connection_config: cfgValues };
    if (secret.trim()) body.secret_ref = secret.trim();
    else if (existing && fields.filter((f) => f.type === "secret").every((f) => !secretChanged[f.key])) {
      // FE-BUG-11: no secret field was touched, so built.cfg carries none of them at all — this is what actually reaches out to the source
      // system for the probe, so it still needs the REAL stored secret, not nothing. Passing the connection's own secret_ref lets the backend
      // transiently hydrate it in for this one probe only (secret_store.hydrate_connection_secrets) — never persisted, and never sent at all
      // if the user IS actively changing a secret (that would let the old stored value silently overwrite what they just typed in the test).
      body.secret_ref = existing.secret_ref;
    }
    return body;
  };

  const submit = async (saveAnyway = false) => {
    setFieldError(null);
    if (!name.trim()) { setFieldError({ key: "name", message: "Connection name is required" }); err("Connection name is required"); return; }
    const built = buildCfg();
    if ("error" in built) { setFieldError({ key: built.field, message: built.error }); err(built.error); return; }
    const body: ConnectionCreate = { name: name.trim(), source_type: st, connection_config: built.cfg };
    if (secret.trim()) body.secret_ref = secret.trim();
    // An edit that changes how the connection connects is tried against the source BEFORE it is saved: a wrong password used to be saved
    // with a plain "Connection updated" and only break the next deploy. A refusal stops the save and asks first ("Save anyway").
    let connectedOk = false;
    if (existing && !saveAnyway && changesHowItConnects(existing, built.cfg, secretChanged, secret)) {
      setRejected(null);
      setTestOut({ spin: true });
      let reason: string | null = null;
      try {
        const r = await testConnectionPayload({ body: probeBody(built.cfg), connectionId: existing.id }).unwrap();
        const ms = latencySuffix(r.details);
        setTestOut({ cls: r.status === "ok" ? "bg" : "br", badge: r.status, text: `${r.message}${ms}`, at: Date.now() });
        if (r.status !== "ok") reason = r.message;
        else connectedOk = true;
      } catch (e: unknown) {
        reason = errorMessage(e);
        setTestOut({ cls: "br", badge: "error", text: reason, at: Date.now() });
      }
      if (reason !== null) { setRejected(reason); return; }      // set after the test, so the effect above that clears it on edits does not undo it
    }
    try {
      if (existing) {
        const updated = await updateConnection({ id: existing.id, body }).unwrap();
        if (connectedOk) {
          // It connected with these settings just now: record that on the saved connection too, so it does not sit as "draft".
          try { await testConnectionLive(existing.id).unwrap(); } catch { /* the save stands; its status shows on the list */ }
          ok("Connection updated — connected ✓");
        }
        // GAP-02: an edit that changes how the connection connects (host, database, credentials, ...) makes its
        // last test result stale — say so, rather than leaving a confident "updated" next to an old green badge.
        else if (updated.verification_stale) toast(saveAnyway ? "Saved, but the source refused these settings — the connection won't work until they are fixed." : "Connection updated — its settings changed, so it needs testing again.", saveAnyway ? "err" : undefined);
        else ok("Connection updated");
      }
      else {
        const c = await createConnection(body).unwrap();
        if (c.status === "active") ok("Connection created — connected ✓");
        else toast(`Connection created, but could not connect (status: ${c.status})`, "err");
      }
      onClose();
    } catch (e) { err(e); }
  };

  const testForm = async () => {
    setFieldError(null);
    const built = buildCfg();
    if ("error" in built) {
      setFieldError({ key: built.field, message: built.error });
      setTestOut({ cls: "br", text: built.error.replace(" is required", " required") });
      return;
    }
    const body = probeBody(built.cfg);
    setTestOut({ spin: true });
    try {
      // existing?.id: purely so the audit trail can say which connection
      // this was, when it's an edit — still probes `body` (the current
      // form values), not the last-saved row.
      const r = await testConnectionPayload({ body, connectionId: existing?.id }).unwrap();
      const ms = latencySuffix(r.details);
      setTestOut({ cls: r.status === "ok" ? "bg" : "br", badge: r.status, text: `${r.message}${ms}`, at: Date.now() });
    } catch (e: unknown) { setTestOut({ cls: "br", badge: "error", text: errorMessage(e), at: Date.now() }); }
  };

  // Derived from the schema itself (any source type whose fields include a
  // "hostname") rather than a hardcoded list of "database-like" source
  // types — so a newly-added connector gets this tip automatically too.
  const dbHint = fields.some((f) => f.key === "hostname");
  const requiredLabels = fields.filter((f) => f.required).map((f) => f.label);
  const hint = requiredLabels.length ? `Requires: ${requiredLabels.join(", ")}.` : "";

  return (
    <Drawer title={`${existing ? "Edit" : "New"} ${st} connection`} onClose={onClose}
      footer={<><button className="btn-s" onClick={onClose}>Cancel</button><button className="btn-s" onClick={testForm}>Test connection</button><button className="btn-p" onClick={() => submit(rejected !== null)}>{rejected !== null ? "Save anyway" : existing ? "Save changes" : "Create connection"}</button></>}>
      {rejected !== null && (
        <div className="alert alert-e" role="alert" style={{ marginBottom: 12 }}>
          <strong>These settings were not saved — the source refused them.</strong>
          <div style={{ marginTop: 4 }}>{rejected}</div>
          <div className="ab" style={{ marginTop: 6 }}>Fix them and save again, or choose “Save anyway” to keep them as they are (pipelines using this connection will fail to connect).</div>
        </div>
      )}
      <div className="fsec">Basics</div>
      <div className="fg">
        <div className="fld">
          <label htmlFor="conn-name">Connection name *</label>
          <input
            id="conn-name" value={name} placeholder={`my-${st}`} onChange={(e) => onNameChange(e.target.value)}
            aria-invalid={fieldError?.key === "name"}
            aria-describedby={fieldError?.key === "name" ? "conn-name-error" : undefined}
          />
          {fieldError?.key === "name" && <FieldError id="conn-name-error" message={fieldError.message} />}
        </div>
      </div>
      {existing && (
        // Read-only. Prefers a fresh in-session "Test connection" result
        // (testOut) over the connection's durably-saved verification state:
        // testForm tests the CURRENT form values, which the backend never
        // persists back onto the connection row (that only happens via a
        // Save, or the table row's own "Test" action — see testForm's own
        // comment on why) — so without this, clicking "Test connection"
        // here looked like it did nothing, since this block kept showing
        // whatever was last durably saved instead of the result you just got.
        <div className="fld" style={{ marginBottom: 12 }}>
          <label>Verification</label>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {testOut?.at ? (
              <>
                <Pill cls={testOut.cls || "bgr"}>{(testOut.badge || "error").toUpperCase()}</Pill>
                <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>
                  Just tested {new Date(testOut.at).toLocaleString()} — current form values, not yet saved
                </span>
              </>
            ) : (
              <>
                <Pill cls={existing.verification_status === "ok" ? "bg" : existing.verification_status === "error" ? "br" : "bgr"}>
                  {(existing.verification_status || "unverified").toUpperCase()}
                </Pill>
                {existing.last_tested_at && (
                  <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>
                    Last tested {new Date(existing.last_tested_at).toLocaleString()}
                  </span>
                )}
                {existing.latency_ms != null && (
                  <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>· {existing.latency_ms} ms</span>
                )}
                {existing.error_code && <Pill cls="br">{existing.error_code}</Pill>}
                {existing.verification_stale && <Pill cls="bgr">settings changed since this test</Pill>}
              </>
            )}
          </div>
          {testOut?.at ? (
            <div className="ab" style={{ marginTop: 4 }}>{testOut.text}</div>
          ) : existing.last_test_message && (
            <div className="ab" style={{ marginTop: 4 }}>{existing.last_test_message}</div>
          )}
        </div>
      )}
      <div className="fld" style={{ marginBottom: 12 }}><label htmlFor="conn-secret-ref">Secret ref (optional)</label><input id="conn-secret-ref" value={secret} placeholder={`vault://dataconnect/<org>/${st}`} onChange={(e) => setSecret(e.target.value)} /></div>
      <div className="fsec">{st} connection parameters</div>
      <div className="fg">
        {fields.map((f) => {
          const inputId = `conn-field-${f.key}`;
          const errorId = `${inputId}-error`;
          const invalid = fieldError?.key === f.key;
          return (
          <div className="fld" key={f.key}>
            <label htmlFor={inputId}>{f.label}{f.required ? " *" : ""}</label>
            {f.type === "secret" && existing && !secretChanged[f.key] ? (
              // FE-BUG-11: a read-only indicator, not a form field — there
              // is genuinely nothing safe to put in an input here (the real
              // value never reaches this form at all). "Change"/"Set" is
              // the one explicit, unambiguous way to signal intent to
              // replace it.
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Pill cls={secretConfigured(f) ? "bg" : "bgr"}>{secretConfigured(f) ? "Configured" : "Not set"}</Pill>
                {/* aria-label, not just the label's htmlFor association —
                    otherwise the field label text ("Password *") would
                    override this button's own visible text ("Change"/
                    "Set") as its computed accessible name. */}
                <button
                  type="button" id={inputId} className="btn-s" style={{ height: 26, padding: "0 10px", fontSize: 11 }}
                  onClick={() => startChangingSecret(f.key)}
                  aria-label={`${secretConfigured(f) ? "Change" : "Set"} ${f.label}`}
                >
                  {secretConfigured(f) ? "Change" : "Set"}
                </button>
              </div>
            ) : f.type === "secret" && existing ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  id={inputId} type="password" placeholder={f.placeholder || "New value"} value={vals[f.key] ?? ""}
                  onChange={(e) => setV(f.key, e.target.value)} autoFocus
                  aria-invalid={invalid} aria-describedby={invalid ? errorId : undefined}
                />
                <button type="button" className="btn-s" style={{ height: 26, padding: "0 10px", fontSize: 11 }} onClick={() => cancelChangingSecret(f.key)}>Cancel</button>
              </div>
            ) : f.type === "enum" ? (
              <Select id={inputId} value={vals[f.key] ?? ""} onChange={(e) => setV(f.key, e.target.value)} aria-invalid={invalid} aria-describedby={invalid ? errorId : undefined}>
                <option value="">—</option>
                {(f.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
              </Select>
            ) : f.type === "boolean" ? (
              <input
                id={inputId}
                type="checkbox" style={{ width: "auto", height: "auto" }}
                checked={vals[f.key] === "true"}
                onChange={(e) => setV(f.key, e.target.checked ? "true" : "false")}
              />
            ) : (
              <input
                id={inputId}
                type={f.type === "secret" ? "password" : f.type === "number" ? "number" : "text"}
                placeholder={f.placeholder || ""} value={vals[f.key] ?? ""}
                onChange={(e) => setV(f.key, e.target.value)}
                aria-invalid={invalid} aria-describedby={invalid ? errorId : undefined}
              />
            )}
            {invalid && <FieldError id={errorId} message={fieldError.message} />}
            {f.description && <div className="ab" style={{ marginTop: 2 }}>{f.description}</div>}
          </div>
          );
        })}
      </div>
      <div className="ab" style={{ marginTop: 6 }}>{hint}</div>
      {dbHint && <div className="ab" style={{ marginTop: 4 }}>Host tip: the connection is made from the DataConnect backend, so <b>never use localhost</b>. Local setup: use <b>host.docker.internal</b> for a database on your machine, or the database's Docker service name (e.g. <b>postgres</b>). Server: use the database server's host name or IP address (e.g. <b>db.mycompany.internal</b> or <b>10.0.12.5</b>).</div>}
      <div style={{ marginTop: 12, minHeight: 18 }}>
        {testOut && (testOut.spin
          ? <span><span className="spin" /> testing connectivity…</span>
          : <span><span className={`badge ${testOut.cls}`}>{testOut.badge || testOut.text}</span>{testOut.badge ? <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}> {testOut.text}</span> : null}</span>)}
      </div>
    </Drawer>
  );
}

// GAP-10/FE-BUG-12: a large source's discovery run can return tens of
// thousands of objects (e.g. 5,000 tables) — this pages through GET
// /discovery/{id}/objects (search + object_type/schema_name filters + cursor,
// all applied server-side) instead of dumping everything the backend found
// into one JSON blob or one unbounded React list.
function DiscoverModal({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [search, setSearch] = useState("");
  const [objectType, setObjectType] = useState("");
  const [schemaName, setSchemaName] = useState("");
  const [cursorStack, setCursorStack] = useState<(string | undefined)[]>([undefined]);
  const cursor = cursorStack[cursorStack.length - 1];
  const { data: run } = useGetDiscoveryRunQuery(runId);
  const { data: facets } = useGetDiscoveryFacetsQuery(runId);
  const { data: page, isFetching } = useGetDiscoveryObjectsQuery({
    id: runId,
    search: search || undefined,
    objectType: objectType || undefined,
    schemaName: schemaName || undefined,
    limit: 50,
    cursor,
  });

  const objects = page?.objects || [];
  const resetToFirstPage = () => setCursorStack([undefined]);
  const hasPrev = cursorStack.length > 1;
  const hasNext = !!page?.next_cursor;

  return (
    <Drawer title="Discovered objects" onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, gap: 8, flexWrap: "wrap" }}>
        <div className="srch"><IconSearch /><input placeholder="Search discovered objects…" value={search} onChange={(e) => { setSearch(e.target.value); resetToFirstPage(); }} /></div>
        <Select aria-label="Filter by object type" value={objectType} onChange={(e) => { setObjectType(e.target.value); resetToFirstPage(); }}>
          <option value="">All types</option>
          {(facets?.object_types || []).map((t) => <option key={t} value={t}>{t}</option>)}
        </Select>
        <Select aria-label="Filter by schema" value={schemaName} onChange={(e) => { setSchemaName(e.target.value); resetToFirstPage(); }}>
          <option value="">All schemas</option>
          {(facets?.schema_names || []).map((s) => <option key={s} value={s}>{s}</option>)}
        </Select>
        {run && <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>{run.object_count}{run.truncated ? "+" : ""} total</span>}
      </div>
      {run?.error_message && <div className="ab" style={{ marginBottom: 10 }}>{run.error_message}</div>}
      {run?.truncated && (
        // GAP-10: the source exposed more than the backend keeps per run — say so rather than imply this is everything.
        <div className="ab" role="status" style={{ marginBottom: 10 }}>
          This source has more objects than a discovery run keeps. Showing the first {run.object_count.toLocaleString()} —
          use search and the schema filter to find a specific one.
        </div>
      )}
      <div role="table" style={{ "--cols": "0.7fr 1.3fr" } as React.CSSProperties}>
        <div className="thead" role="row">
          <div className="th" role="columnheader">Type</div>
          <div className="th" role="columnheader">Name</div>
        </div>
        <div className="tbody" role="rowgroup">
          {objects.length ? objects.map((o) => (
            <div className="tr" role="row" key={o.id}>
              <div className="td" role="cell">{o.object_type}</div>
              <div className="td" role="cell">{o.schema_name ? `${o.schema_name}.${o.name}` : o.name}</div>
            </div>
          )) : <div className="empty">{isFetching ? "Loading…" : "No matching objects."}</div>}
        </div>
      </div>
      {(hasPrev || hasNext) && (
        <div className="pager" style={{ marginTop: 8 }}>
          <button className="pgbtn" disabled={!hasPrev} onClick={() => setCursorStack((s) => s.slice(0, -1))}>‹ Prev</button>
          <button className="pgbtn" disabled={!hasNext} onClick={() => setCursorStack((s) => [...s, page!.next_cursor as string])}>Next ›</button>
        </div>
      )}
    </Drawer>
  );
}

// FE-BUG-19: a genuine read-only detail view — every config field as a
// plain label/value row, not a form (disabled or otherwise). ConnDetail's
// own table only shows up to 2 "notable" columns (columnsFromFields) for
// space; this is the full field list, for anyone with dataconnect:connections:view
// — not gated to canManage the way the actual
// edit form is.
function ConnectionDetailModal({ st, fields, c, onClose }: { st: string; fields: ConfigFieldSchema[]; c: Connection; onClose: () => void }) {
  const cfg = configOf(c);
  // The pipelines that read from or write to this connection (each pipeline records its connection profile).
  const { data: allPipelines, isLoading: pipelinesLoading } = useGetPipelinesQuery();
  const using = (allPipelines ?? []).filter((p) => p.connection_profile_id === c.id);
  const displayValue = (f: ConfigFieldSchema): React.ReactNode => {
    if (f.type === "secret") {
      const configured = cfg[f.key] === SECRET_MASK;
      return <Pill cls={configured ? "bg" : "bgr"}>{configured ? "Configured" : "Not set"}</Pill>;
    }
    const v = cfg[f.key];
    // FE-BUG-09: defense-in-depth — a secret-shaped key the schema didn't type
    // as "secret" is still never shown as a value.
    if (isSecretKey(f.key) && v != null && v !== "") return REDACTED_PLACEHOLDER;
    if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
    return v == null || v === "" ? "—" : String(v);
  };
  return (
    <Drawer title={c.name} onClose={onClose} footer={<button className="btn-s" onClick={onClose}>Close</button>}>
      <div className="kv"><span className="kvk">Source</span><span className="kvv">{st}</span></div>
      <div className="kv"><span className="kvk">Status</span><span className="kvv"><Pill cls={c.status === "active" ? "bg" : "bgr"}>{c.status}</Pill></span></div>
      <div className="kv"><span className="kvk">Created at</span><span className="kvv"><CreatedAt value={c.created_at} /></span></div>
      {fields.map((f) => (
        <div className="kv" key={f.key}><span className="kvk">{f.label}</span><span className="kvv">{displayValue(f)}</span></div>
      ))}
      <div className="fsec" style={{ marginTop: 14 }}>Pipelines using this connection{pipelinesLoading ? "" : ` (${using.length})`}</div>
      {pipelinesLoading ? <Loading /> : using.length === 0 ? (
        <div className="empty" style={{ padding: "12px 0" }}>No pipeline uses this connection yet</div>
      ) : (
        <div role="list" aria-label="Pipelines using this connection">
          {using.map((p) => (
            <div className="kv" role="listitem" key={p.id}>
              <span className="kvk" style={{ color: "var(--color-text-primary)", fontWeight: 500 }}>{p.name}</span>
              <span className="kvv"><Pill cls={PIPE_BADGE[(p.status ?? "draft") as keyof typeof PIPE_BADGE] || "bb"}>{p.status ?? "draft"}</Pill></span>
            </div>
          ))}
        </div>
      )}
    </Drawer>
  );
}

// ── Level 2 detail table ─────────────────────────────────────────────────────
interface ConnDetailProps {
  st: string;
  fields: ConfigFieldSchema[];
  all: Connection[];
  cs: ConnListState;
  setCs: React.Dispatch<React.SetStateAction<ConnListState>>;
  onCreate: (st: string) => void;
  onEdit: (id: string) => void;
  onTest: (id: string) => void;
  onDiscover: (id: string) => void;
  onDelete: (id: string, name?: string) => void;
}
function ConnDetail({ st, fields, all, cs, setCs, onCreate, onEdit, onTest, onDiscover, onDelete }: ConnDetailProps) {
  const { can, openModal, closeModal } = useApp();
  const canManage = can("dataconnect:connections:edit");
  const canDelete = can("dataconnect:connections:edit");
  const canTest = can("dataconnect:connections:test");
  const canDiscover = can("dataconnect:connections:test");
  const cols = columnsFromFields(fields);
  const total = all.filter((c) => c.source_type === st).length;
  let list = all.filter((c) => c.source_type === st);
  if (cs.q) {
    const q = cs.q.toLowerCase();
    list = list.filter((c) => (c.name || "").toLowerCase().includes(q) || (c.id || "").toLowerCase().includes(q) || JSON.stringify(c.connection_config || {}).toLowerCase().includes(q));
  }
  // The list is shown in the order the backend sends: newest first (by Created at). No column re-sorts it, so every heading looks alike.
  // Every connection is on this one page: the card's header and the column titles stay put and the rows scroll
  // (see .card-fit / .tbl-fill in styles.css), instead of paging through 10 at a time.
  const rows = list;
  const colsVar = "1.4fr 1.1fr 1.3fr 1.3fr 0.8fr 1.2fr";

  return (
    <div className="card card-fit">
      <div className="ch">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}><div className="ct">All connections</div><span className="badge bb">{total}</span></div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="srch"><IconSearch /><input placeholder="Search..." value={cs.q} onChange={(e) => setCs((s) => ({ ...s, q: e.target.value, page: 0 }))} /></div>
          {canManage && <button className="iconadd" title={`New ${st} connection`} aria-label={`New ${st} connection`} onClick={() => onCreate(st)}>+</button>}
        </div>
      </div>
      <div role="table" className="tbl-fill" style={{ "--cols": colsVar }}>
        <div className="thead" role="row">
          {cols.map((col, i) => <div className="th" role="columnheader" key={i}>{col.label}</div>)}
          <div className="th th-actions" role="columnheader">Actions</div>
        </div>
        <div className="tbody" role="rowgroup">
          {rows.length ? rows.map((c) => (
            <div className="tr" role="row" key={c.id}>
              {cols.map((col, i) => col.label === "Status"
                ? <div className="td" role="cell" key={i}><Pill cls={c.status === "active" ? "bg" : "bgr"}>{c.status}</Pill></div>
                : col.label === "Created at"
                  ? <div className="tdm" role="cell" key={i}><CreatedAt value={c.created_at} /></div>
                  : <div className="td" role="cell" key={i} style={col.label === "Friendly Name" ? { fontWeight: 500 } : undefined}>{col.get(c)}</div>)}
              <div className="acts" role="cell">
                <button className="ai" title="View" aria-label={`View ${c.name}`} onClick={() => openModal(<ConnectionDetailModal st={st} fields={fields} c={c} onClose={closeModal} />)}><IconView /></button>
                {canTest && <button className="ai" title="Test" aria-label={`Test ${c.name}`} onClick={() => onTest(c.id)}><IconTest /></button>}
                {canDiscover && <button className="ai" title="Discover" aria-label={`Discover ${c.name}`} onClick={() => onDiscover(c.id)}><IconDiscover /></button>}
                {canManage && <button className="ai" title="Edit" aria-label={`Edit ${c.name}`} onClick={() => onEdit(c.id)}><IconEdit /></button>}
                {canDelete && <button className="ai danger" title="Delete" aria-label={`Delete ${c.name}`} onClick={() => onDelete(c.id, c.name)}><IconDel /></button>}
              </div>
            </div>
          )) : <div className="empty">{canManage ? <>No {st} connections yet — click <b>+</b> to add one.</> : `No ${st} connections yet.`}</div>}
        </div>
      </div>
    </div>
  );
}

export default function Connections({ focus, clearFocus }: PageFocusProps) {
  const { openModal, closeModal, ok, err, toast } = useApp();
  const dispatch = useAppDispatch();
  const { data: connsData, isLoading: loading, error } = useGetConnectionsQuery();
  // FE-BUG-10: the field metadata that drives the create/edit form and the
  // list table's columns — one backend definition per source type (app/
  // services/connection_config_schema.py), not a hardcoded per-type branch
  // in this file. Adding a new source type is now a backend data change.
  const { data: schemasData, isLoading: schemasLoading, error: schemasError } = useListConnectionConfigSchemasQuery();
  const [fetchConnection] = useLazyGetConnectionQuery();
  const [deleteConnection] = useDeleteConnectionMutation();
  const [testConnectionLive] = useTestConnectionLiveMutation();
  const [discoverConnection] = useDiscoverConnectionMutation();
  const [src, setSrc] = useState<string | null>(null);
  const [cs, setCs] = useState<ConnListState>({ q: "", page: 0 });

  const back = () => { setSrc(null); };

  // A connection picked in the top bar's search: open its source type's list with the search box already on its name.
  useEffect(() => {
    if (focus?.kind !== "connection") return;
    setSrc(focus.sourceType ?? null);
    setCs({ q: focus.name, page: 0 });
    clearFocus?.();
  }, [focus, clearFocus]);

  const openSource = (st: string) => { setSrc(st); setCs({ q: "", page: 0 }); };

  const schemas = schemasData || [];
  const sourceTypes = schemas.map((s) => s.source_type).sort();
  const fieldsFor = (st: string): ConfigFieldSchema[] => schemas.find((s) => s.source_type === st)?.fields || [];

  const onCreate = (st: string) => openModal(<ConnectionForm st={st} fields={fieldsFor(st)} onClose={closeModal} />, { wide: false });
  const onEdit = async (id: string) => {
    try {
      const c = await fetchConnection(id).unwrap();
      openModal(<ConnectionForm st={c.source_type} fields={fieldsFor(c.source_type)} existing={c} onClose={closeModal} />);
    } catch (e) { err(e); }
  };
  const onTest = async (id: string) => { try { const r = await testConnectionLive(id).unwrap(); const ms = latencySuffix(r.details); toast(`${r.status.toUpperCase()} — ${r.message}${ms}`, r.status === "ok" ? "ok" : "err"); } catch (e) { err(e); } };
  // Discovery is live network I/O against the source system with no bound on
  // how long it takes, and can turn up far more objects than belong in one
  // response — the backend returns 202 + a queued DiscoveryRun (GAP-10)
  // instead of the objects themselves (see api.ts's own header comment);
  // submit, watch the run to its terminal status, then let DiscoverModal page
  // through the actual results.
  //
  // FE-BUG-05: the browser giving up watching is not "discovery failed" —
  // pollDiscoveryRun reports that as a PollOutcome, never a thrown timeout,
  // so it renders as StillProcessingDialog (with a way to keep watching the
  // same run) rather than FailureDialog.
  const watchDiscovery = async (connectionId: string, runId: string) => {
    const outcome = await pollDiscoveryRun(dispatch, runId);
    if (outcome.kind === "polling_timeout" || outcome.kind === "network_disconnected") {
      const conn = conns.find((c) => c.id === connectionId);
      openModal(
        <StillProcessingDialog
          action="Discovery"
          reason={outcome.kind}
          context={conn ? { Connection: conn.name } : undefined}
          onContinueMonitoring={() => { closeModal(); watchDiscovery(connectionId, runId); }}
          onClose={closeModal}
        />,
      );
      return;
    }
    const run = outcome.value;
    if (run.status === "failed") {
      const conn = conns.find((c) => c.id === connectionId);
      openModal(
        <FailureDialog
          title="Discovery failed"
          message={run.error_message || "Discovery failed"}
          retryable={false}
          context={conn ? { Connection: conn.name } : undefined}
          onClose={closeModal}
        />,
      );
      return;
    }
    openModal(<DiscoverModal runId={run.id} onClose={closeModal} />);
  };
  const onDiscover = async (id: string) => {
    try {
      const submitted = await discoverConnection(id).unwrap();
      await watchDiscovery(id, submitted.id);
    } catch (e) {
      const info = toFailureInfo(e);
      if (explainCode(info.code)) {
        const conn = conns.find((c) => c.id === id);
        openModal(
          <FailureDialog
            title="Discovery failed"
            {...info}
            context={conn ? { Connection: conn.name } : undefined}
            onRetry={info.retryable ? () => { closeModal(); onDiscover(id); } : undefined}
            onClose={closeModal}
          />,
        );
      } else err(e);
    }
  };
  const onDelete = async (id: string, name?: string) => { if (!window.confirm(`Delete connection "${name}"?`)) return; try { await deleteConnection(id).unwrap(); ok("Deleted"); } catch (e) { err(e); } };

  if (loading || schemasLoading) return <Loading />;
  if (error) return <ErrBox error={error} />;
  if (schemasError) return <ErrBox error={schemasError} />;

  const conns = connsData || [];
  const byType: Record<string, number> = {};
  conns.forEach((c) => { byType[c.source_type] = (byType[c.source_type] || 0) + 1; });

  if (src) {
    return (
      <div className="page-fill">
        <PH title={src} sub="" right={<button className="btn-s" onClick={back}>← All types</button>} />
        <ConnDetail st={src} fields={fieldsFor(src)} all={conns} cs={cs} setCs={setCs} onCreate={onCreate} onEdit={onEdit} onTest={onTest} onDiscover={onDiscover} onDelete={onDelete} />
      </div>
    );
  }

  return (
    <>
      <PH title="Connections" sub="Select a source type to view or create connections" />
      <StatCards cards={[
        { k: "bl", l: "Total connections", v: conns.length },
        { k: "gn", l: "Types in use", v: Object.keys(byType).length },
        { k: "am", l: "Available types", v: sourceTypes.length },
      ]} />
      <div className="card">
        <div className="ch"><div><div className="ct">Connection types</div><div className="cs">Click a type — the count is shown on each</div></div></div>
        <div className="cb">
          <div className="conn-grid">
            {sourceTypes.map((st) => (
              <button className="ctile" key={st} onClick={() => openSource(st)} aria-label={`${st} — ${byType[st] || 0} connections`}>
                <div className="ccnt" style={byType[st] ? { background: "var(--color-error)", color: "#fff" } : { background: "#cfd8d1" }}>{byType[st] || 0}</div>
                <div className="ccirc"><ConnectorLogo type={st} /></div>
                <div className="clbl">{st}</div>
              </button>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
