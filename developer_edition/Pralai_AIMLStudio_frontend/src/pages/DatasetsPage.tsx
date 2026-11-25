import { useEffect, useMemo, useState } from "react";
import { usePageSearch } from "../context/PageSearchContext";
import { useSearchParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { useApi } from "../api/useApi";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";
import { useToast } from "../context/ToastContext";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import SearchSelect from "../components/SearchSelect";
import { SearchNote } from "../components/SearchNote";
import { ErrorBanner } from "../components/ErrorBanner";
import { confirmDialog } from "../components/ConfirmDrawer";
import FilePreviewModal from "./datasets/FilePreviewModal";
import type { Dataset, DatasetSnapshot, DatasetProfile, ShareLevel } from "../types/domain";

const DETAIL_TABS = ["Data Card", "Snapshots & Quality", "Comments", "Share", "Audit Log"] as const;
type DetailTab = (typeof DETAIL_TABS)[number];
const SHARE_LEVELS: ShareLevel[] = ["read", "download", "upload", "delete"];
/** Upload formats by dataset type — mirrors the backend's FORMATS_BY_STRUCTURE.
 *  The type is chosen when the dataset is created and fixes what it accepts. */
const UPLOAD_FORMATS: Array<{ id: string; label: string; accept: string; structure: "structured" | "unstructured" }> = [
  { id: "csv", label: "CSV", accept: ".csv", structure: "structured" },
  { id: "json", label: "JSON", accept: ".json", structure: "structured" },
  { id: "xls", label: "Excel (XLS)", accept: ".xls", structure: "structured" },
  { id: "xlsx", label: "Excel (XLSX)", accept: ".xlsx", structure: "structured" },
  { id: "parquet", label: "Parquet", accept: ".parquet", structure: "structured" },
  { id: "jsonl", label: "JSONL", accept: ".jsonl,.ndjson", structure: "structured" },
  { id: "html", label: "HTML", accept: ".html,.htm", structure: "unstructured" },
  { id: "ppt", label: "PPT", accept: ".ppt", structure: "unstructured" },
  { id: "pptx", label: "PPTX", accept: ".pptx", structure: "unstructured" },
  { id: "md", label: "Markdown (MD)", accept: ".md,.markdown", structure: "unstructured" },
  { id: "txt", label: "TXT", accept: ".txt", structure: "unstructured" },
  { id: "docx", label: "DOCX", accept: ".docx", structure: "unstructured" },
  { id: "png", label: "PNG", accept: ".png", structure: "unstructured" },
  { id: "zip", label: "ZIP", accept: ".zip", structure: "unstructured" },
  { id: "wav", label: "WAV", accept: ".wav", structure: "unstructured" },
  { id: "pdf", label: "PDF", accept: ".pdf", structure: "unstructured" },
];
/** The upload format a file is, from its extension — null if this dataset type doesn't take it. */
function formatForFile(file: File, structure: "structured" | "unstructured") {
  const n = file.name.toLowerCase();
  return UPLOAD_FORMATS.find((f) => f.structure === structure && f.accept.split(",").some((a) => n.endsWith(a))) ?? null;
}
/** Tables show rows × columns; an archive the files inside it; any other file is one file. */
function snapshotShape(s: DatasetSnapshot) {
  if (s.format === "image-zip") return `${s.rowCount.toLocaleString()} labelled images`;
  if (s.format === "zip") return `${s.rowCount.toLocaleString()} file${s.rowCount === 1 ? "" : "s"} inside`;
  if (s.columnNames.length === 0) return "1 file";
  return `${s.rowCount.toLocaleString()} rows · ${s.columnNames.length} cols`;
}
function initials(s: string) { return s.split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase()).join("") || "?"; }
function fmtDate(s?: string) { return s ? new Date(s).toLocaleString() : "—"; }

