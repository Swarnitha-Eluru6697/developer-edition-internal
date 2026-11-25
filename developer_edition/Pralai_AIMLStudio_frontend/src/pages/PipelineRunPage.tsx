import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import { StatusPill } from "../components/StatusPill";
import { DagStages } from "../components/DagStages";
import { confirmDialog } from "../components/ConfirmDrawer";
import type { PipelineDetail } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

/**
 * A pipeline run's stages and its real log. Reached from the Pipelines list and
 * from a training run's "View logs" — the backend resolves the same id for both
 * a stored pipeline and a training run, so one page serves both kinds.
 *
 * The log is the run's own recorded timeline. Nothing is synthesised to pad it
 * out: a run with three real events shows three lines.
 */
export default function PipelineRunPage() {
  const { can } = usePermissions();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<PipelineDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setDetail(await api.getPipeline(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (detail?.status !== "running" && detail?.status !== "queued") return;
    const timer = setInterval(() => { void load(); }, 1500);
    return () => clearInterval(timer);
  }, [detail?.status, load]);

  async function restart() {
    setRestarting(true);
    setError(null);
    // A failed training run restarts as a fresh run with the same configuration and
    // dataset snapshot (the failed one and its log are kept), then opens its live page.
    if (detail?.kind === "training-run") {
      try {
        if (!(await confirmDialog({ title: "Restart this training run?",
            message: "A new run starts with the same base model, dataset snapshot and settings. This failed run and its log stay as they are.",
            confirmLabel: "Restart" }))) return;
        const next = await api.retrainTrainingRun(id);
        navigate(`/training-runs/${next.id}`);
      } catch (e) {
        setError(e instanceof ApiRequestError ? e.message : String(e));
      } finally {
        setRestarting(false);
      }
      return;
    }
    try {
      const result = await api.restartPipeline(id) as { ok: true; warning?: string };
      setNotice(result.warning ?? "Resubmitted — the run is back on the queue for a worker to claim.");
      await load();
    } catch (e) {
      setError(e instanceof ApiRequestError ? e.message : String(e));
    } finally {
      setRestarting(false);
    }
  }

  async function copyLogs() {
    if (!detail) return;
    const text = detail.logs.map((l) => `${l.t} [${l.src}] ${l.msg}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Couldn't copy — the browser denied clipboard access. Select the log text and copy it manually.");
    }
  }

  const back = (
    <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={() => navigate("/pipelines")}>
      <Icon name="arrow-left" />
      Back to pipelines
    </button>
  );

  if (error && !detail) return <div>{back}<div className="callout callout-amber">Couldn't load this run: {error}</div></div>;
  if (!detail) return <div>{back}<div className="empty-hint">Loading…</div></div>;

  const isTrainingRun = detail.kind === "training-run";

  return (
    <div>
      <div className="page-fixed">
        {back}

        <div className="page-head">
          <div>
            <div className="badge-row" style={{ marginBottom: 10 }}><StatusPill status={detail.status} /></div>
            <h1 style={{ fontFamily: "var(--font-mono)", fontSize: 19 }}>{detail.title}</h1>
            {/* A training run's `meta` already names its compute profile; a stored
                pipeline's doesn't. Printing both unconditionally showed
                "cpu-small · … · cpu-small". */}
            <p>
              {[
                `Started ${formatAgo(detail.startedAt)}`,
                detail.meta.includes(detail.computeProfile) ? null : detail.computeProfile,
                detail.meta,
              ].filter(Boolean).join(" · ")}
            </p>
          </div>
          <div style={{ display: "flex", gap: 8, flex: "none" }}>
            {detail.status === "failed" && (
              <button hidden={!can(PERM.train)}
                className="btn btn-secondary"
                style={{ color: "var(--red)", borderColor: "var(--red)" }}
                disabled={restarting}
                onClick={restart}
              >
                <Icon name="refresh" />
                {restarting ? "Restarting…" : "Restart pipeline"}
              </button>
            )}
            {isTrainingRun && (
              <button className="btn btn-secondary btn-sm" onClick={() => navigate(`/training-runs/${detail.id}`)}>
                <Icon name="flask" />
                Training run detail
              </button>
            )}
            <button className="btn btn-secondary btn-sm" onClick={() => navigate(`/experiments?run=${detail.id}`)}>
              <Icon name="trend" />
              View in Experiments
            </button>
          </div>
        </div>
      </div>

      {notice && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{notice}</div>}
      {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="drawer-section-title">Pipeline stages</div>
        <DagStages stages={detail.stages} />
      </div>

      <div className="card card-pad">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
          <div className="drawer-section-title" style={{ marginBottom: 0 }}>Logs</div>
          <IconAction icon={copied ? "check" : "copy"} label={copied ? "Copied logs" : "Copy logs"} align="end" onClick={copyLogs} />
        </div>
        <p className="hint" style={{ marginBottom: 14 }}>
          {isTrainingRun
            ? "Written live while the run works: dataset download, every trainer stage (data shape, split, parameters, per-step loss and ETA for LLMs, metrics), artifact upload and catalog registration. Lines with \u201cdetails\u201d carry the full structured values."
            : "Log lines written by the workflow executor as each stage ran."}
        </p>
        <div className="step-log" style={{ maxHeight: 480 }}>
          {detail.logs.map((l, i) => (
            <div className={"ln " + (l.level === "error" ? "err" : l.level === "ok" ? "ok" : "")} key={i}>
              <span className="t">{elapsedLabel(detail.logs, i)}</span>
              <span className="msg">
                [{l.src}] {l.msg}
                {l.data && Object.keys(l.data).length > 0 && (
                  <details style={{ display: "inline-block", marginLeft: 8, verticalAlign: "top" }}>
                    <summary style={{ cursor: "pointer", opacity: 0.75 }}>details</summary>
                    <pre style={{ margin: "4px 0 0", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 11.5, opacity: 0.9 }}>
                      {JSON.stringify(l.data, null, 2)}
                    </pre>
                  </details>
                )}
              </span>
            </div>
          ))}
          {!detail.logs.length && <div className="ln"><span className="msg">No log lines recorded for this run.</span></div>}
        </div>
      </div>
    </div>
  );
}

/**
 * Wall-clock time plus an offset from the first line. Local training runs
 * finish inside a second or two, so second-resolution timestamps alone collapse
 * every line to the same value and read as if nothing took any time — `atMs`
 * carries the precision needed to show what actually elapsed.
 */
function elapsedLabel(logs: PipelineDetail["logs"], i: number) {
  const first = logs.find((l) => l.atMs != null)?.atMs;
  const mine = logs[i].atMs;
  if (first == null || mine == null) return logs[i].t;
  const delta = (mine - first) / 1000;
  return `${logs[i].t} +${delta.toFixed(delta < 10 ? 2 : 0)}s`;
}

function formatAgo(iso: string) {
  if (!iso) return "—";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
