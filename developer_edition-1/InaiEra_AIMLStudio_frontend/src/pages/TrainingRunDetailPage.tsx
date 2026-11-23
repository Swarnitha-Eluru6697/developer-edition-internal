import { confirmDialog } from "../components/ConfirmDrawer";
import { ArtifactLocation } from "../components/ArtifactLocation";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { Icon } from "../components/Icons";
import { StatusPill } from "../components/StatusPill";
import { DagStages } from "../components/DagStages";
import type { TrainingRunDetail, GateRowResult, ValidationGate, PipelineLog } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

/**
 * One training run, end to end: the DAG it executed, the run record, what it
 * measured, whether it clears the platform's acceptance policy, and everything
 * needed to reproduce it.
 *
 * Every catalog entry, pipeline row and experiment row that has a run behind it
 * routes here, so this is the single page that answers "what actually happened
 * to this model" — rather than four partial views that can disagree.
 */
export default function TrainingRunDetailPage() {
  const { can } = usePermissions();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<TrainingRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [retraining, setRetraining] = useState(false);
  const [logs, setLogs] = useState<PipelineLog[]>([]);

  const load = useCallback(async () => {
    // The run's own live log carries step progress (LLM trainers log every step
    // with loss and ETA) — the live panel reads progress from it, not from a guess.
    api.getPipeline(id).then((p) => setLogs(p.logs)).catch(() => {});
    try {
      setDetail(await api.getTrainingRunDetail(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  // A run in flight is genuinely changing in Postgres — the worker heartbeats
  // and flips status. Poll while that is true so the DAG and elapsed clock
  // advance, and stop the moment it reaches a terminal state.
  useEffect(() => {
    if (!detail?.live.inFlight) return;
    const timer = setInterval(() => { void load(); }, 2000);
    return () => clearInterval(timer);
  }, [detail?.live.inFlight, load]);

  async function cancel() {
    if (!(await confirmDialog({
      title: "Cancel this training run?",
      message: "The run stops right away — a run that is training has its trainer process stopped, and nothing is registered.",
      confirmLabel: "Cancel run", cancelLabel: "Keep running", danger: true,
    }))) return;
    setCancelling(true);
    setError(null);
    try {
      const r = await api.cancelTrainingRun(id);
      setNotice(r.status === "failed"
        ? "Cancelled — this run hadn't started, so it's stopped and won't be picked up."
        : "Cancelling — the trainer is being stopped; this page shows \"cancelled\" within a few seconds.");
      await load();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  }

  async function retrain() {
    if (!(await confirmDialog({
      title: "Re-train this model?",
      message: "A new run starts with the same base model, dataset snapshot and settings. This failed run stays as it is, with its log.",
      confirmLabel: "Re-train",
    }))) return;
    setRetraining(true);
    setError(null);
    try {
      const next = await api.retrainTrainingRun(id);
      navigate(`/training-runs/${next.id}`);
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setRetraining(false);
    }
  }

  if (error && !detail) {
    return (
      <div>
        <BackButton onClick={() => navigate("/pipelines")} />
        <div className="callout callout-amber">Couldn't load this training run: {error}</div>
      </div>
    );
  }
  if (!detail) return <div className="empty-hint">Loading…</div>;

  const { run, live, reproducibility: rep } = detail;
  // A cancelled run is stored as failed with a "CANCELLED:" reason.
  const shownStatus = run.status === "failed" && run.error?.startsWith("CANCELLED") ? "cancelled" : run.status;
  const metrics = Object.entries(run.metrics ?? {}).filter(([k]) => k !== "realDataset");
  const progress = readProgress(logs, run.status === "succeeded");

  return (
    <div>
      <div className="page-fixed">
        <BackButton onClick={() => navigate("/pipelines")} />

        <div className="page-head">
          <div>
            <h1 style={{ fontFamily: "var(--font-mono)", fontSize: 19 }}>{run.id}</h1>
            <p>
              {run.modelName} — {detail.baseModel?.title ?? run.baseModelId}, trained on {run.datasetName}
              {detail.dataset?.snapshotVersion ? ` v${detail.dataset.snapshotVersion}` : ""}
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flex: "none" }}>
            <button className="btn btn-secondary btn-sm" onClick={() => navigate(`/pipelines/${detail.pipelineId}`)}>
              <Icon name="file" />
              View logs
            </button>
            {run.status === "failed" && (
              <button hidden={!can(PERM.train)} className="btn btn-primary btn-sm" onClick={retrain} disabled={retraining}
                title="Start a new run with this run's base model, dataset snapshot and settings">
                <Icon name="refresh" />
                {retraining ? "Starting…" : "Re-train"}
              </button>
            )}
            {/* Deep-links to the experiment that owns this run when there is one;
                otherwise to the "All training runs" table, which lists every run
                including the ones that belong to no experiment. Never a dead
                button, and never a link to an experiment this run isn't in. */}
            <button
              className="btn btn-secondary btn-sm"
              onClick={() =>
                navigate(detail.experiment ? `/experiments?experiment=${detail.experiment.id}&run=${run.id}` : `/experiments?run=${run.id}`)
              }
            >
              <Icon name="trend" />
              View in Experiments
            </button>
            {detail.registeredVersion && (
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => navigate(`/model-overview?model=${encodeURIComponent(detail.registeredVersion!.modelId)}`)}
              >
                <Icon name="box" />
                Open model
              </button>
            )}
          </div>
        </div>
      </div>

      {notice && <div className="callout callout-teal" style={{ marginBottom: 14 }}>{notice}</div>}
      {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Pipeline stages</div>
        <DagStages stages={detail.stages} />
      </div>

      <div className="field-row" style={{ alignItems: "stretch", marginBottom: 16 }}>
        <div className="card card-pad">
          <div className="drawer-section-title">Training run</div>
          <Kv k="run_id" v={run.id} mono />
          <Kv k="model_template" v={detail.baseModel?.title ?? run.baseModelId} mono />
          <Kv k="dataset" v={run.datasetName} />
          <Kv
            k="dataset_version"
            v={
              detail.dataset?.snapshotVersion
                ? `v${detail.dataset.snapshotVersion} — ${(detail.dataset.snapshotChecksum ?? "").slice(0, 16)}…`
                : "not pinned — this run resolved no immutable snapshot"
            }
            mono
            small
          />
          <Kv k="code_version" v={rep.gitCommit ? `git:${rep.gitCommit.slice(0, 7)}` : "not recorded — GIT_COMMIT isn't set on this deployment"} mono small />
          <Kv k="started_at" v={live.claimedAt ? `${formatAgo(live.claimedAt)} · ${new Date(live.claimedAt).toLocaleString()}` : "not claimed yet"} />
          <div className="kv-row">
            <span className="k">status</span>
            <span className="v"><StatusPill status={shownStatus} /></span>
          </div>
          {live.claimedBy && <Kv k="claimed_by" v={live.claimedBy} mono small />}
          <Kv k="attempt" v={`${live.attemptCount} of ${live.maxAttempts}`} />
          {shownStatus === "cancelled" ? (
            <div className="callout" style={{ marginTop: 12 }}>
              <strong>Cancelled.</strong> {run.error!.replace(/^CANCELLED:\s*/, "").replace(/^./, (c) => c.toUpperCase())}.
            </div>
          ) : run.status === "failed" && run.error && (
            <div className="callout callout-red" style={{ marginTop: 12 }}>
              <strong>Failed.</strong> {run.error}
            </div>
          )}
          {live.inFlight && (
            <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
              <button hidden={!can(PERM.train)}
                className="btn btn-danger-ghost btn-sm"
                style={{ width: "100%" }}
                disabled={cancelling || live.cancelRequested}
                onClick={cancel}
              >
                <Icon name="x" />
                {live.cancelRequested ? "Cancellation already requested" : cancelling ? "Cancelling…" : "Cancel this run"}
              </button>
              <div className="hint" style={{ marginTop: 6 }}>
                Stops the trainer process now; the run ends as cancelled and nothing is registered.
              </div>
            </div>
          )}
        </div>

        <div className="card card-pad">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <div className="drawer-section-title" style={{ marginBottom: 0 }}>Live monitoring</div>
            <StatusPill status={shownStatus} />
          </div>
          {progress.pct !== null && (
            <>
              <div className="progress-track" style={{ marginBottom: 6 }}><div className="progress-fill" style={{ width: `${progress.pct}%` }} /></div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-faint)", marginBottom: 2 }}>
                <span>{progress.unit}</span><span>{progress.pct}%</span>
              </div>
              <div className="hint" style={{ marginBottom: 14 }}>
                {[progress.epoch, progress.loss, progress.eta].filter(Boolean).join(" · ")}
              </div>
            </>
          )}
          <div className="metric-grid" style={{ gridTemplateColumns: "repeat(2,1fr)", marginBottom: 0 }}>
            <Mini label="Elapsed" value={live.elapsedSeconds === null ? "—" : formatElapsed(live.elapsedSeconds)} />
            <Mini label="Attempt" value={`${live.attemptCount}/${live.maxAttempts}`} />
            <Mini label="Worker" value={live.claimedBy ? live.claimedBy.split("-").slice(-1)[0] : "—"} sub={live.claimedBy ? "holding this run" : "not claimed"} />
            <Mini
              label="Heartbeat"
              value={live.heartbeatAt ? `${Math.max(0, Math.round((Date.now() - new Date(live.heartbeatAt).getTime()) / 1000))}s` : "—"}
              sub={live.inFlight ? "since last beat" : "run released"}
            />
          </div>
          {progress.latest && (
            <div className="kv-row" style={{ marginTop: 12 }}>
              <span className="k">Latest activity</span>
              <span className="v" style={{ fontSize: 12, textAlign: "right" }}>{progress.latest}</span>
            </div>
          )}
          <div className="hint" style={{ marginTop: 12 }}>
            {progress.pct !== null
              ? "Elapsed, claim and heartbeat come from the run row the worker maintains; step progress, loss and ETA from the trainer's own live log. CPU/memory aren't measured, so they aren't shown."
              : live.note}
          </div>
        </div>
      </div>

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Training metrics</div>
        {metrics.length ? (
          <>
            <div className="metric-grid" style={{ marginBottom: 0 }}>
              {metrics.slice(0, 8).map(([k, v]) => (
                <Mini key={k} label={metricLabel(k)} value={formatMetric(k, v)} />
              ))}
            </div>
            <div className="hint" style={{ marginTop: 12 }}>
              Measured on the held-out test split by the trainer itself, at the end of this run — not
              re-derived here, and not serving-time metrics. Production performance for a promoted
              version is on the model's Performance tab.
            </div>
          </>
        ) : (
          <div className="empty-hint">
            {run.status === "succeeded"
              ? "This run recorded no metrics — its task reports none of the measures stored here."
              : `No metrics: this run is ${shownStatus}.`}
          </div>
        )}
        {run.qualityGateWarnings?.length ? (
          <div className="callout callout-amber" style={{ marginTop: 14 }}>
            <strong>Dataset quality findings.</strong> Non-blocking, raised before training ran:
            <ul style={{ margin: "8px 0 0 18px", padding: 0 }}>
              {run.qualityGateWarnings.map((w, i) => <li key={i} style={{ marginBottom: 4 }}>{w}</li>)}
            </ul>
          </div>
        ) : null}
      </div>

      {detail.search && (
        <div className="card card-pad" style={{ marginBottom: 16 }}>
          <div className="drawer-section-title">Hyperparameter search results</div>
          <div className="hint" style={{ marginBottom: 10 }}>
            {detail.search.numTrials} trial{detail.search.numTrials === 1 ? "" : "s"} — optimizing for{" "}
            {detail.search.objectiveMetric} ({detail.search.objectiveDirection}). This run was launched from
            the winning trial.
          </div>
          <div className="table-wrap" style={{ marginBottom: 18 }}>
            <table>
              <thead><tr><th style={{ width: "24%" }}>Trial</th><th style={{ width: "48%" }}>Hyperparameters</th><th style={{ width: "28%" }}>{detail.search.objectiveMetric}</th></tr></thead>
              <tbody>
                {detail.search.trials.map((t) => (
                  <tr key={t.trialNumber} style={t.isBest ? { background: "var(--teal-100)", fontWeight: 700 } : undefined}>
                    <td className="cell-name">Trial {t.trialNumber}{t.isBest ? " ← BEST" : ""}</td>
                    <td style={{ fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                      {Object.entries(t.hyperparams).map(([k, v]) => `${k}=${v}`).join(", ")}
                    </td>
                    <td style={{ fontFamily: "var(--font-mono)" }}>{t.metricValue ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="drawer-section-title">Best model candidate</div>
          <div className="field-row" style={{ alignItems: "flex-start" }}>
            <div>
              <Kv k="Algorithm" v={detail.search.best.algorithm} />
              <Kv k="Winning trial" v={`#${detail.search.best.trialNumber}`} mono />
              <Kv k={detail.search.objectiveMetric} v={String(detail.search.best.metricValue ?? "—")} mono />
            </div>
            <div>
              {Object.entries(detail.search.best.hyperparams).map(([k, v]) => (
                <Kv key={k} k={k} v={String(v)} mono />
              ))}
            </div>
          </div>
        </div>
      )}

      <ValidationGateCard gate={detail.validationGate} />

      <div className="card card-pad">
        <div className="drawer-section-title">Reproducibility record</div>
        <div className="hint" style={{ marginBottom: 10 }}>
          Everything needed to reproduce this exact run later — same data, same code, same environment.
          Captured when the run registered; a field that wasn't captured says so rather than showing a
          stand-in.
        </div>
        <Kv
          k="Dataset version / hash"
          v={rep.datasetChecksum ? `v${rep.datasetSnapshotVersion ?? "?"} · sha256:${rep.datasetChecksum}` : rep.syntheticData ? "synthetic data — the dataset had no uploaded file when this ran" : "not recorded"}
          mono
          small
        />
        <Kv k="Feature list" v={rep.features?.length ? rep.features.join(", ") : detail.dataset?.structureType === "unstructured" ? "n/a — fine-tuned directly on raw text" : "not recorded"} />
        <Kv k="Target" v={rep.target ?? "n/a — unsupervised"} mono />
        <Kv
          k="Train / validation / test split"
          v={rep.split ? splitLabel(rep.split) : "not recorded"}
        />
        <Kv k="Random seed" v={rep.seed === null ? "not recorded" : String(rep.seed)} mono />
        <Kv k="Algorithm" v={rep.algorithm} />
        <Kv k="Library versions" v={rep.libraryVersions ?? "not captured for this run — recorded from the training interpreter on runs from v2.37.1 onward"} mono small />
        <Kv k="Python version" v={rep.pythonVersion ?? "not captured for this run"} mono />
        <Kv k="Source-code version" v={rep.gitCommit ? `git:${rep.gitCommit}` : `trainer ${rep.trainerVersion ?? "—"} (GIT_COMMIT not set on this deployment)`} mono small />
        <Kv k="Environment" v={rep.environment ?? "not recorded"} mono small />
        <Kv k="Node version" v={rep.nodeVersion ?? "not recorded"} mono />
        <Kv k="requirements-training.txt" v={rep.trainingRequirementsSha256 ? `sha256:${rep.trainingRequirementsSha256.slice(0, 24)}…` : "not recorded"} mono small />
        <Kv k="package-lock.json" v={rep.packageLockSha256 ? `sha256:${rep.packageLockSha256.slice(0, 24)}…` : "not recorded"} mono small />
        <div className="field" style={{ marginTop: 14, marginBottom: 0 }}>
          <label>Artifact (trained weights)</label>
          <ArtifactLocation uri={rep.weightsUri} />
        </div>
        <div className="field" style={{ marginTop: 14, marginBottom: 0 }}>
          <label>Hyperparameters</label>
          <pre className="step-log" style={{ color: "#9FB0D0", padding: "12px 14px", margin: 0 }}>
            {rep.hyperparameters && Object.keys(rep.hyperparameters).length
              ? JSON.stringify(rep.hyperparameters, null, 2)
              : "{}\n\n// No overrides were passed — this run used the trainer's own defaults for\n// every hyperparameter."}
          </pre>
        </div>
      </div>
    </div>
  );
}

/* ---------------- validation gate ---------------- */

function ValidationGateCard({ gate }: { gate: ValidationGate }) {
  if (!gate.applicable) {
    return (
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Validation gate</div>
        <div className="empty-hint">{gate.reason}</div>
      </div>
    );
  }
  const verdictClass =
    gate.verdict.status === "passed" ? "callout callout-teal"
    : gate.verdict.status === "failed" ? "callout callout-red"
    : "callout callout-amber";
  const verdictTitle =
    gate.verdict.status === "passed" ? "✓ VALIDATION PASSED"
    : gate.verdict.status === "failed" ? "✗ VALIDATION FAILED"
    : "! VALIDATION INCOMPLETE";

  return (
    <div className="card card-pad" style={{ marginBottom: 16 }}>
      <div className="drawer-section-title">Validation gate</div>
      <div className="hint" style={{ marginBottom: 10 }}>
        {gate.description} Policy <code>{gate.policyId}</code>, read from the platform's stored
        acceptance criteria — separate from, and in addition to, the regression gate's comparison
        against the current champion.
      </div>
      <div className="table-wrap" style={{ marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: "30%" }}>Metric</th>
              <th style={{ width: "22%" }}>Required</th>
              <th style={{ width: "22%" }}>Actual</th>
              <th style={{ width: "26%" }}>Result</th>
            </tr>
          </thead>
          <tbody>
            {gate.rows.map((r) => (
              <tr key={r.key}>
                <td className="cell-name">{r.label}</td>
                <td style={{ fontFamily: "var(--font-mono)", color: "var(--text-faint)" }}>
                  {r.direction === "min" ? "≥" : "≤"} {r.threshold}
                </td>
                <td style={{ fontFamily: "var(--font-mono)" }}>{r.actual === null ? "—" : r.actual}</td>
                <td><GatePill result={r.result} /></td>
              </tr>
            ))}
            {gate.generalization && (
              <tr>
                <td className="cell-name">{gate.generalization.label}</td>
                <td style={{ fontFamily: "var(--font-mono)", color: "var(--text-faint)" }}>≤ {gate.generalization.maxGap}</td>
                <td style={{ fontFamily: "var(--font-mono)" }}>{gate.generalization.gap === null ? "—" : gate.generalization.gap}</td>
                <td><GatePill result={gate.generalization.result} /></td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className={verdictClass}>
        <strong>{verdictTitle}</strong> — {gate.verdict.message}
      </div>
    </div>
  );
}

function GatePill({ result }: { result: GateRowResult }) {
  if (result === "pass") return <span className="status-pill status-success"><span className="status-dot" /> PASS</span>;
  if (result === "fail") return <span className="status-pill status-failed"><span className="status-dot" /> FAIL</span>;
  return <span className="status-pill status-queued" title="This run never recorded this metric, so the check could not be evaluated."><span className="status-dot" /> NOT RECORDED</span>;
}

/* ---------------- live progress ---------------- */

/** Step progress from the trainer's live log. LLM fine-tunes log their step
 *  count, loss, epoch and ETA every few steps; classical trainers fit in one
 *  call, so they get the latest activity line and no made-up percentage. */
function readProgress(logs: PipelineLog[], finished: boolean) {
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  let maxSteps: number | null = null, epochs: number | null = null;
  let step: number | null = null, loss: number | null = null, epoch: number | null = null, eta: number | null = null;
  for (const l of logs) {
    const d = l.data ?? {};
    maxSteps = num(d.max_steps) ?? maxSteps;
    epochs = num(d.epochs) ?? epochs;
    if (num(d.step) !== null && num(d.loss) !== null) {
      step = num(d.step); loss = num(d.loss); epoch = num(d.epoch); eta = num(d.eta_s);
    }
  }
  const latest = logs.length ? logs[logs.length - 1].msg : null;
  if (!maxSteps) return { pct: null, unit: "", epoch: "", loss: "", eta: "", latest };
  const done = finished ? maxSteps : (step ?? 0);
  return {
    pct: Math.min(100, Math.round((done / maxSteps) * 100)),
    unit: `Steps ${done} / ${maxSteps}`,
    epoch: epochs ? `Epoch ${Math.min(epochs, Math.max(1, Math.ceil(epoch ?? 0)))} of ${epochs}` : "",
    loss: loss !== null ? `loss ${loss.toFixed(4)}` : "",
    eta: !finished && eta !== null ? `ETA ${Math.floor(eta / 60)}m ${String(eta % 60).padStart(2, "0")}s` : "",
    latest,
  };
}

/* ---------------- small pieces ---------------- */

function BackButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={onClick}>
      <Icon name="arrow-left" />
      Back to pipelines
    </button>
  );
}

function Kv({ k, v, mono, small }: { k: string; v: string; mono?: boolean; small?: boolean }) {
  return (
    <div className="kv-row">
      <span className="k">{k}</span>
      <span className="v" style={{ fontFamily: mono ? "var(--font-mono)" : undefined, fontSize: small ? 11 : undefined, wordBreak: "break-all" }}>{v}</span>
    </div>
  );
}

function Mini({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="metric-mini">
      <div className="m-label">{label}</div>
      <div className="m-val" style={{ fontSize: 18 }}>{value}</div>
      {sub && <div className="m-sub">{sub}</div>}
    </div>
  );
}

/** Counts are counts, not rates: only genuinely percentage-scaled metrics get a
 *  % suffix, so "clustersFound 4" is never rendered as "4%". */
const PERCENT_METRICS = new Set(["accuracy", "f1", "precision", "recall", "trainAccuracy", "mape", "anomalyRate", "explainedVariancePct"]);
const LABELS: Record<string, string> = {
  accuracy: "Accuracy", f1: "F1", precision: "Precision", recall: "Recall", rocAuc: "ROC-AUC",
  trainAccuracy: "Training accuracy", trainR2: "Training R²", r2: "R²", mse: "MSE", rmse: "RMSE",
  mae: "MAE", mape: "MAPE", silhouetteScore: "Silhouette", clustersFound: "Clusters found",
  anomalyRate: "Flagged rate", reconstructionMse: "Reconstruction MSE", trustworthiness: "Trustworthiness",
  explainedVariancePct: "Explained variance", inputDimensions: "Input dimensions",
  outputDimensions: "Output dimensions", evalLoss: "Validation loss", trainLoss: "Training loss",
  perplexity: "Perplexity",
};
function metricLabel(k: string) {
  return LABELS[k] ?? k.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase());
}
function formatMetric(k: string, v: unknown) {
  if (typeof v !== "number") return String(v);
  return PERCENT_METRICS.has(k) ? `${v}%` : String(v);
}

function splitLabel(split: NonNullable<TrainingRunDetail["reproducibility"]["split"]>) {
  const n = (v: number) => v.toLocaleString();
  const pct = (v: number | null) => (v == null ? "?" : String(v));
  const unit = split.unit ?? "rows";
  const parts: string[] = [];
  if (split.source === "requested only") {
    parts.push(`${pct(split.train)} / ${pct(split.val)} / ${pct(split.test)} requested — the split actually used wasn't recorded for this run`);
  } else {
    parts.push(`${pct(split.train)} / ${pct(split.val)} / ${pct(split.test)}`);
    if (split.counts) parts.push(`${n(split.counts.train)} train / ${n(split.counts.val)} validation / ${n(split.counts.test)} test ${unit}`);
    if (split.method) parts.push(split.method);
  }
  return parts.join(" · ");
}

function formatElapsed(seconds: number) {
  const hh = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const mm = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

export function formatAgo(iso: string) {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
