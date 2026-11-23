/**
 * DatabaseConnectionModal — add / edit a database connection, in the
 * right-side Drawer like every other form in the Workspace.
 * Opened from Object Explorer ("Add connection") or from the
 * connections list (edit icon).
 *
 * Calls POST /connections (create) or PATCH /connections/{id} (edit).
 * After create, triggers a connection test automatically.
 *
 * Edit mode prefills from the connection list. The connection type can't be
 * changed (PATCH doesn't accept it), and credentials are never returned by
 * the API — they live in the secrets vault — so the credential fields start
 * empty and are only sent when the user enters new ones.
 *
 * "Unified Catalog source" links the connection to the UCS catalog for the
 * same database, so the Object Explorer browses what UCS governs. Left on
 * auto-detect, the backend matches it by database/port/host on save.
 */
import { useEffect, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import {
  useListConnectionsQuery,
  useListUcsCatalogsQuery,
  useCreateConnectionMutation,
  useUpdateConnectionMutation,
  useTestConnectionMutation,
} from "../../../api/client";
import { closeConnectionModal, notify } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { ConnectionType, UcsCatalogOption } from "../../../types";
import { apiErrorMessage } from "../../../api/errors";
import { Drawer, Field } from "../../../shared/ui/Drawer";
import { drawerBtn, drawerInput as inp } from "../../../shared/ui/drawerStyles";
import SearchSelect from "../../../shared/ui/SearchSelect";
import { Glyph } from "../../../shared/ui/Glyph";

interface ConnTypeOption {
  id: ConnectionType; label: string; icon: string; desc: string; defaultPort: number; offered?: boolean;
  /** Example text for the name and host boxes, in this type's own words. */
  nameExample: string; hostExample: string;
}

// `offered: false` keeps a type out of the "add" list while the connections that already have it (and the backend, which still
// accepts it) keep working: their edit form looks the type up here.
const CONN_TYPES: ConnTypeOption[] = [
  { id: "trino", label: "Iceberg", icon: "ti-database", desc: "Query Iceberg tables (Polaris REST catalog)", defaultPort: 443,
    nameExample: "e.g. production-iceberg", hostExample: "iceberg.yourcompany.com" },
  { id: "pg_lake", label: "pg_lake", icon: "ti-database", desc: "Postgres with pg_lake Iceberg extension (DuckDB engine)", defaultPort: 5432, offered: false,
    nameExample: "e.g. lake-postgres", hostExample: "pg-lake.yourcompany.com" },
  { id: "postgres", label: "PostgreSQL", icon: "ti-database", desc: "Standard Postgres — ad hoc SQL only", defaultPort: 5432,
    nameExample: "e.g. analytics-postgres", hostExample: "postgres.yourcompany.com" },
  { id: "flink", label: "FlinkSQL", icon: "ti-wave-sine", desc: "Flink SQL gateway — streaming jobs (Phase 3)", defaultPort: 8083,
    nameExample: "e.g. streaming-flink", hostExample: "flink-gateway.yourcompany.com" },
  { id: "other", label: "Custom", icon: "ti-plug", desc: "Custom JDBC-compatible data source", defaultPort: 0,
    nameExample: "e.g. reporting-database", hostExample: "db.yourcompany.com" },
];

const isPg = (t: ConnectionType) => t === "postgres" || t === "pg_lake";
// Most Postgres servers (and every local docker one) don't speak TLS; Trino
// endpoints are usually HTTPS.
const defaultTLS = (t: ConnectionType) => t === "trino";

/** UCS catalogs that make sense for this connection type. */
function catalogsFor(t: ConnectionType, all: UcsCatalogOption[]) {
  if (isPg(t)) return all.filter((c) => c.engine === "relational");
  if (t === "trino") return all.filter((c) => c.engine === "iceberg");
  return all;
}

export function DatabaseConnectionModal() {
  const dispatch = useDispatch<AppDispatch>();
  const editingId = useSelector((s: RootState) => s.ui.editingConnectionId);
  const isEdit = !!editingId;
  const { data: connections, isLoading: listLoading } = useListConnectionsQuery(undefined, { skip: !isEdit });
  const existing = isEdit ? connections?.find((c) => c.id === editingId) : undefined;
  const { data: ucsCatalogs } = useListUcsCatalogsQuery();

  const [type, setType] = useState<ConnectionType>("trino");
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState<number>(443);
  const [database, setDatabase] = useState("");
  const [catalog, setCatalog] = useState("iceberg");  // trino is the initial type
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [useTLS, setUseTLS] = useState(true);
  const [desc, setDesc] = useState("");
  // Prefill once — a background refetch mustn't clobber in-progress edits.
  const [prefilled, setPrefilled] = useState(false);
  const [step, setStep] = useState<"form" | "testing" | "result">("form");
  const [testOk, setTestOk] = useState<boolean | null>(null);
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const [createConn, { isLoading: creating }] = useCreateConnectionMutation();
  const [updateConn, { isLoading: updating }] = useUpdateConnectionMutation();
  const [testConn] = useTestConnectionMutation();

  useEffect(() => {
    if (!existing || prefilled) return;
    setType(existing.connection_type);
    setName(existing.name);
    setHost(existing.host ?? "");
    setPort(existing.port ?? CONN_TYPES.find((c) => c.id === existing.connection_type)?.defaultPort ?? 0);
    setDatabase(existing.database_name ?? "");
    setCatalog(existing.catalog_name ?? "");
    setUseTLS(existing.use_tls);
    setDesc(existing.description ?? "");
    setPrefilled(true);
  }, [existing, prefilled]);

  const close = () => dispatch(closeConnectionModal());

  const handleTypeChange = (t: ConnectionType) => {
    setType(t);
    const meta = CONN_TYPES.find(c => c.id === t);
    if (meta) setPort(meta.defaultPort);
    setUseTLS(defaultTLS(t));
    setCatalog(t === "trino" ? "iceberg" : "");
  };

  const options = catalogsFor(type, ucsCatalogs ?? []);
  const linked = options.find((c) => c.name === catalog);
  // A stored name UCS doesn't have (e.g. the old hidden "iceberg" default on
  // a Postgres connection) shows as auto-detect, and is re-detected on save.
  // Trino keeps free text: its catalog name is also what queries use.
  const catalogValue = type === "trino" || linked || !ucsCatalogs?.length ? catalog : "";

  const pickCatalog = (name: string) => {
    setCatalog(name);
    const c = options.find((o) => o.name === name);
    if (!c) return;
    // Fill what the user hasn't typed. Host is left alone: UCS may reach the
    // server under a hostname this service can't resolve (docker networks).
    if (c.database && (!database || !isEdit)) setDatabase(c.database);
    if (c.port) setPort(c.port);
    if (c.username && !username && !isEdit) setUsername(c.username);
  };

  const runTest = async (id: string) => {
    setStep("testing");
    try {
      const result = await testConn(id).unwrap();
      setTestOk(result.success);
      setTestMsg(result.success
        ? `Connected successfully${result.server_version ? ` (${result.server_version})` : ""}${result.latency_ms ? ` — ${result.latency_ms}ms` : ""}`
        : result.error_message ?? "Connection failed");
    } catch (e: unknown) {
      setTestOk(false);
      setTestMsg(apiErrorMessage(e, "Connection test failed.", { prefer: "detail" }));
    }
    setStep("result");
  };

  const handleSave = async () => {
    const base = {
      name: name.trim(), description: desc,
      host: host.trim() || undefined, port: port || undefined,
      database_name: database.trim() || undefined,
      // "" asks the backend to clear the link and auto-detect it again.
      catalog_name: isEdit ? catalogValue.trim() : catalogValue.trim() || undefined,
      username: username.trim() || undefined,
      password: password.trim() || undefined,
      use_tls: useTLS,
    };
    try {
      if (editingId) {
        await updateConn({ id: editingId, body: base }).unwrap();
        dispatch(notify({ kind: "success", message: `Saved changes to "${base.name}"` }));
        close();
      } else {
        const conn = await createConn({ ...base, connection_type: type }).unwrap();
        await runTest(conn.id);
      }
    } catch (e: unknown) {
      setTestOk(false);
      setTestMsg(apiErrorMessage(e, "Failed to save connection.", { prefer: "detail" }));
      setStep("result");
    }
  };

  const isLoading = creating || updating;
  const busy = isLoading || step === "testing";
  const typeMeta = CONN_TYPES.find((c) => c.id === type);

  const footer = step === "result" ? (<>
    {!testOk && <button onClick={() => setStep("form")} style={drawerBtn(true)}>{isEdit ? "Back to edit" : "Edit and retry"}</button>}
    <button onClick={close} style={drawerBtn(!!testOk)}>{testOk ? "Done" : "Close"}</button>
  </>) : step === "testing" ? (
    <button disabled style={{ ...drawerBtn(false), opacity: 0.6 }}>Testing…</button>
  ) : (<>
    <button
      onClick={handleSave}
      disabled={isLoading || !name.trim()}
      style={{ ...drawerBtn(true), opacity: isLoading || !name.trim() ? 0.5 : 1 }}
    >
      {isLoading ? "Saving…" : isEdit ? "Save changes" : "Save and test connection"}
    </button>
    {isEdit && (
      <button onClick={() => editingId && runTest(editingId)} disabled={isLoading} style={drawerBtn(false)} title="Test the saved connection settings">
        <Glyph name="pi-test" size={14} style={{ display: "inline-block", verticalAlign: "-2px" }} /> Test
      </button>
    )}
    <button onClick={close} style={drawerBtn(false)}>Cancel</button>
  </>);

  return (
    <Drawer
      title={isEdit ? `Edit connection${existing ? ` — ${existing.name}` : ""}` : "Add database connection"}
      icon={isEdit ? "pi-pencil" : "ti-database-plus"}
      onClose={close}
      busy={busy}
      width={460}
      footer={footer}
    >
      <div style={{ fontSize: 11.5, color: "#888", marginBottom: 14 }}>
        Named connections are available across all workspaces in your org
      </div>

      {step === "testing" && (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <div style={{ fontSize: 14, color: "#666", marginBottom: 10 }}>Testing connection…</div>
          <div style={{ width: 40, height: 40, border: "3px solid #FFF7ED", borderTop: "3px solid #F97316", borderRadius: "50%", margin: "0 auto", animation: "spin 0.8s linear infinite" }} />
        </div>
      )}

      {step === "result" && (
        <div style={{ textAlign: "center", padding: "32px 0" }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}>{testOk ? "✅" : "❌"}</div>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>{testOk ? "Connection successful!" : "Connection failed"}</div>
          <div style={{ fontSize: 12, color: "#666" }}>{testMsg}</div>
        </div>
      )}

      {step === "form" && isEdit && !existing && (
        <div style={{ fontSize: 12.5, color: "#666", padding: "24px 0", textAlign: "center" }}>
          {listLoading ? "Loading connection…" : "This connection no longer exists."}
        </div>
      )}

      {step === "form" && (!isEdit || existing) && <>
        {/* Connection type — fixed once created */}
        <Field
          label="Connection type"
          hint={isEdit ? "The connection type can't be changed. Create a new connection to use a different engine." : undefined}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {(isEdit ? CONN_TYPES.filter((ct) => ct.id === type) : CONN_TYPES.filter((ct) => ct.offered !== false)).map((ct) => (
              <div
                key={ct.id}
                onClick={isEdit ? undefined : () => handleTypeChange(ct.id)}
                style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 11px", border: `1.5px solid ${type === ct.id ? "#F97316" : "#ddd"}`, background: type === ct.id ? "#FFF7ED" : "#fff", borderRadius: 8, cursor: isEdit ? "default" : "pointer" }}
              >
                <i className={`ti ${ct.icon}`} style={{ fontSize: 18, color: type === ct.id ? "#F97316" : "#888", width: 20 }} />
                <div>
                  <div style={{ fontSize: 12.5, fontWeight: 600 }}>{ct.label}</div>
                  <div style={{ fontSize: 11, color: "#888" }}>{ct.desc}</div>
                </div>
                {type === ct.id && <i className={`ti ${isEdit ? "ti-lock" : "ti-check"}`} style={{ marginLeft: "auto", color: "#F97316", fontSize: 15 }} />}
              </div>
            ))}
          </div>
        </Field>

        <Field label="Connection name" required>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={typeMeta?.nameExample ?? "e.g. production-db"} style={inp} />
        </Field>

        <Field label="Description">
          <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Optional" style={inp} />
        </Field>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 90px", gap: 10 }}>
          <Field label="Host">
            <input value={host} onChange={(e) => setHost(e.target.value)} placeholder={typeMeta?.hostExample ?? "db.yourcompany.com"} style={{ ...inp, fontFamily: "monospace" }} />
          </Field>
          <Field label="Port">
            <input type="number" value={port} onChange={(e) => setPort(Number(e.target.value))} placeholder={String(typeMeta?.defaultPort ?? "")} style={inp} />
          </Field>
        </div>

        {type === "trino" ? (
          <Field label="Catalog name" hint={options.length
            ? "The Iceberg catalog to query; pick one registered in the Unified Catalog to browse it there."
            : "The Iceberg catalog to query, e.g. iceberg."}>
            <input value={catalog} onChange={(e) => setCatalog(e.target.value)} list="ucs-catalogs" placeholder="iceberg" style={{ ...inp, fontFamily: "monospace" }} />
            <datalist id="ucs-catalogs">
              {options.map((c) => <option key={c.name} value={c.name} />)}
            </datalist>
          </Field>
        ) : options.length === 0 ? null : (
          <Field
            label="Unified Catalog source"
            hint={linked
              ? `Browsed through the Unified Catalog${linked.host ? ` (registered as ${linked.host}:${linked.port ?? ""}/${linked.database ?? ""}${linked.username ? ` as ${linked.username}` : ""})` : ""}.`
              : "Auto-detect links the catalog registered for the same database and port. If none matches, the database is read directly."}
          >
            <SearchSelect
              value={catalogValue}
              onChange={pickCatalog}
              ariaLabel="Unified Catalog source"
              noun="source"
              options={[
                { value: "", label: "Auto-detect" },
                ...options.map((c) => ({
                  value: c.name,
                  label: c.name,
                  description: c.database ? `${c.database}${c.port ? ` :${c.port}` : ""}` : undefined,
                })),
              ]}
            />
          </Field>
        )}

        <Field label="Database / Schema">
          <input value={database} onChange={(e) => setDatabase(e.target.value)} placeholder={isPg(type) ? "postgres" : "default"} style={inp} />
        </Field>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <Field label="Username">
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder={isEdit ? "Unchanged" : ""} autoComplete="off" style={inp} />
          </Field>
          <Field label="Password / Token">
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={isEdit ? "Unchanged" : ""} autoComplete="new-password" style={inp} />
          </Field>
        </div>
        {isEdit && (
          <div style={{ fontSize: 11, color: "#9ca3af", marginTop: -6, marginBottom: 12, lineHeight: 1.45 }}>
            Saved credentials aren't shown. Leave a field blank to keep its saved value.
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14 }}>
          <input type="checkbox" id="tls" checked={useTLS} onChange={(e) => setUseTLS(e.target.checked)} />
          <label htmlFor="tls" style={{ fontSize: 12, cursor: "pointer" }}>Use TLS / SSL</label>
        </div>

        <div style={{ fontSize: 11, color: "#888", padding: "9px 11px", background: "#F9FAFB", borderRadius: 7 }}>
          <i className="ti ti-shield-lock" style={{ fontSize: 13 }} /> Credentials are stored in your org's secrets vault (AWS Secrets Manager / Azure Key Vault / GCP Secret Manager) — never in the database.
        </div>
      </>}
    </Drawer>
  );
}