export default function DatasetsPage() {
  const datasets = useApi(api.getDatasets, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [layout, setLayout] = useState<"card" | "list">("card");
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = usePageSearch("Search datasets…");
  const [source, setSource] = useState<"all" | "upload" | "iceberg">("all");
  const [addOpen, setAddOpen] = useState(false);
  const [editing, setEditing] = useState<Dataset | null>(null);
  const { can } = usePermissions();
  const { toast } = useToast();
  // Every dataset write (create, edit, upload, delete, comment, share) needs mlstudio:datasets:upload.
  const canWrite = can(PERM.datasetsUpload);
  // The top-bar search lands here with ?q= (filter) or ?open= (one dataset).
  useEffect(() => {
    const q = params.get("q"), open = params.get("open");
    if (q !== null) setQuery(q);
    if (open) { setSelected(open); setParams({}, { replace: true }); }
  }, [params, setParams]);

  if (selected) return <DatasetDetail name={selected} canWrite={canWrite} onBack={() => { setSelected(null); datasets.reload(); }} onRenamed={setSelected} />;

  // Only a dataset with no files left can be deleted (the server enforces it too).
  async function removeDataset(d: Dataset) {
    if (!(await confirmDialog({
      title: `Delete ${d.name}?`,
      message: "This dataset has no files. Deleting it also removes its comments, sharing grants and audit log. This can't be undone.",
      confirmLabel: "Delete dataset", danger: true,
    }))) return;
    try { await api.deleteDataset(d.name); toast(`Deleted ${d.name}`); datasets.reload(); }
    catch (e) { toast(e instanceof ApiRequestError ? e.message : "Delete failed", "err"); }
  }
  // In the list the actions sit in an overflow-clipped table, so their tooltips open upward.
  const rowActions = (d: Dataset, tip?: "up") => canWrite && (
    <div className="dataset-hover-actions" onClick={(e) => e.stopPropagation()}>
      <IconAction icon="pencil" label={`Edit dataset ${d.name}`} tip={tip} align="end" onClick={() => setEditing(d)} />
      {(d.files ?? 0) === 0 && (
        <IconAction icon="trash" label={`Delete dataset ${d.name}`} danger tip={tip} align="end" onClick={() => removeDataset(d)} />
      )}
    </div>
  );

  const q = query.trim().toLowerCase();
  const list = (datasets.data ?? []).filter((d) => {
    if (source !== "all" && d.source !== source) return false;
    if (!q) return true;
    return d.name.toLowerCase().includes(q) || d.tags.some((t) => t.toLowerCase().includes(q));
  });
  const iconBg = (d: Dataset) => d.source === "iceberg" ? "icon-bg-amber" : (d.structure_type === "unstructured" ? "icon-bg-purple" : "icon-bg-teal");

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Datasets</h1>
            <p>A dataset is a folder — upload structured or unstructured files into it. Uploads and Iceberg-backed tables are both registered in Catalog as filesets. Every upload is checksummed, profiled, and stored as an immutable snapshot.</p>
          </div>
          {canWrite && <button className="btn btn-primary" style={{ flex: "none" }} onClick={() => setAddOpen(true)}><Icon name="plus" /> Add dataset</button>}
        </div>

        {datasets.error && <div className="error-banner">{datasets.error}</div>}

        {query.trim() && (
          <div className="filter-bar">
            <SearchNote term={query} onClear={() => { setQuery(""); setParams((p) => { const n = new URLSearchParams(p); n.delete("q"); return n; }, { replace: true }); }} />
          </div>
        )}
        <div className="tabs tabs-with-tools" role="tablist">
          {([["all", "All sources"], ["upload", "Uploads"], ["iceberg", "Iceberg"]] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={source === id}
              className={"tab" + (source === id ? " active" : "")} onClick={() => setSource(id)}>{label}</button>
          ))}
          <div className="filter-spacer" />
          <span className="cell-sub" style={{ marginRight: 8 }}>{datasets.loading ? "Loading…" : `${list.length} dataset${list.length === 1 ? "" : "s"}`}</span>
          <div className="view-toggle">
            <button className={"has-tip" + (layout === "list" ? " active" : "")} onClick={() => setLayout("list")} data-tip="List view" aria-label="List view"><Icon name="list" /></button>
            <button className={"has-tip tip-end" + (layout === "card" ? " active" : "")} onClick={() => setLayout("card")} data-tip="Card view" aria-label="Card view"><Icon name="grid" /></button>
          </div>
        </div>
      </div>

      {datasets.loading && <div className="spinner">Loading datasets…</div>}
      {!datasets.loading && list.length === 0 && <div className="empty-hint">No datasets match. {canWrite && "Use “Add dataset” to create one."}</div>}

      {!datasets.loading && list.length > 0 && layout === "card" && (
        <div className="dataset-card-grid">
          {list.map((d) => (
            <div key={d.name} className="dataset-card" onClick={() => setSelected(d.name)}>
              {rowActions(d)}
              <div className="dataset-card-head">
                <div className={"dataset-card-icon " + iconBg(d)}><Icon name="database" /></div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="dataset-card-title">{d.name}</div>
                  <div className="dataset-card-owner">{d.owner} · {d.visibility ?? "private"}</div>
                </div>
              </div>
              {d.description && <div className="dataset-card-desc" title={d.description}>{d.description}</div>}
              <div className="dataset-card-tags">
                {d.tags.slice(0, 3).map((t) => <span key={t} className="tag">{t}</span>)}
                {d.tags.length > 3 && <span className="tag">+{d.tags.length - 3}</span>}
              </div>
              <div className="dataset-card-stats">
                <div className="dataset-card-stat"><div className="n">{d.files ?? 0}</div><div className="l">Files</div></div>
                <div className="dataset-card-stat"><div className="n">{d.size}</div><div className="l">Size</div></div>
                <div className="dataset-card-stat"><div className="n">{d.rows}</div><div className="l">Rows</div></div>
              </div>
            </div>
          ))}
        </div>
      )}

      {!datasets.loading && list.length > 0 && layout === "list" && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Dataset</th><th>Source</th><th>Tags</th><th>Size</th><th>Owner</th><th>Files</th><th style={{ width: 84 }} /></tr></thead>
            <tbody>
              {list.map((d) => (
                <tr key={d.name} className="clickable dataset-row" onClick={() => setSelected(d.name)}>
                  <td><div className="cell-name">{d.name}</div><div className="cell-sub">{d.description ? `${d.description} · ` : ""}{d.structure_type === "unstructured" ? "Unstructured" : "Structured"} · {d.detail}</div></td>
                  <td><span className="tag">{d.source === "iceberg" ? "Iceberg" : "Upload"}</span></td>
                  <td><div className="badge-row">{d.tags.map((t) => <span key={t} className="tag">{t}</span>)}</div></td>
                  <td className="mono">{d.size}</td>
                  <td>{d.owner}</td>
                  <td className="mono">{d.files ?? 0}</td>
                  <td>{rowActions(d, "up")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && <EditDatasetDrawer dataset={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); datasets.reload(); }} />}
      {addOpen && <AddDatasetModal onClose={() => setAddOpen(false)} onCreated={(name) => { setAddOpen(false); datasets.reload(); setSelected(name); }} />}
    </div>
  );
}

/* ---------------- Detail ---------------- */
function DatasetDetail({ name, canWrite, onBack, onRenamed }: { name: string; canWrite: boolean; onBack: () => void; onRenamed: (name: string) => void }) {
  const ds = useApi(() => api.getDataset(name), [name]);
  const [tab, setTab] = useState<DetailTab>("Data Card");
  const [editing, setEditing] = useState(false);
  const d = ds.data;

  return (
    <div>
      <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={onBack}><Icon name="arrow-left" /> Back to datasets</button>
      {ds.loading && <div className="spinner">Loading…</div>}
      {ds.error && <div className="error-banner">{ds.error}</div>}
      {d && (
        <>
          <div className="page-head">
            <div>
              <div className="badge-row" style={{ marginBottom: 10 }}>
                <span className={"badge " + (d.structure_type === "unstructured" ? "badge-outline" : "badge-ml")}><span className="dot" />{d.structure_type === "unstructured" ? "Unstructured" : "Structured"}</span>
                <span className="tag">{d.source === "iceberg" ? "Iceberg" : "Upload"}</span>
                <span className="badge badge-outline">{d.visibility ?? "private"}</span>
                {d.tags.map((t) => <span key={t} className="tag">#{t}</span>)}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <h1 style={{ fontFamily: "var(--font-mono)" }}>{d.name}</h1>
                {canWrite && <IconAction icon="pencil" label={`Edit dataset ${d.name}`} onClick={() => setEditing(true)} />}
              </div>
              {d.description && <p style={{ color: "var(--text)", marginBottom: 4 }}>{d.description}</p>}
              <p>Owned by {d.owner} · created {fmtDate(d.createdAt)}</p>
            </div>
          </div>

          <div className="ds-stat-grid">
            <div className="ds-stat"><div className="s-label">Updated</div><div className="s-val">{fmtDate(d.updatedAt)}</div></div>
            <div className="ds-stat"><div className="s-label">Files</div><div className="s-val">{d.files ?? 0}</div></div>
            <div className="ds-stat"><div className="s-label">Rows</div><div className="s-val">{d.rows}</div></div>
            <div className="ds-stat"><div className="s-label">Size</div><div className="s-val">{d.size}</div></div>
            <div className="ds-stat"><div className="s-label">Deploy target</div><div className="s-val" style={{ fontSize: 11 }}>{d.deployTarget}</div></div>
          </div>

          <div className="tabs">
            {DETAIL_TABS.map((t) => <button key={t} className={"tab" + (tab === t ? " active" : "")} onClick={() => setTab(t)}>{t}</button>)}
          </div>

          {tab === "Data Card" && <DataCardTab d={d} canWrite={canWrite} onChange={ds.reload} />}
          {tab === "Snapshots & Quality" && <SnapshotsTab d={d} canWrite={canWrite} onChange={ds.reload} />}
          {tab === "Comments" && <CommentsTab name={name} canWrite={canWrite} />}
          {tab === "Share" && <ShareTab name={name} canWrite={canWrite} />}
          {tab === "Audit Log" && <AuditTab name={name} />}
          {editing && <EditDatasetDrawer dataset={d} onClose={() => setEditing(false)}
            onSaved={(u) => { setEditing(false); if (u.name !== name) onRenamed(u.name); else ds.reload(); }} />}
        </>
      )}
    </div>
  );
}

/* Copies the Iceberg table's current snapshot (or the pinned one) into a new, profiled
   Parquet version of this dataset — training then reads that exact version. */
function PullIcebergButton({ d, onPulled }: { d: Dataset; onPulled: () => void }) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const [askCreds, setAskCreds] = useState(false);
  const catalogs = useApi(() => api.getIcebergCatalogs(), []);
  const ref = d.icebergRef;
  if (!ref) return null;
  const needsCredentials = !!catalogs.data?.find((c) => c.name === ref.catalog)?.needsCredentials;
  async function pull(credentials?: { accessKeyId: string; secretAccessKey: string; sessionToken?: string }) {
    setBusy(true);
    try {
      const r = await api.pullIcebergSnapshot(d.name, `${ref!.schemaName}.${ref!.table}`, ref!.snapshot, credentials);
      toast(r.unchanged ? `Already up to date — Iceberg snapshot ${r.icebergSnapshotId} is version ${r.datasetSnapshotVersion}` : `Pulled snapshot ${r.icebergSnapshotId} — ${r.rowCount.toLocaleString()} rows, ${r.columnNames.length} columns`);
      setAskCreds(false);
      onPulled();
    } catch (e) { toast(e instanceof ApiRequestError ? e.message : "Pulling the Iceberg snapshot failed", "err"); }
    finally { setBusy(false); }
  }
  return <>
    <button className="btn btn-primary btn-sm" disabled={busy || catalogs.loading} onClick={() => needsCredentials ? setAskCreds(true) : pull()}>
      <Icon name="download" /> {busy ? "Pulling…" : ref.snapshot ? "Pull pinned snapshot" : "Pull latest snapshot"}
    </button>
    {askCreds && <StorageCredentialsDrawer catalog={ref.catalog} warehouse={catalogs.data?.find((c) => c.name === ref.catalog)?.warehouse ?? null}
      busy={busy} onClose={() => setAskCreds(false)} onSubmit={pull} />}
  </>;
}

/* Asks for the object-store keys of a catalog whose credentials aren't kept on the server.
   They live only in this form's state and the one pull request — closing it forgets them. */
function StorageCredentialsDrawer({ catalog, warehouse, busy, onClose, onSubmit }: {
  catalog: string; warehouse: string | null; busy: boolean; onClose: () => void;
  onSubmit: (c: { accessKeyId: string; secretAccessKey: string; sessionToken?: string }) => void;
}) {
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const ready = accessKeyId.trim() && secretAccessKey.trim();
  const submit = () => { if (ready && !busy) onSubmit({ accessKeyId: accessKeyId.trim(), secretAccessKey: secretAccessKey.trim(), ...(sessionToken.trim() ? { sessionToken: sessionToken.trim() } : {}) }); };
  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)" }}>Storage credentials for {catalog}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>Needed to read this table's data files{warehouse ? ` in ${warehouse}` : ""}</div>
          </div>
          <button className="drawer-close" onClick={onClose} disabled={busy}><Icon name="x" /></button>
        </div>
        <form className="drawer-body" autoComplete="off" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <div className="callout callout-teal" style={{ marginBottom: 16 }}>
            Used for this pull only — ML Studio doesn't save them anywhere. A read-only key for the bucket is enough.
          </div>
          <div className="field">
            <label htmlFor="ice-akid">Access key ID</label>
            <input id="ice-akid" className="input mono" value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="AKIA…" autoComplete="off" spellCheck={false} disabled={busy} autoFocus />
          </div>
          <div className="field">
            <label htmlFor="ice-secret">Secret access key</label>
            <input id="ice-secret" className="input mono" type="password" value={secretAccessKey} onChange={(e) => setSecretAccessKey(e.target.value)} autoComplete="new-password" spellCheck={false} disabled={busy} />
          </div>
          <div className="field">
            <label htmlFor="ice-token">Session token <span style={{ fontWeight: 400, color: "var(--text-faint)" }}>(optional)</span></label>
            <input id="ice-token" className="input mono" type="password" value={sessionToken} onChange={(e) => setSessionToken(e.target.value)} autoComplete="off" spellCheck={false} disabled={busy} />
            <div className="hint">Only for temporary credentials (keys starting with ASIA).</div>
          </div>
          <button type="submit" hidden />
        </form>
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy || !ready}><Icon name="download" /> {busy ? "Pulling…" : "Pull snapshot"}</button>
        </div>
      </div>
    </div>
  );
}

