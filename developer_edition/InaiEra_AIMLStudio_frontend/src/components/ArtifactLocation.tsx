import { useState } from "react";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "./Icons";
import IconAction from "./IconAction";

/** Where a version's weights really are. The recorded URI ("s3://bucket/...") opened
 *  nowhere locally — the bucket is in MinIO, not AWS — and couldn't say whether the
 *  file was still there. This shows the storage, bucket and path, the size, whether it
 *  exists, and offers a download and the MinIO console. */
export function ArtifactLocation({ uri }: { uri: string | null | undefined }) {
  const loc = useApi(() => (uri ? api.locateArtifact(uri) : Promise.resolve(null)), [uri]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!uri) return <div className="hint" style={{ margin: 0 }}>No artifact was recorded for this version.</div>;
  if (loc.loading) return <div className="hint" style={{ margin: 0 }}>Locating artifact…</div>;
  if (loc.error || !loc.data) return <div className="hint" style={{ margin: 0 }}>{uri} <span style={{ color: "var(--amber-700)" }}>(couldn't look it up: {loc.error})</span></div>;
  const a = loc.data;
  const size = a.sizeBytes == null ? "—" : a.sizeBytes > 1e6 ? `${(a.sizeBytes / 1e6).toFixed(1)} MB` : `${(a.sizeBytes / 1e3).toFixed(1)} KB`;
  const kv = (k: string, v: React.ReactNode) => (
    <div className="kv-row"><span className="k">{k}</span><span className="v" style={{ overflowWrap: "anywhere" }}>{v}</span></div>
  );
  return (
    <div>
      {kv("Status", a.exists
        ? <span style={{ color: "var(--teal-700)", fontWeight: 600 }}>Stored · {size}</span>
        : <span style={{ color: "var(--amber-700)", fontWeight: 600 }}>Missing from storage — re-train to regenerate it</span>)}
      {kv("Storage", `${a.storage}${a.endpoint ? ` · ${a.endpoint}` : ""}`)}
      {kv("Bucket", <code>{a.bucket}</code>)}
      {kv("Path", <code>{a.key}</code>)}
      {a.lastModified && kv("Written", new Date(a.lastModified).toLocaleString())}
      {kv("URI", <code>{a.uri}</code>)}
      <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
        <IconAction icon="download" disabled={!a.exists || busy} align="start"
          label={busy ? "Downloading artifact…" : a.exists ? `Download artifact ${a.key.split("/").pop()}` : "Download artifact · missing from storage"}
          onClick={async () => { setBusy(true); setErr(null); try { await api.downloadArtifact(a.uri, a.key.split("/").slice(-3).join("__")); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } }} />
        {a.consoleUrl && (
          <a className="btn btn-secondary btn-sm" href={a.consoleUrl} target="_blank" rel="noreferrer">
            <Icon name="external" /> Open in MinIO console
          </a>
        )}
      </div>
      {err && <div className="hint" style={{ color: "var(--amber-700)", marginTop: 6 }}>{err}</div>}
    </div>
  );
}
