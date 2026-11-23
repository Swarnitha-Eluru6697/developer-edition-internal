import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

/** Bytes-ish size string ("4.2 GB", "820 MB") -> MB, for the Datasets bar widths.
 *  Iceberg-sourced datasets report no size at all; those are shown at full width
 *  with their source as the value instead of a fabricated number. */
function sizeToMb(size?: string | null): number | null {
  if (!size) return null;
  const m = /^([\d.]+)\s*(TB|GB|MB|KB)$/i.exec(size.trim());
  if (!m) return null;
  const n = parseFloat(m[1]);
  const mult: Record<string, number> = { TB: 1024 * 1024, GB: 1024, MB: 1, KB: 1 / 1024 };
  return n * mult[m[2].toUpperCase()];
}

export default function DashboardPage() {
  const { can } = usePermissions();
  const navigate = useNavigate();
  const models = useApi(api.getModels, []);
  const endpoints = useApi(api.getEndpoints, []);
  // Compute-pool capacity is cluster data (mlstudio:clusters:view); without it the card is not shown.
  const canPools = can(PERM.clusters);
  const pools = useApi(() => (canPools ? api.getComputePools() : Promise.resolve(null)), [canPools]);
  const datasets = useApi(api.getDatasets, []);
  const pending = useApi(api.getPendingPromotions, []);
  const runs = useApi(api.getTrainingRuns, []);
  const trainingActivity = useApi(() => api.getTrainingActivity(14), []);
  const spend = useApi(() => api.getComputeSpend(7), []);
  const auditLog = useApi(() => api.getActivity(8), []);

  const anyError = models.error || endpoints.error || pools.error || datasets.error || pending.error;

  const runList = runs.data ?? [];
  const activeRuns = runList.filter((r: any) => r.status === "queued" || r.status === "running");
  const runsByProfile = activeRuns.reduce((acc: Record<string, number>, r: any) => {
    const p = r.computeProfile ?? r.compute_profile ?? "unknown";
    acc[p] = (acc[p] ?? 0) + 1;
    return acc;
  }, {});

  const poolBy = (id: string) => pools.data?.pools.find((p) => p.id === id);
  const gpu4x = poolBy("gpu-4x");
  const gpu1x = poolBy("gpu-1x");

  // Backend / domain mix — counted from the real catalog, using the same
  // domain/kind trio the catalog itself is keyed on.
  const mlBase = models.data?.filter((m: any) => m.domain === "ml" && m.kind !== "adapter").length ?? 0;
  const llmBase = models.data?.filter((m: any) => m.domain === "llm" && m.kind !== "adapter").length ?? 0;
  const adapters = models.data?.filter((m: any) => m.kind === "adapter").length ?? 0;
  const mixMax = Math.max(mlBase, llmBase, adapters, 1);

  const dsMax = Math.max(
    ...(datasets.data ?? []).map((d: any) => sizeToMb(d.size) ?? 0),
    1
  );

  // Recent activity, from the real version history the catalog already returns.
  const activity = (models.data ?? [])
    .flatMap((m: any) =>
      (m.versions ?? []).map((v: any) => ({
        model: m.id,
        version: v.v,
        alias: v.alias,
        note: v.note as string,
        updated: m.updated as string,
      }))
    )
    .filter((a) => a.alias !== "archived")
    .slice(0, 5);

  // Charted from the server's real per-day aggregate rather than re-bucketing
  // in the browser — the backend already returns a complete, gap-filled series.
  const ta = trainingActivity.data;
  // Recent activity = real audited actions first (promotions, rollbacks), then
  // the real version history behind them. Both are genuine records; neither is
  // seeded, so a brand-new install legitimately shows very little here.
  const auditItems = (auditLog.data ?? []).map((a) => ({
    key: `audit-${a.createdAt}-${a.target}`,
    dot: "",
    text: `${a.action} — ${a.target}`,
    sub: `${a.actor} · ${new Date(a.createdAt).toLocaleString()}`,
  }));
  const versionItems = activity.map((a, i) => ({
    key: `ver-${a.model}-${a.version}-${i}`,
    dot: a.alias === "challenger" ? " amber" : a.alias === "staging" ? " navy" : "",
    text: `v${a.version} ${a.alias} — ${a.model}`,
    sub: a.note,
  }));
  const feed = [...auditItems, ...versionItems].slice(0, 6);

  const spark = (() => {
    if (!ta?.hasData) return null;
    const max = Math.max(...ta.series.map((p) => p.runs), 1);
    const W = 380, H = 110, pad = 8;
    const step = (W - pad * 2) / Math.max(ta.series.length - 1, 1);
    return {
      pts: ta.series
        .map((p, i) => `${(pad + i * step).toFixed(1)},${(H - pad - (p.runs / max) * (H - pad * 2)).toFixed(1)}`)
        .join(" "),
      total: ta.total,
    };
  })();

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Dashboard</h1>
            <p>acme-corp's data plane — what's running, what's training, what needs attention.</p>
          </div>
          <button hidden={!can(PERM.train)} className="btn btn-primary" onClick={() => navigate("/train")}>
            <Icon name="plus" />
            Train a new model
          </button>
        </div>

        {anyError && (
          <div className="error-banner">
            Couldn't reach the ML Studio API — is the backend running? Start it with `npm run dev` in InaiEra_AIMLStudio_backend. ({anyError})
          </div>
        )}

        <div className="stat-grid">
          <div className="stat-card">
            <div className="s-top">
              <span className="s-label">Models registered</span>
              <span className="s-icon icon-bg-navy"><Icon name="grid" /></span>
            </div>
            <div className="s-value">{models.loading ? "…" : models.data?.length ?? "—"}</div>
            <div className="s-delta flat">{models.data?.length ? `${mlBase} ML · ${llmBase} LLM · ${adapters} adapters` : "—"}</div>
          </div>

          <div className="stat-card">
            <div className="s-top">
              <span className="s-label">Active training runs</span>
              <span className="s-icon icon-bg-amber"><Icon name="clock" /></span>
            </div>
            <div className="s-value">{runs.loading ? "…" : activeRuns.length}</div>
            <div className="s-delta flat">
              {activeRuns.length
                ? Object.entries(runsByProfile).map(([p, n]) => `${n} on ${p}`).join(" · ")
                : "nothing queued or running"}
            </div>
          </div>

          <div className="stat-card">
            <div className="s-top">
              <span className="s-label">Serving endpoints</span>
              <span className="s-icon icon-bg-teal"><Icon name="plug" /></span>
            </div>
            <div className="s-value">{endpoints.loading ? "…" : endpoints.data?.length ?? "—"}</div>
            <div className="s-delta up">
              {endpoints.data?.every((e: any) => e.status === "healthy") ? "All within latency SLO" : "Check endpoint health"}
            </div>
          </div>

          <div className="stat-card">
            <div className="s-top">
              <span className="s-label">Pending promotions</span>
              <span className="s-icon icon-bg-purple"><Icon name="arrow-up" /></span>
            </div>
            <div className="s-value">{pending.loading ? "…" : pending.data?.length ?? "—"}</div>
            <div className="s-delta warn">
              {pending.data?.length
                ? `${pending.data[0].modelId} v${pending.data[0].candidateVersion} awaiting review`
                : "nothing awaiting review"}
            </div>
          </div>
        </div>
      </div>

      <div className="dash-grid">
        {/* ---------------- column 1 ---------------- */}
        <div className="dash-col">
          <div className="card card-pad">
            <div className="drawer-section-title">GPU pool utilization</div>
            <div className="gauge-row">
              {[gpu4x, gpu1x].map((p, i) =>
                p ? (
                  <div key={p.id}>
                    <div className="gauge" style={{ ["--pct" as any]: p.utilizationPct }}>
                      <span className="gauge-val">{p.utilizationPct}%</span>
                    </div>
                    <div className="gauge-label">{p.label} pool</div>
                  </div>
                ) : (
                  <div key={i}>
                    <div className="gauge" style={{ ["--pct" as any]: 0 }}><span className="gauge-val">—</span></div>
                    <div className="gauge-label">pool unavailable</div>
                  </div>
                )
              )}
            </div>
            <div className="callout callout-navy" style={{ marginTop: 14 }}>
              Multi-LoRA serving keeps several adapters on one shared base-model deployment — no
              dedicated GPU per fine-tune.
            </div>
          </div>

          <div className="card card-pad">
            <div className="drawer-section-title">Recent activity</div>
            <div className="activity">
              {models.loading && <div className="empty-hint">Loading…</div>}
              {!models.loading && feed.length === 0 && (
                <div className="empty-hint">No registered model versions yet.</div>
              )}
              {feed.map((f) => (
                <div className="activity-item" key={f.key}>
                  <div className={"activity-dot" + f.dot} />
                  <div>
                    <div className="activity-text">{f.text}</div>
                    <div className="activity-sub">{f.sub}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ---------------- column 2 ---------------- */}
        <div className="dash-col">
          <div className="card card-pad">
            <div className="drawer-section-title">Training runs — last 14 days</div>
            {spark ? (
              <>
                <svg viewBox="0 0 380 110" style={{ width: "100%", height: 100 }}>
                  <defs>
                    <linearGradient id="gArea" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--brand)" />
                      <stop offset="100%" stopColor="var(--brand)" stopOpacity="0" />
                    </linearGradient>
                  </defs>
                  <polyline points={spark.pts} fill="none" stroke="var(--brand)" strokeWidth="2.5" />
                </svg>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10.5, color: "var(--text-faint)", marginTop: 2 }}>
                  <span>14 days ago</span>
                  <span>today</span>
                </div>
              </>
            ) : (
              <div className="empty-hint">
                No training runs recorded yet — launch one from the Train wizard and it will chart here.
              </div>
            )}
          </div>

          <div className="card card-pad">
            <div className="drawer-section-title">Backend / domain mix</div>
            <div className="bar-list">
              {[
                { label: "ML base", n: mlBase },
                { label: "LLM base", n: llmBase },
                { label: "Adapters", n: adapters },
              ].map((r) => (
                <div className="bar-row" key={r.label}>
                  <span className="b-label">{r.label}</span>
                  <div className="bar-track">
                    <div className="bar-fill" style={{ width: `${(r.n / mixMax) * 100}%` }} />
                  </div>
                  <span className="b-val">{r.n}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="card card-pad">
            <div className="drawer-section-title">Datasets</div>
            <div className="bar-list">
              {datasets.loading && <div className="empty-hint">Loading…</div>}
              {!datasets.loading && (datasets.data?.length ?? 0) === 0 && (
                <div className="empty-hint">No datasets registered yet.</div>
              )}
              {datasets.data?.map((d: any) => {
                const mb = sizeToMb(d.size);
                const isIceberg = d.source === "iceberg";
                return (
                  <div className="bar-row" key={d.name}>
                    <span className="b-label">{d.name}</span>
                    <div className="bar-track">
                      <div
                        className="bar-fill"
                        style={{
                          width: isIceberg || mb === null ? "100%" : `${Math.max((mb / dsMax) * 100, 4)}%`,
                          background: isIceberg ? "linear-gradient(90deg,var(--amber),var(--amber-700))" : undefined,
                        }}
                      />
                    </div>
                    <span className="b-val">{isIceberg ? "Iceberg" : d.size ?? "—"}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* ---------------- column 3 ---------------- */}
        <div className="dash-col">
          <div className="card card-pad">
            <div className="drawer-section-title">Needs attention</div>
            {pending.loading && <div className="empty-hint">Loading…</div>}
            {!pending.loading && (pending.data?.length ?? 0) === 0 && (
              <div className="empty-hint">Nothing is awaiting a promotion decision.</div>
            )}
            {pending.data?.map((p: any, i: number) => (
              <div className={"callout " + (i === 0 ? "callout-amber" : "callout-navy")} key={p.modelId} style={{ marginBottom: 10 }}>
                <strong>{p.modelId} v{p.candidateVersion}</strong> {p.note}
                <div style={{ marginTop: 8 }}>
                  <button className="btn btn-sm btn-primary" onClick={() => navigate("/deploy")}>
                    Review &amp; promote
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="card card-pad">
            <div className="drawer-section-title">Compute profile spend (last 7 days)</div>
            {spend.loading && <div className="empty-hint">Loading…</div>}
            {spend.data?.profiles.map((p) => (
              <div className="kv-row" key={p.id}>
                <span className="k">{p.label}</span>
                <span className="v">
                  {p.hours} hrs{p.runs ? ` · ${p.runs} run${p.runs === 1 ? "" : "s"}` : ""}
                </span>
              </div>
            ))}
            <div className="hint" style={{ marginTop: 8 }}>
              {spend.data?.hasData
                ? "Measured worker-held wall clock (claimed → last heartbeat), resolved from the Compute Profile chosen in the Train Wizard — not raw Kubernetes billing."
                : "No training run has consumed compute yet, so every profile is genuinely at zero — this is measured, not estimated."}
            </div>
          </div>

          {canPools && <div className="card card-pad">
            <div className="drawer-section-title">Compute pool capacity</div>
            {pools.loading && <div className="empty-hint">Loading…</div>}
            {pools.data?.pools.map((p) => (
              <div className="kv-row" key={p.id}>
                <span className="k">{p.label}</span>
                <span className="v">
                  {p.inUse}/{p.capacity}
                  {p.queuedJobs > 0 ? ` · ${p.queuedJobs} queued` : ""}
                </span>
              </div>
            ))}
          </div>}
        </div>
      </div>
    </div>
  );
}