/* ---------------- Data Card (files = snapshots, eye → real preview) ---------------- */
function DataCardTab({ d, canWrite, onChange }: { d: Dataset; canWrite: boolean; onChange: () => void }) {
  const snaps = useApi(() => api.getDatasetSnapshots(d.name), [d.name]);
  const [preview, setPreview] = useState<{ version: number; label: string } | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [openFiles, setOpenFiles] = useState<Set<string>>(new Set());
  const { toast } = useToast();
  // Uploads grouped by file name, newest version first; training uses each file's newest.
  const fileGroups = useMemo(() => {
    const m = new Map<string, DatasetSnapshot[]>();
    for (const sn of snaps.data ?? []) {
      const k = sn.fileName ?? `${d.name}.${sn.format}`;
      m.set(k, [...(m.get(k) ?? []), sn]);
    }
    return [...m.entries()].map(([k, v]) => [k, v.sort((a, b) => b.version - a.version)] as [string, DatasetSnapshot[]]);
  }, [snaps.data, d.name]);
  const toggleFile = (f: string) => setOpenFiles((s) => { const n = new Set(s); n.has(f) ? n.delete(f) : n.add(f); return n; });

  async function del(version: number, label: string) {
    if (!(await confirmDialog({
      title: `Delete ${label}?`,
      message: `This removes ${label}. Other files and versions are untouched; if it was the file's latest version, the previous one becomes the version used for training.`,
      confirmLabel: "Delete", danger: true,
    }))) return;
    try { await api.deleteDatasetSnapshot(d.name, version); toast(`Deleted ${label}`); snaps.reload(); onChange(); }
    catch (e) { toast(e instanceof ApiRequestError ? e.message : "Delete failed", "err"); }
  }

  return (
    <div className="card card-pad">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div className="drawer-section-title" style={{ marginBottom: 0 }}>Dataset files</div>
        {canWrite && d.source !== "iceberg" && <button className="btn btn-primary btn-sm" onClick={() => setUploadOpen(true)}><Icon name="plus" /> Add files</button>}
        {canWrite && d.source === "iceberg" && <PullIcebergButton d={d} onPulled={() => { snaps.reload(); onChange(); }} />}
      </div>
      <div className="hint" style={{ marginBottom: 12 }}>A dataset holds any number of files. Uploading a file with the same name adds a new version of it — nothing is overwritten. Training uses the latest version of every file.</div>
      {snaps.loading && <div className="spinner">Loading…</div>}
      {d.source === "iceberg" && <div className="file-row"><div className="file-icon"><Icon name="database" /></div><div className="file-main"><div className="file-name">Iceberg table {d.icebergRef ? `${d.icebergRef.catalog}.${d.icebergRef.schemaName}.${d.icebergRef.table}` : d.detail}</div><div className="file-meta">{d.icebergRef?.snapshot ? `Pinned to snapshot ${d.icebergRef.snapshot}` : "Follows the current snapshot"} · each pull below is a frozen copy training can use{!snaps.loading && (snaps.data ?? []).length === 0 && " — none pulled yet"}</div></div></div>}
      {!snaps.loading && d.source !== "iceberg" && (snaps.data ?? []).length === 0 && <div className="empty-hint">No files yet. {canWrite && "Use “Add file” to upload one — it becomes version 1."}</div>}
      {fileGroups.map(([fileName, versions]) => {
        const latest = versions[0];
        const open = openFiles.has(fileName);
        const label = (v: DatasetSnapshot) => `${fileName} · v${v.fileVersion ?? 1}`;
        const actions = (v: DatasetSnapshot) => (
          <div className="file-actions">
            {d.structure_type !== "unstructured" && (
              <IconAction icon="eye" label={`Preview ${label(v)}`} align="end" onClick={() => setPreview({ version: v.version, label: label(v) })} />
            )}
            <IconAction icon="download" label={`Download ${label(v)}`} align="end" onClick={() => window.open(api.datasetSnapshotDownloadUrl(d.name, v.version), "_blank")} />
            {canWrite && <IconAction icon="trash" label={`Delete version ${label(v)}`} danger align="end" onClick={() => del(v.version, label(v))} />}
          </div>
        );
        return (
          <div key={fileName}>
            <div className="file-row">
              <div className="file-icon"><Icon name="file" /></div>
              <div className="file-main">
                <div className="file-name">{fileName}</div>
                <div className="file-meta">
                  v{latest.fileVersion ?? 1}{versions.length > 1 ? ` (latest of ${versions.length})` : ""} · {snapshotShape(latest)} · {latest.format.toUpperCase()} · by {latest.uploadedBy} · {fmtDate(latest.uploadedAt)}
                  {versions.length > 1 && (
                    <button className="link-btn" style={{ marginLeft: 8 }} onClick={() => toggleFile(fileName)}>
                      {open ? "hide versions" : `show ${versions.length - 1} older version${versions.length > 2 ? "s" : ""}`}
                    </button>
                  )}
                </div>
              </div>
              {actions(latest)}
            </div>
            {open && versions.slice(1).map((v) => (
              <div className="file-row" key={v.version} style={{ marginLeft: 36, opacity: 0.85 }}>
                <div className="file-main">
                  <div className="file-name" style={{ fontSize: 12.5 }}>v{v.fileVersion ?? 1} <span className="tag" style={{ marginLeft: 6 }}>not used for training</span></div>
                  <div className="file-meta">{snapshotShape(v)} · by {v.uploadedBy} · {fmtDate(v.uploadedAt)}</div>
                </div>
                {actions(v)}
              </div>
            ))}
          </div>
        );
      })}
      {preview && <FilePreviewModal datasetName={d.name} version={preview.version} label={preview.label} onClose={() => setPreview(null)} />}
      {uploadOpen && <UploadModal name={d.name} structure={d.structure_type === "unstructured" ? "unstructured" : "structured"} existingFiles={fileGroups.map(([f]) => f)} onClose={() => setUploadOpen(false)} onUploaded={() => { snaps.reload(); onChange(); }} />}
    </div>
  );
}

