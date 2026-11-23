import { useState } from "react";
import { Icon } from "../../components/Icons";
import { api } from "../../api/client";
import { useApi } from "../../api/useApi";
import type { ProfileColumn, DatasetProfile } from "../../types/domain";

// Brand ramp, so dataset profiling reads as part of the same product as
// everything else rather than carrying its own blue palette.
const PIE_COLORS = ["#F97316", "#FB923C", "#FDBA74", "#FFEDD5"];
function fmt(n: number | null | undefined) {
  if (n === null || n === undefined) return "—";
  return Number.isInteger(n) ? n.toLocaleString() : Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 });
}

// Real file preview — a "file" here is one immutable snapshot version; its
// column profile (histograms, quantiles, cardinality, sample rows) is the
// real profile computed by dataset_profiler.py at upload time.
export default function FilePreviewModal({ datasetName, version, label, onClose }: {
  datasetName: string; version: number; label: string; onClose: () => void;
}) {
  const snap = useApi(() => api.getDatasetSnapshot(datasetName, version), [datasetName, version]);
  const [ptab, setPtab] = useState<"detail" | "compact" | "column">("detail");
  const profile = snap.data?.profile as DatasetProfile | undefined;
  // Documents, slides, images, audio and archives have no columns — their
  // profile says what kind of file it is instead.
  const fileKind = (profile as any)?.kind as string | undefined;

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-wide">
        <div className="preview-head">
          <div>
            <div className="preview-head-title">{label}</div>
            <div className="preview-head-sub">{snap.data ? `${snap.data.format.toUpperCase()} · ${fileKind ? `${snap.data.rowCount.toLocaleString()} file${snap.data.rowCount === 1 ? "" : "s"}` : `${snap.data.rowCount.toLocaleString()} rows`} · sha256 ${snap.data.checksum.slice(0, 12)}…` : "loading…"}</div>
          </div>
          <div className="preview-head-actions">
            <button className="preview-icon-btn has-tip tip-end" data-tip="Close preview" aria-label="Close preview" onClick={onClose}><Icon name="x" /></button>
          </div>
        </div>
        <div className="preview-body">
          {snap.loading && <div className="spinner">Loading profile…</div>}
          {snap.error && <div className="error-banner">{snap.error}</div>}
          {profile && profile.ok && fileKind && <FileFacts p={profile as any} />}
          {profile && profile.ok && !fileKind && (
            <>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div className="preview-tabs">
                  {(["detail", "compact", "column"] as const).map((t) => (
                    <button key={t} className={"preview-tab" + (ptab === t ? " active" : "")} onClick={() => setPtab(t)} style={{ textTransform: "capitalize" }}>{t}</button>
                  ))}
                </div>
                <div className="preview-colcount">{profile.columnCount} columns · {profile.rowCount.toLocaleString()} rows · {profile.duplicateRowCount} duplicate rows</div>
              </div>

              {ptab === "detail" && (
                <div>
                  <div className="about-file">
                    <div className="t">About this file</div>
                    <div className="d">Snapshot v{version} of <strong>{datasetName}</strong> — {profile.rowCount.toLocaleString()} rows across {profile.columnCount} columns. Every statistic below is computed from the actual uploaded file at upload time.</div>
                  </div>
                  {profile.columns.map((c) => <DetailCol key={c.name} c={c} rowCount={profile.rowCount} />)}
                </div>
              )}

              {ptab === "compact" && (
                <div className="compact-table-wrap">
                  <table className="compact-table">
                    <thead><tr>{(profile.sampleColumns ?? profile.columns.map((c) => c.name)).map((c) => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>
                      {(profile.sampleRows ?? []).map((r, i) => (
                        <tr key={i}>{(profile.sampleColumns ?? []).map((c) => <td key={c}>{r[c] === null || r[c] === undefined ? "—" : String(r[c])}</td>)}</tr>
                      ))}
                      {(!profile.sampleRows || profile.sampleRows.length === 0) && <tr><td colSpan={profile.columnCount}><div className="empty-hint">No sample rows captured for this snapshot.</div></td></tr>}
                    </tbody>
                  </table>
                </div>
              )}

              {ptab === "column" && profile.columns.map((c) => <ColumnStat key={c.name} c={c} rowCount={profile.rowCount} />)}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function catRows(c: ProfileColumn, rowCount: number) {
  const total = rowCount - (c.missingCount ?? 0);
  return (c.topValues ?? []).map((v) => ({ label: v.value, count: v.count, pct: total ? Math.round((v.count / total) * 100) : 0 }));
}

function DetailCol({ c, rowCount }: { c: ProfileColumn; rowCount: number }) {
  return (
    <div className="col-row">
      <div className="col-name-cell">
        <span className="col-type-icon"><Icon name={c.kind === "numeric" ? "hash" : "type"} /></span>
        <div><div className="col-name-text">{c.name}</div><div className="col-desc-text">{c.dtype} · {c.kind}{c.missingPct > 0 ? ` · ${c.missingPct}% missing` : ""}</div></div>
      </div>
      <div>
        {c.kind === "numeric" ? (
          c.histogram && c.histogram.length ? (
            <><div className="mini-hist">{c.histogram.map((h, i) => {
              const max = Math.max(...(c.histogram as number[]), 1);
              return <div key={i} className="bar" style={{ height: Math.round((h / max) * 100) + "%" }} />;
            })}</div><div className="hist-range"><span>{fmt(c.min)}</span><span>{fmt(c.max)}</span></div></>
          ) : <div className="cat-breakdown"><div className="cat-breakdown-row"><span className="lbl">range</span><span className="pct">{fmt(c.min)} – {fmt(c.max)}</span></div></div>
        ) : (
          <div className="cat-breakdown">
            {catRows(c, rowCount).map((r) => <div className="cat-breakdown-row" key={r.label}><span className="lbl">{r.label}</span><span className="pct">{r.pct}%</span></div>)}
            {(c.cardinality ?? 0) > (c.topValues?.length ?? 0) && <div className="cat-breakdown-row"><span className="lbl" style={{ color: "var(--text-faint)" }}>…{(c.cardinality ?? 0) - (c.topValues?.length ?? 0)} more</span><span className="pct">{c.cardinality} distinct</span></div>}
          </div>
        )}
      </div>
    </div>
  );
}

function ColumnStat({ c, rowCount }: { c: ProfileColumn; rowCount: number }) {
  const validPct = (100 - c.missingPct).toFixed(1);
  let visual: JSX.Element; let extra: JSX.Element | null = null;

  if (c.kind === "numeric") {
    const hist = c.histogram || [];
    const max = Math.max(...hist, 1);
    visual = (<><div className="mini-hist" style={{ height: 100 }}>{hist.length ? hist.map((h, i) => <div key={i} className="bar" style={{ height: Math.round((h / max) * 100) + "%", background: "var(--brand)" }} />) : <div className="bar" style={{ height: "100%", flex: 1, background: "var(--brand)" }} />}</div><div className="hist-range"><span>{fmt(c.min)}</span><span>{fmt(c.max)}</span></div></>);
    extra = (<>
      <div className="stat-kv" style={{ marginTop: 8 }}><span className="k">Mean</span><span className="v">{fmt(c.mean)}</span></div>
      <div className="stat-kv"><span className="k">Std. deviation</span><span className="v">{fmt(c.std)}</span></div>
      <div className="stat-kv"><span className="k">Outliers (IQR)</span><span className="v">{c.outlierCount ?? 0} ({c.outlierPct ?? 0}%)</span></div>
      <div className="quantile-row"><span>Min</span><span className="v">{fmt(c.min)}</span></div>
      <div className="quantile-row"><span>25%</span><span className="v">{fmt(c.p25)}</span></div>
      <div className="quantile-row"><span>50%</span><span className="v">{fmt(c.median)}</span></div>
      <div className="quantile-row"><span>75%</span><span className="v">{fmt(c.p75)}</span></div>
      <div className="quantile-row"><span>Max</span><span className="v">{fmt(c.max)}</span></div>
    </>);
  } else {
    const rows = catRows(c, rowCount);
    let acc = 0;
    const parts = rows.map((r, i) => { const s = acc; acc += r.pct; return `${PIE_COLORS[i % PIE_COLORS.length]} ${s}% ${acc}%`; }).join(", ");
    visual = <div className="pie-chart" style={{ background: rows.length ? `conic-gradient(${parts})` : "var(--surface-sunken)" }} />;
    extra = (<>
      <div className="stat-kv" style={{ marginTop: 8 }}><span className="k">Distinct</span><span className="v">{c.cardinality}</span></div>
      {rows.map((r, i) => <div className="stat-kv" key={r.label}><span className="k"><span className="sw" style={{ background: PIE_COLORS[i % PIE_COLORS.length] }} />{r.label}</span><span className="v">{r.pct}%</span></div>)}
    </>);
  }

  return (
    <div className="colstat-block">
      <div className="col-name-cell">
        <span className="col-type-icon"><Icon name={c.kind === "numeric" ? "hash" : "type"} /></span>
        <div><div className="col-name-text">{c.name}</div><div className="col-desc-text">{c.dtype} · {c.kind}</div></div>
      </div>
      <div className="colstat-grid">
        <div>{visual}</div>
        <div>
          <div className="quality-bar">
            <div style={{ width: validPct + "%", background: "var(--brand)" }} />
            <div style={{ width: c.missingPct + "%", background: "var(--amber)" }} />
          </div>
          <div className="stat-kv"><span className="k"><span className="sw" style={{ background: "var(--brand)" }} />Valid</span><span className="v">{validPct}%</span></div>
          <div className="stat-kv"><span className="k"><span className="sw" style={{ background: "var(--amber)" }} />Missing</span><span className="v">{c.missingPct}% ({c.missingCount})</span></div>
          {extra}
        </div>
      </div>
    </div>
  );
}

/** What was measured from a non-tabular file at upload (file_profiler.py /
 *  the document profiler). Only fields the profile actually has are shown. */
function FileFacts({ p }: { p: Record<string, any> }) {
  const size = (b?: number) => (b == null ? null : b >= 1e6 ? `${(b / 1e6).toFixed(2)} MB` : b >= 1e3 ? `${(b / 1e3).toFixed(1)} KB` : `${b} B`);
  const rows: [string, string | null][] = [
    ["Kind", p.kind],
    ["Size", size(p.bytes)],
    ["Slides", p.slides != null ? String(p.slides) : null],
    ["Words", p.words != null ? Number(p.words).toLocaleString() : null],
    ["Lines", p.lines != null ? Number(p.lines).toLocaleString() : null],
    ["Characters", p.characters != null ? Number(p.characters).toLocaleString() : null],
    ["Dimensions", p.width != null ? `${p.width} × ${p.height} px` : null],
    ["Color", p.colorMode ? `${p.colorMode}, ${p.bitDepth}-bit` : null],
    ["Duration", p.durationSeconds != null ? `${p.durationSeconds}s` : null],
    ["Audio", p.sampleRate ? `${p.sampleRate.toLocaleString()} Hz · ${p.channels} channel${p.channels === 1 ? "" : "s"} · ${p.bitsPerSample}-bit` : null],
    ["Files inside", p.files != null ? String(p.files) : null],
    ["Uncompressed", size(p.uncompressedBytes)],
    ["File types", p.fileTypes ? Object.entries(p.fileTypes).map(([k, v]) => `${k} (${v})`).join(", ") : null],
  ];
  return (
    <div>
      <div className="hint" style={{ marginBottom: 12 }}>
        Measured from the uploaded file itself — column statistics don't apply to this kind of file.
        {p.format === "ppt" && " Slide count isn't read from the legacy .ppt format; upload .pptx for slide and word counts."}
      </div>
      {rows.filter(([, v]) => v).map(([k, v]) => (
        <div className="kv-row" key={k}><span className="k">{k}</span><span className="v">{v}</span></div>
      ))}
      {Array.isArray(p.sample) && p.sample.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="drawer-section-title">First files in the archive</div>
          {p.sample.map((f: string) => <div key={f} className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)" }}>{f}</div>)}
        </div>
      )}
    </div>
  );
}
