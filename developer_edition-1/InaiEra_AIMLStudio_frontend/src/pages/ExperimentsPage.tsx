import { useState, useEffect, useRef } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import SearchSelect from "../components/SearchSelect";
import { StatusPill } from "../components/StatusPill";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";
import type { TrainingRun } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

export default function ExperimentsPage() {
  const { can, me } = usePermissions();
  const navigate = useNavigate();
  const experiments = useApi(api.getExperiments, []);
  const runs = useApi(api.getTrainingRuns, []);
  // `?run=` / `?experiment=` are how "View in Experiments" arrives from a
  // training run or a pipeline: open the right experiment if the run belongs to
  // one, and flash the run's row so the thing you followed the link for is
  // visibly the thing you land on.
  const [searchParams] = useSearchParams();
  const linkedRunId = searchParams.get("run");
  const linkedExpId = searchParams.get("experiment");
  const [filter, setFilter] = useState<"all" | "mine" | "hpo">(searchParams.get("tab") === "hpo" ? "hpo" : "all");
  const [selectedExpId, setSelectedExpId] = useState<string | null>(linkedExpId);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);
  const [query] = usePageSearch(filter === "hpo" ? "Search hyperparameter searches…" : "Search experiments and runs…");
  // A run matches on its model, base model, dataset or status.
  const runMatches = (r: TrainingRun) => matchesQuery(query, r.modelName, r.baseModelId, r.datasetName, r.status, r.id);
  // "Mine" = experiments this user created (owner is the creator's user name).
  const owner = me?.user ?? "";

  async function complete(id: string) {
    setError(null);
    setNotice(null);
    try {
      const result = await api.completeTrainingRun(id);
      runs.reload();
      if (result.registrationWarning) setNotice({ text: result.registrationWarning, ok: false });
      else setNotice({ text: `Registered as '${result.modelName}' — view it in Model Catalog or Model Overview.`, ok: true });
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "Failed to complete run");
    }
  }

  // Declared before the detail-view early return below: a hook after it ran on the
  // list view but not the detail view, and React crashed ("Rendered fewer hooks").
  // Runs arrive asynchronously, so the scroll has to wait for the row to exist
  // rather than firing on mount against an empty table.
  useEffect(() => {
    if (!linkedRunId || !runs.data?.length) return;
    const el = document.getElementById(`exprun-${linkedRunId}`) ?? document.getElementById(`run-${linkedRunId}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [linkedRunId, runs.data]);

  if (selectedExpId) {
    return <ExperimentDetail id={selectedExpId} onBack={() => setSelectedExpId(null)} />;
  }

  const visible = (experiments.data ?? []).filter((e) => {
    if (filter === "mine" && e.owner !== owner) return false;
    // An experiment matches on its own fields, or when one of its runs does.
    return matchesQuery(query, e.name, e.team, e.owner, e.objectiveMetric, e.description, ...(e.tags ?? []))
      || (runs.data ?? []).some((r) => r.experimentId === e.id && runMatches(r));
  });

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Experiments</h1>
            <p>
              Native tracking — Postgres + object storage, no MLflow. Every run, not just the ones that
              made it to a registered model.
            </p>
          </div>
          <button hidden={!can(PERM.train)} className="btn btn-primary" onClick={() => setCreating(true)}>
            <Icon name="plus" />
            New experiment
          </button>
        </div>

        <div className="tabs" role="tablist">
          {([["all", "All experiments"], ["mine", "Mine"], ["hpo", "Hyperparameter searches"]] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" aria-selected={filter === id}
              className={"tab" + (filter === id ? " active" : "")} onClick={() => setFilter(id)}>{label}</button>
          ))}
        </div>
      </div>

      {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
      {notice && <div className={notice.ok ? "callout callout-teal" : "callout callout-amber"} style={{ marginBottom: 14 }}>{notice.text}</div>}

      {experiments.loading && <div className="empty-hint">Loading…</div>}

      {filter !== "hpo" && visible.map((e) => {
        const expRuns = (runs.data ?? []).filter((r) => r.experimentId === e.id);
        return (
          <div className="card" style={{ marginBottom: 18 }} key={e.id}>
            <div
              className="card-pad"
              style={{ borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
              onClick={() => setSelectedExpId(e.id)}
            >
              <div>
                <div style={{ fontWeight: 700, fontSize: 14.5 }}>{e.name}</div>
                <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 2 }}>
                  {expRuns.length} run{expRuns.length === 1 ? "" : "s"} · objective {e.objectiveMetric} ({e.objectiveDirection}) · {e.team ?? "no team"} · {e.owner}
                </div>
              </div>
              <IconAction icon="eye" label={`Open experiment ${e.name}`} align="end" onClick={(ev) => { ev.stopPropagation(); setSelectedExpId(e.id); }} />
            </div>
            <div className="table-wrap" style={{ border: "none", borderRadius: 0 }}>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "22%" }}>Run</th>
                    <th style={{ width: "16%" }}>Base model</th>
                    <th style={{ width: "26%" }}>Hyperparameters</th>
                    <th style={{ width: "14%" }}>{e.objectiveMetric}</th>
                    <th style={{ width: "12%" }}>Status</th>
                    <th style={{ width: "10%" }} />
                  </tr>
                </thead>
                <tbody>
                  {expRuns.map((r) => (
                    <tr
                      key={r.id}
                      id={`exprun-${r.id}`}
                      className={r.id === linkedRunId ? "deep-link-flash" : undefined}
                      onClick={() => navigate(`/training-runs/${r.id}`)}
                    >
                      <td><div className="cell-name">{r.modelName}</div><div className="cell-sub">{r.id.slice(0, 8)}</div></td>
                      <td><span className="tag">{r.baseModelId}</span></td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                        {r.hyperparams ? Object.entries(r.hyperparams).map(([k, v]) => `${k}=${v}`).join(", ") : "trainer defaults"}
                      </td>
                      {/* An unrecorded metric shows a dash, never a zero — a run
                          that hasn't finished has no value, which is not the
                          same as a value of nothing. */}
                      <td style={{ fontWeight: 700, color: r.metrics?.[e.objectiveMetric] ? "var(--teal-700)" : "var(--text-faint)" }}>
                        {r.metrics?.[e.objectiveMetric] ?? "—"}
                      </td>
                      <td><StatusPill status={r.status} /></td>
                      <td>
                        {r.status === "queued" && (
                          <IconAction icon="check" label={`Mark ${r.modelName} run complete`} gated={PERM.train} tip="up" align="end" onClick={(e) => { e.stopPropagation(); void complete(r.id); }} />
                        )}
                      </td>
                    </tr>
                  ))}
                  {expRuns.length === 0 && (
                    <tr><td colSpan={6}><div className="empty-hint">
                      No runs in this experiment yet — pick it in the Train wizard's Experiment field,
                      or open it to launch a hyperparameter search.
                    </div></td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}

      {filter === "hpo" && (
        <HpoSearchList
          query={query}
          onOpen={setSelectedExpId}
          onPromoted={(text, ok) => setNotice({ text, ok })}
        />
      )}

      {/* On "All", the per-model cards below already show every run — an empty
          experiments box above them would read as "nothing here". */}
      {filter !== "hpo" && !experiments.loading && visible.length === 0 && (filter === "mine" || !runs.data?.length) && (
        <div className="card card-pad">
          <div className="empty-hint">
            {filter === "mine"
              ? `No experiments owned by '${owner}'${query.trim() ? ` match “${query.trim()}”` : ""}.`
              : "No experiments yet — create one to group runs and hyperparameter searches toward a shared objective."}
          </div>
        </div>
      )}

      {/* Every run, grouped per model as in the mock — not just the ones that
          were filed under an experiment. A model's card lists each run that
          produced (or tried to produce) a version of it, with the hyperparameter
          searches that tuned the same base model on the same data. */}
      {filter === "all" && (
        <ModelRunCards
          runs={(runs.data ?? []).filter(runMatches)}
          query={query}
          loading={runs.loading}
          linkedRunId={linkedRunId}
          onComplete={(id) => void complete(id)}
          onShowSearches={() => setFilter("hpo")}
        />
      )}

      {creating && (
        <CreateExperimentForm onClose={() => setCreating(false)} onCreated={() => { setCreating(false); experiments.reload(); }} />
      )}
    </div>
  );
}

const timeAgo = (iso?: string | null) => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60); if (m < 60) return `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(m / 60); if (h < 24) return `${h} hour${h === 1 ? "" : "s"} ago`;
  const d = Math.round(h / 24); return d === 1 ? "yesterday" : `${d} days ago`;
};

/** The run's headline metric — the one its trainer reports for its task. Loss
 *  is lower-is-better, everything else higher. */
function headlineMetric(runs: TrainingRun[]): { key: string; label: string; lowerIsBetter: boolean } | null {
  const keys = new Set(runs.flatMap((r) => Object.keys(r.metrics ?? {})));
  const pick = ["accuracy", "r2", "evalLoss", "silhouetteScore", "trustworthiness", "detectionRate", "mae", "rmse", "reconstructionMse"].find((k) => keys.has(k));
  if (!pick) return null;
  const lowerIsBetter = /loss|mae|rmse|mse/i.test(pick);
  return { key: pick, label: pick === "evalLoss" ? "eval_loss" : `val_${pick.replace(/([A-Z])/g, "_$1").toLowerCase()}`, lowerIsBetter };
}

// Small values keep their significant digits — rounding a 0.00004 loss to
// four decimals would print a misleading "0".
const fmtVal = (v: unknown) =>
  typeof v !== "number" ? String(v)
  : Number.isInteger(v) ? String(v)
  : Math.abs(v) < 0.001 ? v.toPrecision(2)
  : String(Number(v.toFixed(4)));

/** The params a run actually trained with — overrides for classical/deep
 *  runs, the adapter config for a fine-tune. "trainer defaults" when the run
 *  overrode nothing, rather than an invented list. */
function runParams(r: TrainingRun): string {
  if (r.loraParams) return `lr=${r.loraParams.learningRate}, epochs=${r.loraParams.epochs}, base=${r.baseModelId}`;
  if (r.fullParams) return `lr=${r.fullParams.learningRate}, epochs=${r.fullParams.epochs}, warmup=${r.fullParams.warmupSteps}`;
  if (r.hyperparams && Object.keys(r.hyperparams).length) return Object.entries(r.hyperparams).map(([k, v]) => `${k}=${v}`).join(", ");
  return "trainer defaults";
}

/** Status pill in the mock's vocabulary: a finished run reads "completed". */
const RunStatus = ({ status }: { status: string }) => (
  <StatusPill status={status === "completed" ? "succeeded" : status} label={status === "succeeded" || status === "completed" ? "completed" : undefined} />
);

function ModelRunCards({ runs, query, loading, linkedRunId, onComplete, onShowSearches }: {
  query: string;
  runs: TrainingRun[]; loading: boolean; linkedRunId: string | null;
  onComplete: (id: string) => void; onShowSearches: () => void;
}) {
  const navigate = useNavigate();
  const baseModels = useApi(api.getBaseModels, []);
  const searches = useApi(api.getHpoSearches, []);
  const baseOf = (id: string) => baseModels.data?.find((b) => b.id === id);

  // One card per model, most recently active first.
  const groups = new Map<string, TrainingRun[]>();
  for (const r of runs) groups.set(r.modelName, [...(groups.get(r.modelName) ?? []), r]);
  const ordered = [...groups.entries()]
    .map(([name, rs]) => ({ name, runs: [...rs].sort((a, b) => b.createdAt.localeCompare(a.createdAt)) }))
    .sort((a, b) => b.runs[0].createdAt.localeCompare(a.runs[0].createdAt));

  if (loading) return <div className="empty-hint">Loading runs…</div>;

  return (
    <>
      {(searches.data ?? []).filter((s) => matchesQuery(query, s.baseModelId, s.datasetName, s.objectiveMetric, s.status, s.id)).map((s) => (
        <SearchRunsCard key={s.id} searchId={s.id} algorithm={baseOf(s.baseModelId)?.algorithm_key ?? s.baseModelId}
          domain={baseOf(s.baseModelId)?.domain ?? "ml"} onShowSearches={onShowSearches} />
      ))}

      {ordered.map(({ name, runs: rs }) => {
        const first = rs[0];
        const base = baseOf(first.baseModelId);
        const isLlm = base?.domain === "llm" || !!first.fineTuneMethod;
        const metric = headlineMetric(rs);
        const measured = metric ? rs.filter((r) => typeof r.metrics?.[metric.key] === "number") : [];
        const best = metric && measured.length
          ? measured.reduce((a, b) => ((metric.lowerIsBetter ? b.metrics![metric.key] < a.metrics![metric.key] : b.metrics![metric.key] > a.metrics![metric.key]) ? b : a))
          : null;
        const oldest = rs[rs.length - 1].createdAt;
        const algoLabel = (r: TrainingRun) =>
          r.fineTuneMethod === "full" ? "full fine-tune"
          : r.fineTuneMethod ? `${r.fineTuneMethod === "qlora" ? "QLoRA" : "LoRA"} r=${r.loraParams?.rank ?? "?"}`
          : baseOf(r.baseModelId)?.algorithm_key ?? r.baseModelId;
        // A run records no link to a search, so a model card never claims one —
        // searches get their own cards (SearchRunsCard) instead.
        const kind = isLlm ? "finetune" : "training";
        const pipePrefix = isLlm ? "finetune" : "train";
        return (
          <div className="card" style={{ marginBottom: 18 }} key={name}>
            <div className="card-pad" style={{ borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: 14.5 }}>{name} {kind}</div>
                <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 2 }}>
                  {rs.length} run{rs.length === 1 ? "" : "s"} ·{" "}
                  {isLlm ? (first.fineTuneMethod === "full" ? "full fine-tune" : first.fineTuneMethod === "qlora" ? "QLoRA" : "LoRA")
                    : (base?.algorithm_key ?? first.baseModelId).replace(/_/g, " ")}
                  {" "}· started {timeAgo(oldest)}
                </div>
              </div>
              <span className={"badge " + (isLlm ? "badge-llm" : "badge-ml")}><span className="dot" /> domain · {isLlm ? "llm" : "ml"}</span>
            </div>
            <div className="table-wrap" style={{ border: "none", borderRadius: 0 }}>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "13%" }}>Run</th>
                    <th style={{ width: "14%" }}>{isLlm ? "Technique" : "Algorithm"}</th>
                    <th style={{ width: "28%" }}>Params</th>
                    <th style={{ width: "12%" }}>{metric?.label ?? "metric"}</th>
                    <th style={{ width: "11%" }}>Status</th>
                    <th style={{ width: "13%" }}>Pipeline</th>
                    <th style={{ width: "9%" }} />
                  </tr>
                </thead>
                <tbody>
                  {rs.map((r) => {
                    const v = metric ? r.metrics?.[metric.key] : undefined;
                    const active = r.status === "queued" || r.status === "running";
                    const isBest = best?.id === r.id && measured.length > 0;
                    return (
                      <tr key={r.id} id={`run-${r.id}`} className={r.id === linkedRunId ? "deep-link-flash" : undefined}
                        onClick={() => navigate(r.status === "failed" ? `/pipelines/${r.id}` : `/training-runs/${r.id}`)}>
                        <td className="cell-name" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>run_{r.id.slice(0, 6)}</td>
                        <td><span className="tag">{algoLabel(r)}</span></td>
                        <td style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-muted)" }}>{runParams(r)}</td>
                        <td style={isBest ? { fontWeight: 700, color: "var(--teal-700)" } : typeof v === "number" ? undefined : { color: "var(--text-faint)" }}>
                          {typeof v === "number" ? fmtVal(v) : active ? "— (training)" : "—"}
                          {isBest && measured.length > 1 && <span style={{ fontWeight: 400, color: "var(--text-faint)", fontSize: 11, marginLeft: 4 }}>best</span>}
                        </td>
                        <td><RunStatus status={r.status} /></td>
                        <td>
                          <Link to={`/pipelines/${r.id}`} onClick={(e) => e.stopPropagation()}
                            style={{ fontSize: 11.5, color: "var(--teal-700)", fontWeight: 600 }} title={r.error ?? undefined}>
                            {pipePrefix}-…-{r.id.slice(0, 3)}
                          </Link>
                        </td>
                        <td>
                          {r.status === "queued" ? (
                            <IconAction icon="check" label={`Mark run_${r.id.slice(0, 6)} complete`} gated={PERM.train} tip="up" align="end" onClick={(e) => { e.stopPropagation(); onComplete(r.id); }} />
                          ) : (
                            <IconAction icon={r.status === "failed" ? "file" : "eye"} tip="up" align="end"
                              label={r.status === "failed" ? `View pipeline logs of run_${r.id.slice(0, 6)}` : `View training run run_${r.id.slice(0, 6)}`}
                              onClick={(e) => { e.stopPropagation(); navigate(r.status === "failed" ? `/pipelines/${r.id}` : `/training-runs/${r.id}`); }} />
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}

      {!ordered.length && !(searches.data ?? []).length && (
        <div className="card card-pad"><div className="empty-hint">{query.trim() ? <>No training runs match “{query.trim()}”.</> : "No training runs yet — launch one from the Train wizard."}</div></div>
      )}
    </>
  );
}

/** A hyperparameter search in the same card shape as a model's runs: the best
 *  trial and the next few, then a "+ N more" row into the full leaderboard. */
function SearchRunsCard({ searchId, algorithm, domain, onShowSearches }: {
  searchId: string; algorithm: string; domain: string; onShowSearches: () => void;
}) {
  const detail = useApi(() => api.getHpoSearch(searchId), [searchId]);
  const s = detail.data;
  if (!s) return null;
  const sorted = [...s.trials].sort((a, b) => Number(b.isBest) - Number(a.isBest) || a.trialNumber - b.trialNumber);
  const shown = sorted.slice(0, 3);
  const label = s.objectiveMetric === "evalLoss" ? "eval_loss" : `val_${s.objectiveMetric}`;
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-pad" style={{ borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14.5 }}>{s.baseModelId} · {s.datasetName} search</div>
          <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 2 }}>
            {s.numTrials} run{s.numTrials === 1 ? "" : "s"} · 1 hyperparameter search · started {timeAgo(s.createdAt)}
          </div>
        </div>
        <span className={"badge " + (domain === "llm" ? "badge-llm" : "badge-ml")}><span className="dot" /> domain · {domain}</span>
      </div>
      <div className="table-wrap" style={{ border: "none", borderRadius: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: "13%" }}>Run</th>
              <th style={{ width: "14%" }}>Algorithm</th>
              <th style={{ width: "28%" }}>Params</th>
              <th style={{ width: "12%" }}>{label}</th>
              <th style={{ width: "11%" }}>Status</th>
              <th style={{ width: "13%" }}>Pipeline</th>
              <th style={{ width: "9%" }} />
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={t.id} onClick={onShowSearches}>
                <td className="cell-name" style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>trial_{t.trialNumber}</td>
                <td><span className="tag">{algorithm}</span></td>
                <td style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                  {Object.entries(t.hyperparams).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("/") : v}`).join(", ")}
                </td>
                <td style={t.isBest ? { fontWeight: 700, color: "var(--teal-700)" } : t.metricValue == null ? { color: "var(--text-faint)" } : undefined}>
                  {t.metricValue != null ? fmtVal(t.metricValue) : t.status === "failed" ? "—" : "— (training)"}
                  {t.isBest && <span style={{ fontWeight: 400, color: "var(--text-faint)", fontSize: 11, marginLeft: 4 }}>{s.status === "completed" ? "best" : "best so far"}</span>}
                </td>
                <td><RunStatus status={t.status} /></td>
                <td style={{ fontSize: 11.5, color: "var(--text-faint)" }} title={`search ${s.id}`}>tune-…-{s.id.slice(0, 3)}</td>
                <td><IconAction icon={t.status === "failed" ? "file" : "eye"} label={t.status === "failed" ? `View logs of trial_${t.trialNumber}` : `View trial_${t.trialNumber} in its search`} tip="up" align="end" onClick={(e) => { e.stopPropagation(); onShowSearches(); }} /></td>
              </tr>
            ))}
            {sorted.length > shown.length && (
              <tr onClick={onShowSearches}>
                <td className="cell-name" style={{ color: "var(--text-faint)" }}>+ {sorted.length - shown.length} more trial{sorted.length - shown.length === 1 ? "" : "s"}</td>
                <td colSpan={4} style={{ color: "var(--text-faint)", fontSize: 11.5 }}>See the full leaderboard and promote the best candidate under Hyperparameter searches</td>
                <td style={{ fontSize: 11.5, color: "var(--text-faint)" }}>tune-…-{s.id.slice(0, 3)}</td>
                <td><IconAction icon="eye" label="View all trials in Hyperparameter searches" tip="up" align="end" onClick={(e) => { e.stopPropagation(); onShowSearches(); }} /></td>
              </tr>
            )}
            {!sorted.length && (
              <tr><td colSpan={7}><div className="empty-hint">No trials recorded yet — the worker claims them from the queue.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Every HPO search across every experiment, for the "Hyperparameter searches"
 *  filter — the full trial leaderboard per search, with promotion. */
function HpoSearchList({ query, onOpen, onPromoted }: { query: string; onOpen: (experimentId: string) => void; onPromoted: (msg: string, ok: boolean) => void }) {
  const searches = useApi(api.getHpoSearches, []);
  const shown = (searches.data ?? []).filter((s) => matchesQuery(query, s.baseModelId, s.datasetName, s.objectiveMetric, s.status, s.id));
  return (
    <>
      {!!searches.data?.length && !shown.length && (
        <div className="card card-pad"><div className="empty-hint">No hyperparameter searches match “{query.trim()}”.</div></div>
      )}
      {shown.map((s) => (
        <HpoSearchDetailCard key={s.id} id={s.id} experimentId={s.experimentId ?? null} onOpen={onOpen} onPromoted={onPromoted} />
      ))}
      {!searches.loading && !searches.data?.length && (
        <div className="card card-pad"><div className="empty-hint">No hyperparameter searches have been run yet.</div></div>
      )}
    </>
  );
}

function HpoSearchDetailCard({ id, experimentId, onOpen, onPromoted }: {
  id: string; experimentId: string | null; onOpen: (experimentId: string) => void; onPromoted: (msg: string, ok: boolean) => void;
}) {
  const detail = useApi(() => api.getHpoSearch(id), [id]);
  if (!detail.data) return null;
  return (
    <>
      {experimentId && (
        <div style={{ textAlign: "right", marginBottom: 6 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => onOpen(experimentId)}>Open experiment</button>
        </div>
      )}
      <HpoSearchCard search={detail.data} onPromoted={(msg, ok) => { onPromoted(msg, ok); detail.reload(); }} />
    </>
  );
}

function CreateExperimentForm({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [objectiveMetric, setObjectiveMetric] = useState("accuracy");
  const [objectiveDirection, setObjectiveDirection] = useState<"maximize" | "minimize">("maximize");
  const [team, setTeam] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function create() {
    if (!name.trim()) return setError("Give the experiment a name.");
    setSubmitting(true);
    setError(null);
    try {
      await api.createExperiment({ name, objectiveMetric, objectiveDirection, team: team || undefined });
      onCreated();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : "Failed to create experiment");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>New experiment</div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
          <div className="drawer-section">
            <div className="field">
              <label>Name</label>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. fraud-dl-tuning" />
            </div>
            <div className="field">
              <label id="exp-objective-label">Objective metric</label>
              <SearchSelect labelledBy="exp-objective-label" noun="metric" value={objectiveMetric}
                options={["accuracy", "f1", "mse", "r2"].map((m) => ({ value: m, label: m }))}
                onChange={(v) => setObjectiveMetric(v)} />
              <div className="hint">What every run and trial in this experiment is compared on.</div>
            </div>
            <div className="field">
              <label id="exp-direction-label">Direction</label>
              <SearchSelect labelledBy="exp-direction-label" noun="direction" value={objectiveDirection}
                options={[{ value: "maximize", label: "maximize" }, { value: "minimize", label: "minimize" }]}
                onChange={(v) => setObjectiveDirection(v as "maximize" | "minimize")} />
            </div>
            <div className="field">
              <label>Team (optional)</label>
              <input className="input" value={team} onChange={(e) => setTeam(e.target.value)} />
            </div>
            <button className="btn btn-primary btn-block" disabled={submitting} onClick={create}>
              {submitting ? "Creating…" : "Create experiment"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function ExperimentDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { can } = usePermissions();
  const navigate = useNavigate();
  const detail = useApi(() => api.getExperiment(id), [id]);
  const [showHpoForm, setShowHpoForm] = useState(false);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);

  // Real polling for real async HPO searches — POST /api/hpo-searches
  // now returns 202 immediately and processes trials in the background
  // (see hpoWorker.ts), so the one-time reload() this component used to
  // rely on after creating a search would show it stuck at "queued"
  // forever unless the user manually refreshed the page. This polls
  // silently (a plain api.getExperiment call, not detail.reload(),
  // which clears data and flips back to a loading spinner on every
  // call) whenever any real HPO search for this experiment is still in
  // a non-terminal state, and stops polling once none are — a real,
  // finite background refresh, not an indefinite timer left running
  // after there's nothing left to wait for.
  const [livePolled, setLivePolled] = useState<typeof detail.data>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const exp = livePolled ?? detail.data;

  useEffect(() => {
    // A real reload() (creating a search, promoting a trial, switching
    // experiments) produced fresh data — drop any stale polled snapshot
    // so we render the real freshly-loaded data, not last poll's.
    setLivePolled(null);
  }, [detail.data]);

  useEffect(() => {
    const hasActiveSearch = exp?.hpoSearches?.some((s: any) => s.status === "queued" || s.status === "running");
    if (!hasActiveSearch) {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
      return;
    }
    if (pollingRef.current) return; // already polling
    pollingRef.current = setInterval(() => {
      api.getExperiment(id).then(setLivePolled).catch(() => {});
    }, 3000);
    return () => {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exp?.hpoSearches?.map((s: any) => s.status).join(",")]);

  if (detail.loading) return <div className="empty-hint">Loading…</div>;
  if (detail.error || !exp) return <div className="callout callout-amber">{detail.error ?? "Experiment not found"}</div>;

  return (
    <div>
      <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={onBack}>
        <Icon name="arrow-left" />
        Back to Experiments
      </button>

      <div className="page-head">
        <div>
          <h1>{exp.name}</h1>
          <p>
            Objective <strong>{exp.objectiveMetric}</strong> ({exp.objectiveDirection}) · {exp.team ?? "no team"} · owned by {exp.owner}
          </p>
        </div>
        <button hidden={!can(PERM.train)} className="btn btn-primary" onClick={() => setShowHpoForm(true)}>
          <Icon name="plus" />
          New hyperparameter search
        </button>
      </div>

      {notice && (
        <div className={notice.ok ? "callout callout-teal" : "callout callout-amber"} style={{ marginBottom: 14 }}>
          {notice.text}
        </div>
      )}

      {/* Only shown once something has actually produced a value for the
          objective — there is no "best so far" before any run has finished. */}
      {exp.bestOverall && (
        <div className="card card-pad" style={{ marginBottom: 16 }}>
          <div className="drawer-section-title">Best so far</div>
          <div className="kv-row"><span className="k">{exp.bestOverall.label}</span><span className="v" style={{ fontFamily: "var(--font-mono)", color: "var(--teal-700)", fontWeight: 700 }}>{exp.objectiveMetric} = {exp.bestOverall.value}</span></div>
          <div className="hint" style={{ marginTop: 6 }}>
            {exp.bestOverall.source === "hpo" ? "From a hyperparameter search trial." : "From a direct training run."}
          </div>
        </div>
      )}

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Training runs in this experiment</div>
        {exp.runs.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: "30%" }}>Model</th>
                  <th style={{ width: "26%" }}>Base model</th>
                  <th style={{ width: "22%" }}>Status</th>
                  <th style={{ width: "22%" }}>{exp.objectiveMetric}</th>
                </tr>
              </thead>
              <tbody>
                {exp.runs.map((r) => (
                  <tr key={r.id} onClick={() => navigate(`/training-runs/${r.id}`)}>
                    <td><div className="cell-name">{r.modelName}</div></td>
                    <td><span className="tag">{r.baseModelId}</span></td>
                    <td><StatusPill status={r.status} /></td>
                    <td style={{ fontFamily: "var(--font-mono)" }}>{r.metrics?.[exp.objectiveMetric] ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-hint">
            No direct training runs in this experiment yet — pick it in the Train wizard's Experiment
            field, or promote a winning search trial below.
          </div>
        )}
      </div>

      <div className="drawer-section-title">Hyperparameter searches</div>
      {exp.hpoSearches.map((s) => (
        <HpoSearchCard key={s.id} search={s} onPromoted={(msg, ok) => { setNotice({ text: msg, ok }); detail.reload(); }} />
      ))}
      {exp.hpoSearches.length === 0 && (
        <div className="card card-pad"><div className="empty-hint">No hyperparameter searches in this experiment yet.</div></div>
      )}

      {showHpoForm && (
        <CreateHpoSearchForm
          experimentId={exp.id}
          defaultObjectiveMetric={exp.objectiveMetric}
          defaultObjectiveDirection={exp.objectiveDirection}
          onClose={() => setShowHpoForm(false)}
          onDone={() => { setShowHpoForm(false); detail.reload(); }}
        />
      )}
    </div>
  );
}

function CreateHpoSearchForm({
  experimentId, defaultObjectiveMetric, defaultObjectiveDirection, onClose, onDone,
}: { experimentId: string; defaultObjectiveMetric: string; defaultObjectiveDirection: "maximize" | "minimize"; onClose: () => void; onDone: () => void }) {
  const baseModels = useApi(api.getBaseModels, []);
  const datasets = useApi(api.getDatasets, []);
  const [baseModelId, setBaseModelId] = useState("");
  const [datasetName, setDatasetName] = useState("");
  const [numTrials, setNumTrials] = useState(5);
  const [hiddenLayerOptions, setHiddenLayerOptions] = useState("32 | 64,32 | 128,64");
  const [learningRateOptions, setLearningRateOptions] = useState("0.01, 0.001, 0.0001");
  const [objectiveMetric, setObjectiveMetric] = useState(defaultObjectiveMetric);
  const [targetColumn, setTargetColumn] = useState("");
  const [timestampColumn, setTimestampColumn] = useState("");
  const [valueColumn, setValueColumn] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const selectedBaseModel = baseModels.data?.find((b) => b.id === baseModelId);
  const isDeepLearning = selectedBaseModel && /pytorch|torch|tensorflow|keras|jax/i.test(selectedBaseModel.framework);
  const deepForecastIds = new Set(["lstm-forecaster", "gru-forecaster", "nbeats-forecaster", "temporal-fusion-transformer"]);
  const reconstructionIds = new Set(["autoencoder-anomaly", "lstm-autoencoder", "denoising-autoencoder", "tabular-embedding-nn"]);
  const isDeepForecast = !!selectedBaseModel && deepForecastIds.has(selectedBaseModel.id);
  const isReconstruction = !!selectedBaseModel && reconstructionIds.has(selectedBaseModel.id);
  const needsTarget = !!selectedBaseModel && ["deep-mlp-classifier", "deep-mlp-regressor", "gru-classifier", "cnn-image-classifier"].includes(selectedBaseModel.id);
  const objectiveOptions = isDeepForecast
    ? ["mae", "rmse", "mape"]
    : isReconstruction
    ? ["reconstructionMse", "anomalyRate"]
    : selectedBaseModel?.id === "deep-mlp-regressor"
    ? ["mse", "r2"]
    : isDeepLearning
    ? ["accuracy", "f1"]
    : ["accuracy", "f1", "mse", "r2"];

  async function run() {
    if (!baseModelId || !datasetName) return setError("Pick a base model and dataset first.");
    setSubmitting(true);
    setError(null);
    try {
      const hiddenLayers = hiddenLayerOptions.split("|").map((s) => s.trim().split(",").map((n) => Number(n.trim())).filter((n) => !Number.isNaN(n))).filter((a) => a.length);
      const learningRate = learningRateOptions.split(",").map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
      if (needsTarget && !targetColumn.trim()) throw new Error("Target column is required for this supervised deep-learning model.");
      if (isDeepForecast && (!timestampColumn.trim() || !valueColumn.trim())) throw new Error("Timestamp and value columns are required for deep forecasting.");
      await api.createHpoSearch({
        experimentId, baseModelId, datasetName, numTrials,
        searchSpace: { hiddenLayers, learningRate },
        targetColumn: needsTarget ? targetColumn.trim() : undefined,
        timeSeriesConfig: isDeepForecast ? { timestampColumn: timestampColumn.trim(), valueColumn: valueColumn.trim(), missingPeriodPolicy: "ffill" } : undefined,
        objectiveMetric: (objectiveOptions.includes(objectiveMetric) ? objectiveMetric : objectiveOptions[0]) as import("../types/domain").HpoSearchRequest["objectiveMetric"],
        objectiveDirection: defaultObjectiveDirection,
      });
      onDone();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : e instanceof Error ? e.message : "Failed to run HPO search");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>New hyperparameter search</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>Ray Tune + Optuna — one real trial per sampled combination</div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
          <div className="drawer-section">
            <div className="field">
              <label id="hpo-base-model-label">Base model</label>
              <SearchSelect labelledBy="hpo-base-model-label" noun="model" placeholder="Select…" value={baseModelId}
                options={(baseModels.data ?? []).map((b) => ({ value: b.id, label: b.id }))}
                onChange={(v) => setBaseModelId(v)} />
            </div>
            <div className="field">
              <label id="hpo-dataset-label">Dataset</label>
              <SearchSelect labelledBy="hpo-dataset-label" noun="dataset" placeholder="Select…" value={datasetName}
                options={(datasets.data ?? []).map((d) => ({ value: d.name, label: d.name }))}
                onChange={(v) => setDatasetName(v)} />
            </div>
            {baseModelId && (
              <div className={isDeepLearning ? "callout callout-teal" : "callout callout-navy"} style={{ marginBottom: 14 }}>
                {isDeepLearning
                  ? "Deep-learning base model — trials run the native PyTorch architecture executor against the governed dataset snapshot. An unsupported architecture or data contract fails closed rather than substituting a stand-in."
                  : "Classical / time-series model — trials use that model's own real trainer where one is supported."}
              </div>
            )}
            {needsTarget && (
              <div className="field">
                <label>Target column</label>
                <input className="input" value={targetColumn} onChange={(e) => setTargetColumn(e.target.value)} placeholder="e.g. is_fraudulent" />
              </div>
            )}
            {isDeepForecast && (
              <div className="field-row">
                <div className="field">
                  <label>Timestamp column</label>
                  <input className="input" value={timestampColumn} onChange={(e) => setTimestampColumn(e.target.value)} placeholder="e.g. timestamp" />
                </div>
                <div className="field">
                  <label>Value column</label>
                  <input className="input" value={valueColumn} onChange={(e) => setValueColumn(e.target.value)} placeholder="e.g. value" />
                </div>
              </div>
            )}
            <div className="field">
              <label id="hpo-objective-label">Objective metric</label>
              <SearchSelect
                labelledBy="hpo-objective-label"
                noun="metric"
                value={objectiveOptions.includes(objectiveMetric) ? objectiveMetric : objectiveOptions[0]}
                options={objectiveOptions.map((m) => ({ value: m, label: m }))}
                onChange={(v) => setObjectiveMetric(v)}
              />
              <div className="hint">Validated against the selected architecture — direction is {defaultObjectiveDirection}.</div>
            </div>
            <div className="field">
              <label>Hidden layer options</label>
              <input className="input" value={hiddenLayerOptions} onChange={(e) => setHiddenLayerOptions(e.target.value)} />
              <div className="hint">One architecture per group, separated by <code>|</code>; comma-separated layer sizes within a group.</div>
            </div>
            <div className="field">
              <label>Learning rate options</label>
              <input className="input" value={learningRateOptions} onChange={(e) => setLearningRateOptions(e.target.value)} />
              <div className="hint">Comma-separated values to sample from.</div>
            </div>
            <div className="field">
              <label>Number of trials</label>
              <input className="input" type="number" min={1} max={20} value={numTrials} onChange={(e) => setNumTrials(Number(e.target.value))} />
            </div>
            <button className="btn btn-primary btn-block" disabled={submitting} onClick={run}>
              <Icon name="play" />
              {submitting ? "Queueing trials…" : `Run ${numTrials} trials`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function HpoSearchCard({ search, onPromoted }: { search: import("../types/domain").HpoSearchDetail; onPromoted: (msg: string, ok: boolean) => void }) {
  const { can } = usePermissions();
  const [modelName, setModelName] = useState("");
  const [promoting, setPromoting] = useState(false);

  async function promote() {
    if (!modelName.trim()) return onPromoted("Give the promoted model a name first.", false);
    setPromoting(true);
    try {
      const result = await api.promoteBestTrial(search.id, modelName);
      onPromoted(
        result.registrationWarning ?? `Promoted trial #${result.promotedFromTrial} as '${result.modelName}' — a real challenger version, ready for Deploy & Promote.`,
        !result.registrationWarning
      );
    } catch (e) {
      onPromoted(e instanceof ApiRequestError ? e.message : "Failed to promote best trial", false);
    } finally {
      setPromoting(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-pad" style={{ borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14 }}>{search.baseModelId} on {search.datasetName}</div>
          <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 3 }}>
            {search.numTrials} trials · objective {search.objectiveMetric} ({search.objectiveDirection})
          </div>
        </div>
        <RunStatus status={search.status} />
      </div>
      <div className="table-wrap" style={{ border: "none", borderRadius: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: "12%" }}>Trial</th>
              <th style={{ width: "42%" }}>Hyperparameters</th>
              <th style={{ width: "16%" }}>{search.objectiveMetric}</th>
              <th style={{ width: "18%" }}>Source</th>
              <th style={{ width: "12%" }} />
            </tr>
          </thead>
          <tbody>
            {search.trials.map((t) => (
              <tr key={t.id} style={t.isBest ? { background: "var(--teal-100)" } : undefined}>
                <td className="cell-name">#{t.trialNumber}{t.isBest ? " ★" : ""}</td>
                <td style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-muted)" }}>
                  {Object.entries(t.hyperparams).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")}
                </td>
                <td style={{ fontFamily: "var(--font-mono)" }}>{t.status === "failed" ? "failed" : t.metricValue ?? "—"}</td>
                {/* "measured" means a real trainer produced this number.
                    Anything else is labelled so it can never be mistaken for one. */}
                <td>
                  <span className={`badge ${t.dataSource === "measured" ? "badge-ml" : "badge-amber"}`}>
                    {t.dataSource}
                  </span>
                </td>
                <td>{t.error && <span title={t.error} style={{ color: "var(--amber-700)" }}>⚠</span>}</td>
              </tr>
            ))}
            {search.trials.length === 0 && (
              <tr><td colSpan={5}><div className="empty-hint">No trials recorded yet — the worker claims them from the queue.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
      {search.bestTrialId && (
        <div className="card-pad" style={{ borderTop: "1px solid var(--border)", display: "flex", gap: 8, alignItems: "center" }}>
          <input
            className="input"
            placeholder="Name for the promoted model"
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            style={{ flex: 1 }}
          />
          <button hidden={!can(PERM.train)} className="btn btn-primary" style={{ flex: "none" }} disabled={promoting} onClick={promote}>
            {promoting ? "Promoting…" : "Promote best trial"}
          </button>
        </div>
      )}
    </div>
  );
}