/* ---------------- Snapshots & Quality (real profiles) ---------------- */
function qualityFlags(p: DatasetProfile) {
  const flags: { level: "warn" | "crit"; text: string }[] = [];
  if (p.duplicateRowCount > 0) flags.push({ level: "warn", text: `${p.duplicateRowCount} duplicate rows` });
  const highMissing = p.columns.filter((c) => c.missingPct >= 20);
  if (highMissing.length) flags.push({ level: highMissing.some((c) => c.missingPct >= 50) ? "crit" : "warn", text: `${highMissing.length} column(s) >20% missing` });
  const outliers = p.columns.filter((c) => (c.outlierCount ?? 0) > 0);
  if (outliers.length) flags.push({ level: "warn", text: `${outliers.length} column(s) with outliers` });
  const constant = p.columns.filter((c) => c.isConstant || c.isNearConstant);
  if (constant.length) flags.push({ level: "warn", text: `${constant.length} constant / near-constant column(s)` });
  return flags;
}
function qualityBadge(p?: DatasetProfile) {
  if (!p || !p.ok) return <span className="cell-sub">—</span>;
  const f = qualityFlags(p);
  if (f.some((x) => x.level === "crit")) return <span className="status-pill status-failed"><span className="status-dot" /> {f.length} issue{f.length > 1 ? "s" : ""}</span>;
  if (f.length) return <span className="status-pill status-running"><span className="status-dot" /> {f.length} warning{f.length > 1 ? "s" : ""}</span>;
  return <span className="status-pill status-success"><span className="status-dot" /> clean</span>;
}
function fmtN(n: number | null | undefined) { return n === null || n === undefined ? "—" : (Number.isInteger(n) ? n.toLocaleString() : Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 })); }

