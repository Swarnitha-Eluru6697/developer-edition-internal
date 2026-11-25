import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import { StatusPill } from "../components/StatusPill";
import { TestDrawer } from "../components/TestDrawer";
import { domainBadge } from "../components/modelBadges";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";
import type { Deployment, PendingPromotion, PromotionCheck, PromotionVersion } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

type Tab = "pending" | "live";

export default function DeployPage() {
  const [tab, setTab] = useState<Tab>("pending");
  const deployments = useApi(api.getDeployments, []);
  const pending = useApi(api.getPendingPromotions, []);
  const [query] = usePageSearch(tab === "pending" ? "Search models awaiting promotion…" : "Search deployed models…");
  const pendingShown = (pending.data ?? []).filter((p) => matchesQuery(query, p.modelId, p.task, p.domain, p.framework, p.note, `v${p.candidateVersion}`));
  const deployedShown = (deployments.data ?? []).filter((d) => matchesQuery(query, d.modelId, d.task, d.domain, d.runtime, d.healthStatus, `v${d.currentVersion}`));
  const [reviewing, setReviewing] = useState<PendingPromotion | null>(null);
  const [inspecting, setInspecting] = useState<Deployment | null>(null);

  const reload = () => { deployments.reload(); pending.reload(); };

  // Arriving from a succeeded pipeline (?model=&version=): open that candidate's review,
  // or, if it isn't pending, show it under "Currently deployed".
  const [params, setParams] = useSearchParams();
  const focusModel = params.get("model");
  const focusVersion = params.get("version");
  const [focusNote, setFocusNote] = useState<string | null>(null);
  useEffect(() => {
    if (!focusModel || !pending.data || !deployments.data) return;
    const cand = pending.data.filter((p) => p.modelId === focusModel);
    const match = cand.find((p) => String(p.candidateVersion) === focusVersion) ?? cand[cand.length - 1];
    const live = deployments.data.find((d) => d.modelId === focusModel);
    if (match) { setTab("pending"); setReviewing(match); setFocusNote(null); }
    else if (live) { setTab("live"); setInspecting(live); setFocusNote(`${focusModel} has no version waiting for promotion — it is already deployed.`); }
    else setFocusNote(`${focusModel}${focusVersion ? ` v${focusVersion}` : ""} has nothing to promote yet.`);
    setParams((p) => { const n = new URLSearchParams(p); n.delete("model"); n.delete("version"); return n; }, { replace: true });
  }, [focusModel, focusVersion, pending.data, deployments.data, setParams]);

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Deploy &amp; promote</h1>
            <p>Alias-based promotion — moving "champion" is a metadata change, never a rewritten artifact.</p>
          </div>
        </div>

        <div className="tabs">
          <button className={"tab" + (tab === "pending" ? " active" : "")} onClick={() => setTab("pending")}>
            Awaiting promotion
            {pending.data?.length ? <span className="nav-badge" style={{ marginLeft: 4 }}>{pending.data.length}</span> : null}
          </button>
          <button className={"tab" + (tab === "live" ? " active" : "")} onClick={() => setTab("live")}>Currently deployed</button>
        </div>
      </div>

      {focusNote && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{focusNote}</div>}
      {tab === "pending" && (
        <div style={{ marginTop: 16 }}>
          {pending.loading && <div className="empty-hint">Loading…</div>}
          <div className="opt-grid">
            {pendingShown.map((p) => (
              <div className="opt-card" style={{ cursor: "pointer" }} key={`${p.modelId}-${p.candidateVersion}`} onClick={() => setReviewing(p)}>
                <div className="opt-card-head">
                  <span className="opt-card-title">{p.modelId}</span>
                  <span className="alias alias-staging" style={{ fontSize: 10.5 }}>v{p.candidateVersion} candidate</span>
                </div>
                <div className="opt-card-desc">{p.note}</div>
                <div className="dataset-card-stats" style={{ marginTop: 10 }}>
                  <div className="dataset-card-stat">
                    <div className="n">{domainBadge(p.domain, p.framework)}</div>
                    <div className="l">{p.task}</div>
                  </div>
                  <div className="dataset-card-stat">
                    <div className="n">
                      {p.currentChampionVersion != null ? `v${p.candidateVersion} → v${p.currentChampionVersion}` : `v${p.candidateVersion}`}
                    </div>
                    <div className="l">{p.currentChampionVersion != null ? "Replacing" : "First deployment"}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
          {!!pending.data?.length && !pendingShown.length && (
            <div className="card card-pad"><div className="empty-hint">No models awaiting promotion match “{query.trim()}”.</div></div>
          )}
          {!pending.loading && !pending.data?.length && (
            <div className="card card-pad"><div className="empty-hint">Nothing is awaiting promotion right now.</div></div>
          )}
        </div>
      )}

      {tab === "live" && (
        <div style={{ marginTop: 16 }}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: "24%" }}>Model</th>
                  <th style={{ width: "12%" }}>Alias</th>
                  <th style={{ width: "18%" }}>Runtime</th>
                  <th style={{ width: "14%" }}>Replicas</th>
                  <th style={{ width: "16%" }}>Health</th>
                  <th style={{ width: "16%" }} />
                </tr>
              </thead>
              <tbody>
                {deployedShown.map((d) => (
                  <tr key={d.modelId} onClick={() => setInspecting(d)}>
                    <td>
                      <div className="cell-name">{d.modelId}</div>
                      <div className="cell-sub">v{d.currentVersion} · {d.task}</div>
                    </td>
                    <td><span className="alias alias-champion">champion</span></td>
                    <td><span className="tag">{d.runtime}</span></td>
                    <td style={{ fontSize: 12 }}>{d.replicas}</td>
                    <td><StatusPill status={d.healthStatus} /></td>
                    <td>
                      <IconAction icon="eye" label={`View deployment details for ${d.modelId}`} tip="up" align="end" onClick={(e) => { e.stopPropagation(); setInspecting(d); }} />
                    </td>
                  </tr>
                ))}
                {!!deployments.data?.length && !deployedShown.length && (
                  <tr><td colSpan={6}><div className="empty-hint">No deployed models match “{query.trim()}”.</div></td></tr>
                )}
                {!deployments.data?.length && (
                  <tr><td colSpan={6}><div className="empty-hint">Nothing is deployed yet.</div></td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {reviewing && (
        <PromotionReview
          modelId={reviewing.modelId}
          version={reviewing.candidateVersion}
          onClose={() => setReviewing(null)}
          onPromoted={() => { setReviewing(null); reload(); setTab("live"); }}
        />
      )}
      {inspecting && (
        <DeployedModelPanel deployment={inspecting} onClose={() => setInspecting(null)} onChanged={reload} />
      )}
    </div>
  );
}

function checkIcon(status: PromotionCheck["status"]) {
  if (status === "pass") return <span style={{ color: "var(--teal-700)", fontWeight: 700 }}>✓</span>;
  if (status === "fail") return <span style={{ color: "var(--red)", fontWeight: 700 }}>✗</span>;
  return <span style={{ color: "var(--text-faint)", fontWeight: 700 }}>?</span>;
}

function VersionCard({ label, aliasClass, v }: { label: string; aliasClass: string; v: PromotionVersion }) {
  return (
    <div className="card card-pad">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span className={`alias ${aliasClass}`}>{label}</span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-faint)" }}>v{v.version}</span>
      </div>
      <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginBottom: 14 }}>{v.note}</div>
      {v.metrics ? (
        Object.entries(v.metrics).map(([k, val]) => (
          <div className="kv-row" key={k}><span className="k">{k}</span><span className="v">{String(val)}</span></div>
        ))
      ) : (
        // Deliberately not zeros or dashes-as-numbers: this version has no
        // linked training run, so no metrics were ever measured for it.
        <div className="empty-hint" style={{ padding: "14px 8px" }}>
          No measured metrics — this version isn't linked to a training run.
        </div>
      )}
      <div className="kv-row">
        <span className="k">Trained on</span>
        <span className="v">
          {v.datasetName ? `${v.datasetName}${v.datasetSnapshotVersion ? ` v${v.datasetSnapshotVersion}` : ""}` : "not recorded"}
        </span>
      </div>
      <div className="kv-row">
        <span className="k">Artifact</span>
        <span className="v" style={{ fontSize: 11, fontFamily: "var(--font-mono)", wordBreak: "break-all", maxWidth: "70%" }}>
          {v.weightsUri ?? "none recorded"}
        </span>
      </div>
    </div>
  );
}

function PromotionReview({
  modelId, version, onClose, onPromoted,
}: { modelId: string; version: number; onClose: () => void; onPromoted: () => void }) {
  const { can } = usePermissions();
  const detail = useApi(() => api.getPromotionDetail(modelId, version), [modelId, version]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function promote() {
    setBusy(true);
    setError(null);
    try {
      await api.promote(modelId, version);
      onPromoted();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const d = detail.data;
  const failed = d?.checklist.filter((c) => c.status === "fail") ?? [];

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-wide">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)" }}>{modelId}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>
              Promotion review — v{version} candidate
            </div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {detail.loading && <div className="empty-hint">Loading…</div>}
          {detail.error && <div className="callout callout-amber">{detail.error}</div>}
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          {d && (
            <>
              <div className="drawer-section">
                <div className="field-row" style={{ alignItems: "stretch" }}>
                  <VersionCard label="Candidate" aliasClass="alias-staging" v={d.candidate} />
                  {d.champion ? (
                    <VersionCard label="Current champion" aliasClass="alias-champion" v={d.champion} />
                  ) : (
                    <div className="card card-pad">
                      <div className="empty-hint">
                        Nothing is deployed for this model yet — promoting this candidate would be its
                        first deployment, so there's no champion to compare against.
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div className="drawer-section">
                <div className="drawer-section-title">Promotion checklist</div>
                <div className="hint" style={{ marginBottom: 10 }}>
                  Each line is a real check over recorded data. A "?" means the underlying fact isn't
                  recorded for this model — which is not the same as the check failing.
                </div>
                {d.checklist.map((c) => (
                  <div className="kv-row" style={{ alignItems: "flex-start" }} key={c.label}>
                    <span className="k" style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                      {checkIcon(c.status)} {c.label}
                    </span>
                    <span className="v" style={{ maxWidth: "58%", textAlign: "right", fontSize: 11.5, color: "var(--text-muted)" }}>
                      {c.detail}
                    </span>
                  </div>
                ))}
              </div>

              <div className="drawer-section">
                {failed.length > 0 && (
                  <div className="callout callout-amber" style={{ marginBottom: 12 }}>
                    {failed.length} check{failed.length === 1 ? "" : "s"} did not pass. Promotion is
                    still yours to make — but the server independently refuses a candidate with no
                    durable artifact when the deployment requires one.
                  </div>
                )}
                <button hidden={!can(PERM.deploy)} className="btn btn-primary btn-block" disabled={busy} onClick={promote}>
                  <Icon name="arrow-up" />
                  {busy ? "Promoting…" : `Promote v${version} to champion`}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function DeployedModelPanel({
  deployment: d, onClose, onChanged,
}: { deployment: Deployment; onClose: () => void; onChanged: () => void }) {
  const { can } = usePermissions();
  const navigate = useNavigate();
  const endpoint = useApi(() => api.getEndpoint(d.modelId).catch(() => null), [d.modelId]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  async function rollback() {
    setBusy(true);
    setError(null);
    try {
      await api.rollback(d.modelId);
      onChanged();
      onClose();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-md">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)" }}>{d.modelId}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>Runtime, versions, and rollback</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          <div className="drawer-section">
            <div className="drawer-section-title">Runtime</div>
            <div className="kv-row"><span className="k">Alias</span><span className="v"><span className="alias alias-champion">champion</span></span></div>
            <div className="kv-row"><span className="k">Live version</span><span className="v">v{d.currentVersion}</span></div>
            <div className="kv-row"><span className="k">Runtime</span><span className="v">{d.runtime}</span></div>
            <div className="kv-row"><span className="k">Replicas</span><span className="v">{d.replicas}</span></div>
            <div className="kv-row"><span className="k">Health</span><span className="v"><StatusPill status={d.healthStatus} /></span></div>
            {endpoint.data && (
              <div className="kv-row" style={{ alignItems: "flex-start" }}>
                <span className="k">Endpoint</span>
                <span className="v" style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, wordBreak: "break-all", maxWidth: "70%" }}>
                  {endpoint.data.url}
                </span>
              </div>
            )}
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">Previous version</div>
            {d.canRollback ? (
              <>
                <div className="kv-row"><span className="k">Rolls back to</span><span className="v">v{d.previousVersion}</span></div>
                <button hidden={!can(PERM.deploy)} className="btn btn-secondary btn-block" style={{ marginTop: 12, color: "var(--red)" }} disabled={busy} onClick={rollback}>
                  <Icon name="refresh" />
                  {busy ? "Rolling back…" : `Roll back to v${d.previousVersion}`}
                </button>
              </>
            ) : (
              <div className="callout callout-navy">
                This is the first version ever deployed for this model — there's nothing to roll back to.
              </div>
            )}
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">Production observability</div>
            <div className="hint" style={{ marginBottom: 10 }}>
              Drift, calibration, confusion matrix and the per-request prediction trace all live on
              Model Overview, which reads the same monitoring record this deployment writes.
            </div>
            <button className="btn btn-secondary btn-block" onClick={() => { onClose(); navigate(`/model-overview?model=${encodeURIComponent(d.modelId)}`); }}>
              <Icon name="activity" />
              Open Model Overview
            </button>
          </div>

          <div className="drawer-section">
            <button className="btn btn-primary btn-block" onClick={() => setTesting(true)}><Icon name="test" />Test this model</button>
          </div>
        </div>
      </div>

      {testing && (
        <TestDrawer
          label={`${d.modelId} · v${d.currentVersion}`}
          task={d.task}
          domain={d.domain}
          modelId={d.modelId}
          onRun={(input) => api.testPredict(d.modelId, input)}
          onClose={() => setTesting(false)}
        />
      )}
    </div>
  );
}