function SnapshotsTab({ d, canWrite, onChange }: { d: Dataset; canWrite: boolean; onChange: () => void }) {
  // Fetch every version WITH its real profile (few versions per dataset) so
  // each row shows a real quality gate and the selected one shows its profile.
  const full = useApi(() => api.getDatasetSnapshots(d.name).then((list) =>
    Promise.all(list.map((s) => api.getDatasetSnapshot(d.name, s.version)))), [d.name]);
  const [openVersion, setOpenVersion] = useState<number | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  if (d.source === "iceberg" && !full.loading && !full.error && (full.data ?? []).length === 0) return <div className="card card-pad" style={{ textAlign: "center", padding: 32 }}>
    <div style={{ fontWeight: 700, marginBottom: 6 }}>No snapshot pulled from Iceberg yet</div>
    <div className="hint" style={{ maxWidth: 520, margin: "0 auto 14px" }}>Pulling reads <code>{d.icebergRef ? `${d.icebergRef.schemaName}.${d.icebergRef.table}` : "—"}</code> {d.icebergRef?.snapshot ? `at snapshot ${d.icebergRef.snapshot}` : "at its current snapshot"} and stores it here as an immutable, profiled version.</div>
    {canWrite && <PullIcebergButton d={d} onPulled={() => { full.reload(); onChange(); }} />}
  </div>;
  if (d.structure_type === "unstructured") return <div className="card card-pad" style={{ textAlign: "center", padding: 32 }}>
    <div style={{ fontWeight: 700, marginBottom: 6 }}>Profiling applies to structured/tabular data</div>
    <div className="hint" style={{ maxWidth: 480, margin: "0 auto" }}>This dataset is unstructured — column-level statistics don't apply the same way. Snapshot versioning still happens; a text-specific profile isn't built yet.</div>
  </div>;

  if (full.loading) return <div className="spinner">Loading snapshots…</div>;
  if (full.error) return <div className="error-banner">{full.error}</div>;
  const snaps = full.data ?? [];
  const active = snaps.find((s) => s.version === openVersion) || snaps[0];

  return (
    <div>
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
          <div className="drawer-section-title" style={{ marginBottom: 0 }}>Real, immutable versions</div>
          {canWrite && (d.source === "iceberg"
            ? <PullIcebergButton d={d} onPulled={() => { full.reload(); onChange(); }} />
            : <button className="btn btn-primary btn-sm" onClick={() => setUploadOpen(true)}><Icon name="upload" /> Upload new version</button>)}
        </div>
        <div className="hint" style={{ marginBottom: 10 }}>Every upload creates a new version — none are ever overwritten. A trained model records exactly which version it used.</div>
        {snaps.length === 0 && <div className="empty-hint">No versions uploaded yet.</div>}
        {snaps.length > 0 && (
          <table>
            <thead><tr><th>File</th><th>Version</th><th>Uploaded</th><th>Rows</th><th>Format</th><th>Checksum (SHA-256)</th><th>Quality gate</th><th></th></tr></thead>
            <tbody>
              {snaps.map((s) => (
                <tr key={s.version} className="clickable" onClick={() => setOpenVersion(s.version)} style={s.version === active?.version ? { background: "var(--surface-sunken)" } : undefined}>
                  <td className="cell-name" style={{ wordBreak: "break-all" }}>{s.fileName ?? "—"}</td>
                  <td className="cell-name">v{s.fileVersion ?? s.version} {s.isLatest && <span className="tag" style={{ marginLeft: 4 }} title="The version training uses">in use</span>}</td>
                  <td>{fmtDate(s.uploadedAt)}<div style={{ fontSize: 11, color: "var(--text-faint)" }}>by {s.uploadedBy}</div></td>
                  <td className="mono">{s.rowCount.toLocaleString()}</td>
                  <td>{s.format}</td>
                  <td className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>{s.checksum.slice(0, 20)}…</td>
                  <td>{qualityBadge(s.profile)}</td>
                  <td><IconAction icon="eye" label={`View profile of ${s.fileName ?? "file"} v${s.fileVersion ?? s.version}`} tip="up" align="end" onClick={(e) => { e.stopPropagation(); setOpenVersion(s.version); }} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {active && active.profile && active.profile.ok && (
        <div className="card card-pad">
          <div className="drawer-section-title">Real column profile — <span className="mono" style={{ color: "var(--text-muted)", fontWeight: 600 }}>v{active.version}</span></div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
            {qualityFlags(active.profile).length === 0
              ? <span className="quality-flag ok"><Icon name="check" /> No quality issues detected</span>
              : qualityFlags(active.profile).map((f, i) => <span key={i} className={"quality-flag " + f.level}><Icon name="alert" /> {f.text}</span>)}
          </div>
          <table>
            <thead><tr><th>Column</th><th>Missing %</th><th>Mean / cardinality</th><th>Std dev</th><th>Min / Median / Max</th><th>Top value</th></tr></thead>
            <tbody>
              {active.profile.columns.map((c) => (
                <tr key={c.name}>
                  <td className="cell-name">{c.name}<div className="tag" style={{ marginTop: 3 }}>{c.kind}</div></td>
                  <td style={c.missingPct > 20 ? { color: "var(--red)", fontWeight: 700 } : c.missingPct > 0 ? { color: "var(--amber-700)" } : undefined}>{c.missingPct}%</td>
                  <td>{c.kind === "numeric" ? fmtN(c.mean) : `${c.cardinality} distinct`}</td>
                  <td>{c.kind === "numeric" ? fmtN(c.std) : "—"}</td>
                  <td className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>{c.kind === "numeric" ? `${fmtN(c.min)} / ${fmtN(c.median)} / ${fmtN(c.max)}` : "—"}</td>
                  <td>{c.kind === "categorical" ? (c.topValues?.[0] ? `${c.topValues[0].value} (${c.topValues[0].count})` : "—") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {uploadOpen && <UploadModal name={d.name} structure="structured" existingFiles={[...new Set(snaps.map((sn) => sn.fileName ?? ""))].filter(Boolean)} onClose={() => setUploadOpen(false)} onUploaded={() => { full.reload(); onChange(); }} />}
    </div>
  );
}

/* ---------------- Comments ---------------- */
function CommentsTab({ name, canWrite }: { name: string; canWrite: boolean }) {
  const comments = useApi(() => api.getDatasetComments(name), [name]);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();
  async function post() {
    if (!text.trim()) return; setError(null);
    try { await api.postDatasetComment(name, text); setText(""); comments.reload(); toast("Comment posted"); }
    catch (e) { setError(e instanceof ApiRequestError ? e.message : "Failed to post comment"); }
  }
  return (
    <div>
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Add comment</div>
        {error && <ErrorBanner message={error} />}
        <input className="input" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && post()} placeholder="Share your thoughts about this dataset…" />
        {canWrite ? <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}><button className="btn btn-primary btn-sm" onClick={post}><Icon name="message" /> Post comment</button></div>
          : <div className="role-gated-note show" style={{ marginTop: 10 }}><Icon name="alert" /> Commenting needs permission to manage datasets.</div>}
      </div>
      <div className="card card-pad">
        <div className="drawer-section-title">Discussion timeline</div>
        {comments.data?.length === 0 && <div className="empty-hint">No comments yet.</div>}
        <div className="comment-thread">
          {comments.data?.map((c) => (
            <div className="comment-item" key={c.id}>
              <div className="comment-avatar">{initials(c.author)}</div>
              <div className="comment-body">
                <div className="comment-head"><span className="comment-author">{c.author}</span><span className="comment-time">{fmtDate(c.createdAt)}</span></div>
                <div className="comment-text">{c.body}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ---------------- Share (levels) ---------------- */
function ShareTab({ name, canWrite }: { name: string; canWrite: boolean }) {
  const access = useApi(() => api.getDatasetAccess(name), [name]);
  const [user, setUser] = useState("");
  const [levels, setLevels] = useState<Record<string, boolean>>({ read: true, download: false, upload: false, delete: false });
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();
  async function grant() {
    if (!user.trim()) { toast("Enter a user to share with"); return; }
    const checked = SHARE_LEVELS.filter((l) => levels[l]);
    if (!checked.length) { toast("Select at least one permission"); return; }
    setError(null);
    try { await api.grantDatasetAccess(name, user.trim(), checked); setUser(""); access.reload(); toast(`Shared with ${user.trim()}`); }
    catch (e) { setError(e instanceof ApiRequestError ? e.message : "Failed to share"); }
  }
  async function revoke(id: number) {
    setError(null);
    try { await api.revokeDatasetAccess(name, id); access.reload(); toast("Access revoked"); }
    catch (e) { setError(e instanceof ApiRequestError ? e.message : "Failed to revoke"); }
  }
  return (
    <div>
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Share dataset</div>
        {error && <ErrorBanner message={error} />}
        <div className="field"><label>User</label><input className="input" value={user} onChange={(e) => setUser(e.target.value)} placeholder="name@acme-corp.com" /></div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Permissions to grant</label>
          <div className="perm-checkboxes">
            {SHARE_LEVELS.map((l) => <label className="perm-checkbox" key={l}><input type="checkbox" checked={!!levels[l]} onChange={(e) => setLevels({ ...levels, [l]: e.target.checked })} /> <span style={{ textTransform: "capitalize" }}>{l}</span></label>)}
          </div>
          <div className="hint">Read, download, upload, and delete are granted independently.</div>
        </div>
        {canWrite && <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 14 }}><button className="btn btn-primary btn-sm" onClick={grant}><Icon name="share" /> Share</button></div>}
      </div>
      <div className="card card-pad">
        <div className="drawer-section-title">Current access</div>
        {access.data?.length === 0 && <div className="empty-hint">No explicit sharing yet.</div>}
        {access.data?.map((g) => (
          <div className="perm-row" key={g.id}>
            <div className="comment-avatar">{initials(g.principal)}</div>
            <span className="perm-row-name">{g.principal}</span>
            <div style={{ display: "flex", gap: 5 }}>{g.levels.map((l) => <span key={l} className="perm-level-tag">{l}</span>)}</div>
            {canWrite && <IconAction icon="trash" label={`Revoke access for ${g.principal}`} danger align="end" onClick={() => revoke(g.id)} />}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------- Audit (type + details) ---------------- */
const AUDIT_ICON: Record<string, string> = { upload: "upload", share: "share", comment: "message", register: "register", delete: "trash", event: "clock" };
function AuditTab({ name }: { name: string }) {
  const log = useApi(() => api.getDatasetAuditLog(name), [name]);
  if (log.loading) return <div className="spinner">Loading…</div>;
  const rows = log.data ?? [];
  return (
    <div className="card card-pad">
      <div className="drawer-section-title">Dataset audit log</div>
      <div className="hint" style={{ marginBottom: 10 }}>Every action — create, upload, comment, share, revoke — is recorded here automatically.</div>
      {rows.length === 0 && <div className="empty-hint">No audit entries yet.</div>}
      {rows.map((a) => (
        <div className="audit-entry" key={a.id}>
          <div className={"audit-icon " + a.type}><Icon name={AUDIT_ICON[a.type] || "clock"} /></div>
          <div className="audit-main">
            <div className="audit-title-row"><span className="audit-title" style={{ textTransform: "capitalize" }}>{a.action}</span><span className="audit-time">{fmtDate(a.createdAt)}</span></div>
            <div className="audit-actor">@{a.actor}</div>
            {a.details.length > 0 && <ul className="audit-details">{a.details.map((x, xi) => <li key={xi}>{x}</li>)}</ul>}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---------------- Add dataset modal (real create) ---------------- */
function AddDatasetModal({ onClose, onCreated }: { onClose: () => void; onCreated: (name: string) => void }) {
  const [srcMode, setSrcMode] = useState<"upload" | "iceberg">("upload");
  const [name, setName] = useState("");
  const [structure, setStructure] = useState<"structured" | "unstructured">("structured");
  const [visibility, setVisibility] = useState<"private" | "org">("private");
  const [tags, setTags] = useState("");
  const [description, setDescription] = useState("");
  const [catalog, setCatalog] = useState("lakehouse");
  const catalogs = useApi(() => api.getIcebergCatalogs(), []);
  const [schemaName, setSchemaName] = useState("");
  const [table, setTable] = useState("");
  const [snapMode, setSnapMode] = useState<"latest" | "pin">("latest");
  const [snapId, setSnapId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();

  async function create() {
    if (!name.trim()) { setError("Give the dataset a name first"); return; }
    setBusy(true); setError(null);
    try {
      const d = await api.createDataset({
        name: name.trim(), source: srcMode, structureType: srcMode === "iceberg" ? "structured" : structure,
        visibility, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), detail: "", description: description.trim(),
        iceberg: srcMode === "iceberg" ? { catalog, schemaName: schemaName.trim(), table: table.trim(), snapshot: snapMode === "pin" ? snapId.trim() : null } : undefined,
      });
      toast(`${d.name} created`); onCreated(d.name);
    } catch (e) { setError(e instanceof ApiRequestError ? e.message : "Failed to create dataset"); }
    finally { setBusy(false); }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)" }}>Add dataset</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>Registered in Catalog as a fileset</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <div className="field">
            <label>Source</label>
            <div className="filter-bar" style={{ marginBottom: 0 }}>
              <button className={"chip" + (srcMode === "upload" ? " active" : "")} onClick={() => setSrcMode("upload")}>Upload files</button>
              <button className={"chip" + (srcMode === "iceberg" ? " active" : "")} onClick={() => setSrcMode("iceberg")}>Import from Iceberg</button>
            </div>
          </div>
          <div className="field"><label>Dataset name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. returns-transactions-2026" /><div className="hint">Lowercase letters, digits and hyphens.</div></div>
          <div className="field">
            <label>Description <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(optional)</span></label>
            <textarea className="input" rows={3} maxLength={1000} value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="What's in this dataset, where it comes from, and what it's for" />
          </div>

          {srcMode === "upload" ? (
            <div className="field">
              <label>Type</label>
              <div className="filter-bar" style={{ marginBottom: 0 }}>
                <button className={"chip" + (structure === "structured" ? " active" : "")} onClick={() => setStructure("structured")}>Structured — CSV, JSON, JSONL, Excel, Parquet</button>
                <button className={"chip" + (structure === "unstructured" ? " active" : "")} onClick={() => setStructure("unstructured")}>Unstructured — PDF, DOCX, PPT, TXT, MD, HTML, PNG, WAV, ZIP</button>
              </div>
              <div className="hint">Sets what the Add files dialog accepts later — this can't be mixed within one dataset.</div>
            </div>
          ) : (
            <>
              <div className="callout callout-teal" style={{ marginBottom: 14 }}>Import-only — registers a <strong>pointer</strong> to an existing Iceberg table, not a copy.</div>
              <div className="field-row">
                <div className="field"><label id="import-catalog-label">Catalog</label><SearchSelect labelledBy="import-catalog-label" noun="catalog" menuMinWidth={300} value={catalog}
                  options={(catalogs.data ?? [{ name: "lakehouse", warehouse: null, configured: true }]).map((c) => ({ value: c.name, label: c.configured ? c.name : `${c.name} (not connected yet)` }))}
                  onChange={setCatalog} />
                  {(() => { const c = catalogs.data?.find((x) => x.name === catalog); return c && <div className="hint" style={{ marginTop: 4 }}>{!c.configured ? "This catalog's connection isn't set up on the server yet — you can register now, pulling will fail until it is." : c.needsCredentials ? `${c.warehouse} — you'll be asked for storage credentials when pulling.` : c.warehouse}</div>; })()}</div>
                <div className="field"><label>Schema</label><input className="input" value={schemaName} onChange={(e) => setSchemaName(e.target.value)} placeholder="e.g. acme-corp.sensors" /></div>
              </div>
              <div className="field"><label>Table</label><input className="input" value={table} onChange={(e) => setTable(e.target.value)} placeholder="e.g. equipment_v13" /></div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Snapshot</label>
                <div className="filter-bar" style={{ marginBottom: 0 }}>
                  <button className={"chip" + (snapMode === "latest" ? " active" : "")} onClick={() => setSnapMode("latest")}>Always read latest</button>
                  <button className={"chip" + (snapMode === "pin" ? " active" : "")} onClick={() => setSnapMode("pin")}>Pin a snapshot</button>
                </div>
                {snapMode === "pin" && <input className="input" style={{ marginTop: 10 }} value={snapId} onChange={(e) => setSnapId(e.target.value)} placeholder="Snapshot ID, e.g. 40281" />}
              </div>
            </>
          )}
          <div className="field-row" style={{ marginTop: 16 }}>
            <div className="field" style={{ marginBottom: 0 }}><label id="dataset-visibility-label">Visibility</label>
              <SearchSelect labelledBy="dataset-visibility-label" noun="level" menuMinWidth={300} value={visibility}
                options={[{ value: "private", label: "Private" }, { value: "org", label: "Org" }]}
                onChange={(v) => setVisibility(v as "private" | "org")} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}><label>Tags</label><input className="input" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="finance, classification" /></div>
          </div>
          {error && <ErrorBanner message={error} style={{ marginTop: 14, marginBottom: 0 }} />}
        </div>
        <div className="drawer-foot"><button className="btn btn-secondary" onClick={onClose} disabled={busy}>Cancel</button><button className="btn btn-primary" onClick={create} disabled={busy || !name.trim()}><Icon name="plus" /> {busy ? "Creating…" : "Add dataset"}</button></div>
      </div>
    </div>
  );
}

/* ---------------- Edit dataset (name + description) ---------------- */
function EditDatasetDrawer({ dataset, onClose, onSaved }: { dataset: Dataset; onClose: () => void; onSaved: (d: Dataset) => void }) {
  const [name, setName] = useState(dataset.name);
  const [description, setDescription] = useState(dataset.description ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();
  const renamed = name.trim() !== dataset.name;
  const changed = renamed || description.trim() !== (dataset.description ?? "");

  async function save() {
    if (!name.trim()) { setError("The dataset needs a name"); return; }
    if (!/^[a-z0-9][a-z0-9-]*$/i.test(name.trim())) { setError("Use letters, digits and hyphens in the name"); return; }
    setBusy(true); setError(null);
    try {
      const d = await api.updateDataset(dataset.name, { ...(renamed ? { name: name.trim() } : {}), description: description.trim() });
      toast(renamed ? `Renamed to ${d.name}` : "Dataset updated");
      onSaved(d);
    } catch (e) { setError(e instanceof ApiRequestError ? e.message : "Couldn't save the change"); }
    finally { setBusy(false); }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)" }}>Edit dataset</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3, fontFamily: "var(--font-mono)" }}>{dataset.name}</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <div className="field">
            <label>Dataset name</label>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
            <div className="hint">Lowercase letters, digits and hyphens.</div>
          </div>
          {renamed && (
            <div className="callout callout-amber" style={{ marginBottom: 14 }}>
              Renaming moves its files, comments, sharing and training history to the new name. Training runs and pipeline
              definitions that use this dataset follow it; anything outside ML Studio that refers to <code>{dataset.name}</code> won't.
            </div>
          )}
          <div className="field">
            <label>Description</label>
            <textarea className="input" rows={4} maxLength={1000} value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="What's in this dataset, where it comes from, and what it's for" />
          </div>
          {error && <ErrorBanner message={error} />}
        </div>
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={busy || !changed}><Icon name="check" /> {busy ? "Saving…" : "Save changes"}</button>
        </div>
      </div>
    </div>
  );
}

/* ---------------- Upload modal (real file → snapshot) ---------------- */
function UploadModal({ name, structure, existingFiles, onClose, onUploaded }: {
  name: string; structure: "structured" | "unstructured"; existingFiles: string[]; onClose: () => void; onUploaded: () => void;
}) {
  const formats = UPLOAD_FORMATS.filter((f) => f.structure === structure);
  const [picked, setPicked] = useState<{ file: File; format: string | null }[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();
  // One picker, any number of files, every format this dataset type takes; each
  // file's format is read from its extension (the backend verifies the bytes).
  const accept = formats.map((f) => f.accept).join(",");
  const rejected = picked.filter((p) => !p.format);

  async function submit() {
    const ok = picked.filter((p) => p.format);
    if (!ok.length) return;
    setBusy(true); setError(null);
    const failed: string[] = [];
    let done = 0;
    for (const p of ok) {
      setProgress(`Uploading ${done + 1} of ${ok.length} — ${p.file.name}`);
      try { await api.uploadDataset(name, p.file, p.format!); done++; }
      catch (e) { failed.push(`${p.file.name}: ${e instanceof ApiRequestError ? e.message : "upload failed"}`); }
    }
    setProgress(null); setBusy(false);
    if (done) { toast(`Uploaded ${done} file${done === 1 ? "" : "s"} to ${name}`); onUploaded(); }
    if (failed.length) setError(failed.join("\n")); else onClose();
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--ink)" }}>Upload to {name}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>Add files, or new versions of files already here</div>
          </div>
          <button className="drawer-close" onClick={onClose} disabled={busy}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <div className="callout callout-amber" style={{ marginBottom: 14 }}>
            {structure === "unstructured" ? "Unstructured dataset" : "Structured dataset"} — {formats.map((f) => f.id.toUpperCase()).join(", ")}
          </div>
          <p className="cell-sub" style={{ marginBottom: 16 }}>
            A file with a <strong>new name</strong> is added to the dataset; a file with the <strong>same name</strong> as one already here
            becomes that file's next version. Training uses the latest version of every file{structure === "structured" ? ", stacked into one table" : ", combined"}.
            Each upload is checksummed and {structure === "unstructured" ? "checked to really be the format its extension says." : "profiled column by column."}
          </p>
          <div className="field">
            <label>Files</label>
            <input className="input" type="file" multiple accept={accept} disabled={busy}
              onChange={(e) => { setError(null); setPicked([...(e.target.files ?? [])].map((f) => ({ file: f, format: formatForFile(f, structure)?.id ?? null }))); }} />
            <div className="hint">Select one or many files.</div>
          </div>
          {picked.length > 0 && (
            <div className="field">
              {picked.map((p) => (
                <div key={p.file.name} className="kv-row">
                  <span className="k" style={{ wordBreak: "break-all" }}>{p.file.name}</span>
                  <span className="v" style={{ color: p.format ? undefined : "var(--red)" }}>
                    {!p.format ? "not an accepted format"
                      : existingFiles.includes(p.file.name) ? `new version · ${(p.file.size / 1024).toFixed(0)} KB`
                      : `new file · ${(p.file.size / 1024).toFixed(0)} KB`}
                  </span>
                </div>
              ))}
            </div>
          )}
          {rejected.length > 0 && <div className="hint" style={{ color: "var(--red)" }}>{rejected.length} file(s) aren't a format this dataset accepts and will be skipped.</div>}
          {progress && <div className="callout callout-teal" style={{ marginTop: 10 }}>{progress}</div>}
          {error && <div style={{ whiteSpace: "pre-wrap" }}><ErrorBanner message={error} /></div>}
        </div>
        <div className="drawer-foot">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy || !picked.some((p) => p.format)}>
            <Icon name="upload" /> {busy ? "Uploading…" : `Upload ${picked.filter((p) => p.format).length || ""} file${picked.filter((p) => p.format).length === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
