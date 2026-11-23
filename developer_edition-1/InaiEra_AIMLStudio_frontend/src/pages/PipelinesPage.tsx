import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import SearchSelect from "../components/SearchSelect";
import { StatusPill } from "../components/StatusPill";
import { confirmDialog } from "../components/ConfirmDrawer";
import { dagClass } from "../components/DagStages";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";
import type { HpoSearch, Pipeline, PipelineDefinition, PipelineDefinitionStage, PipelineDetail } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

type TopTab = "runs" | "definitions";

/** One row on the Runs tab. Training runs and definition runs come from
 *  /pipelines; hyperparameter searches (the mock's `tune-…-hpo` rows) come from
 *  /hpo-searches and are listed alongside them — each is one workflow. */
type RunRow = Omit<Pipeline, "kind"> & { kind: "training-run" | "definition" | "hpo" };

const ACTIVE = new Set(["queued", "running"]);

function hpoAsRun(s: HpoSearch): RunRow {
  const status = s.status === "completed" ? "succeeded" : (s.status as RunRow["status"]);
  return {
    id: s.id,
    title: `tune-${s.baseModelId}-${s.datasetName}`,
    status,
    computeProfile: (s as any).computeProfile ?? "cpu-small",
    meta: `Ray Tune + Optuna, ${s.numTrials} trial${s.numTrials === 1 ? "" : "s"}`,
    startedAt: s.createdAt,
    kind: "hpo",
    currentStage: `Hyperparameter search · optimizing ${s.objectiveMetric} (${s.objectiveDirection})`,
  } as RunRow;
}

export default function PipelinesPage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<TopTab>("runs");
  const definitions = useApi(api.getPipelineDefinitions, []);
  const [viewMode, setViewMode] = useState<"list" | "card">("card");

  // Runs are held locally rather than through useApi so the live refresh below
  // can swap rows in place — a reload() blanks the list on every tick.
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const loadRuns = useCallback(async () => {
    try {
      const [pipelines, searches] = await Promise.all([
        api.getPipelines(),
        api.getHpoSearches().catch(() => [] as HpoSearch[]),
      ]);
      const merged: RunRow[] = [
        ...pipelines.map((p) => ({ ...p, kind: p.kind ?? "definition" }) as RunRow),
        ...searches.map(hpoAsRun),
      ].sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime());
      setRuns(merged);
      setRunsError(null);
    } catch (e) {
      setRunsError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => { void loadRuns(); }, [loadRuns]);

  // Live while anything is in flight — stages advance on the server in real
  // wall-clock time, and the list follows them instead of freezing at "running".
  const anyActive = !!runs?.some((r) => ACTIVE.has(r.status));
  useEffect(() => {
    if (!anyActive) return;
    const t = setInterval(() => { void loadRuns(); }, 3000);
    return () => clearInterval(t);
  }, [anyActive, loadRuns]);

  const [restarting, setRestarting] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);

  // "View run" opens the quick look (stage flow + full logs / Experiments) in
  // the side drawer; failed runs go straight to their full log.
  const [viewing, setViewing] = useState<RunRow | null>(null);
  function openRun(r: RunRow) {
    if (r.kind === "hpo") return navigate("/experiments?tab=hpo");
    if (r.status === "failed") return navigate(`/pipelines/${r.id}`);
    setViewing(r);
  }

  // A failed training run goes back on the queue with the same configuration and
  // pinned snapshot; a definition run is resubmitted from its first stage. In
  // both cases the failure log is kept — it's the reason for restarting.
  async function restart(r: RunRow) {
    const isRun = r.kind === "training-run";
    if (!(await confirmDialog({
      title: `Restart "${r.title}"?`,
      message: isRun
        ? "The run goes back on the queue with the same base model, dataset snapshot and settings. Its failure log stays for reference."
        : "This resubmits the workflow from its first stage. The failed run's log stays for reference.",
      confirmLabel: "Restart",
    }))) return;
    setRestarting(r.id);
    setNotice(null);
    try {
      const res: any = await api.restartPipeline(r.id);
      setNotice({
        text: res?.warning ? `Restarted, but: ${res.warning}` : `"${r.title}" restarted — ${isRun ? "queued for a worker" : "starting from its first stage"}.`,
        ok: !res?.warning,
      });
      await loadRuns();
    } catch (e) {
      setNotice({ text: `Couldn't restart: ${e instanceof Error ? e.message : String(e)}`, ok: false });
    } finally {
      setRestarting(null);
    }
  }

  // Tooltips open upward: in the list these sit in an overflow-clipped table.
  const actions = (r: RunRow) => (
    <div style={{ display: "flex", gap: 6, flex: "none" }}>
      {r.status === "failed" && r.kind !== "hpo" ? (
        <>
          <IconAction icon="file" label={`View logs of ${r.title}`} tip="up" align="end" onClick={() => openRun(r)} />
          <IconAction icon="refresh" label={restarting === r.id ? `Restarting ${r.title}…` : `Restart ${r.title}`} danger gated={PERM.train} tip="up" align="end"
            disabled={restarting === r.id} onClick={() => { void restart(r); }} />
        </>
      ) : (
        <IconAction icon="eye" label={r.kind === "hpo" ? `View trials of ${r.title}` : `View run ${r.title}`} tip="up" align="end" onClick={() => openRun(r)} />
      )}
    </div>
  );

  const [query] = usePageSearch(tab === "runs" ? "Search pipeline runs…" : "Search pipeline definitions…");
  const shownRuns = runs?.filter((r) => matchesQuery(query, r.title, r.status, r.meta, r.computeProfile, r.currentStage, r.id)) ?? null;
  const noMatch = <>No pipeline runs match “{query.trim()}”.</>;

  // Run counts per definition name, for the Definitions tab's "run N times".
  const runsByDefinition = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of runs ?? []) if (r.definitionId) m.set(r.definitionId, (m.get(r.definitionId) ?? 0) + 1);
    return m;
  }, [runs]);

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Pipelines</h1>
            <p>
              One Argo Workflow per training request — validate, train (RayJob on KubeRay), evaluate,
              register. Not four services polling each other.
            </p>
          </div>
        </div>

        <div className="tabs">
          <button className={"tab" + (tab === "runs" ? " active" : "")} onClick={() => setTab("runs")}>Runs</button>
          <button className={"tab" + (tab === "definitions" ? " active" : "")} onClick={() => setTab("definitions")}>
            Definitions {definitions.data ? <span>({new Set(definitions.data.map((d) => d.name)).size})</span> : null}
          </button>
        </div>

        {/* Pinned with the header on Runs: the fleet stays in view while the
            run list scrolls underneath it. */}
        {tab === "runs" && (
          <div style={{ marginTop: 16 }}>
            <div className="page-head" style={{ marginBottom: 10 }}>
              <div className="hint" style={{ margin: 0 }}>
                Pipeline logs and DAG state are retained for 30 days after completion.
                {anyActive && <span style={{ marginLeft: 8, color: "var(--amber-700)" }}>● live — refreshing every 3s</span>}
              </div>
              <div className="view-toggle">
                <button className={"has-tip" + (viewMode === "list" ? " active" : "")} onClick={() => setViewMode("list")} data-tip="List view" aria-label="List view"><Icon name="list" /></button>
                <button className={"has-tip tip-end" + (viewMode === "card" ? " active" : "")} onClick={() => setViewMode("card")} data-tip="Card view" aria-label="Card view"><Icon name="grid" /></button>
              </div>
            </div>

            <WorkerFleetCard tick={runs} />
          </div>
        )}
      </div>

      {notice && <div className={notice.ok ? "callout callout-teal" : "callout callout-amber"} style={{ margin: "14px 0" }}>{notice.text}</div>}

      {tab === "runs" && (
        <div style={{ marginTop: 16 }}>

          {runs === null && !runsError && <div className="empty-hint">Loading…</div>}
          {runsError && <div className="callout callout-amber">Couldn't load pipelines: {runsError}</div>}

          {runs && (viewMode === "list" ? (
            <div className="table-wrap" style={{ marginBottom: 16 }}>
              <table>
                <thead>
                  <tr>
                    <th style={{ width: "26%" }}>Run</th>
                    <th style={{ width: "11%" }}>Status</th>
                    <th style={{ width: "12%" }}>Started</th>
                    <th style={{ width: "11%" }}>Compute</th>
                    <th style={{ width: "24%" }}>Current stage</th>
                    <th style={{ width: "16%" }} />
                  </tr>
                </thead>
                <tbody>
                  {shownRuns!.map((r) => (
                    <tr key={`${r.kind}-${r.id}`} onClick={() => openRun(r)}>
                      <td className="cell-name">{r.title}</td>
                      <td><StatusPill status={r.status} /></td>
                      <td style={{ color: "var(--text-faint)", fontSize: 12 }}>{formatStarted(r.startedAt)}</td>
                      <td style={{ fontSize: 12 }}>{r.computeProfile}</td>
                      <td style={{ fontSize: 12 }}>{r.currentStage ?? r.meta}</td>
                      <td onClick={(e) => e.stopPropagation()}>{actions(r)}</td>
                    </tr>
                  ))}
                  {!runs.length && (
                    <tr><td colSpan={6}><div className="empty-hint">No pipeline runs yet — launch one from the Train wizard or run a definition.</div></td></tr>
                  )}
                  {runs.length > 0 && !shownRuns!.length && (
                    <tr><td colSpan={6}><div className="empty-hint">{noMatch}</div></td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <div>
              {shownRuns!.map((r) => <PipelineCard key={`${r.kind}-${r.id}`} run={r} actions={actions(r)} />)}
              {!runs.length && (
                <div className="card card-pad"><div className="empty-hint">No pipeline runs yet — launch one from the Train wizard or run a definition.</div></div>
              )}
              {runs.length > 0 && !shownRuns!.length && (
                <div className="card card-pad"><div className="empty-hint">{noMatch}</div></div>
              )}
            </div>
          ))}
        </div>
      )}

      {viewing && <RunDrawer run={viewing} live={anyActive ? runs : null} onClose={() => setViewing(null)} />}

      {tab === "definitions" && (
        <div style={{ marginTop: 16 }}>
          <DefinitionsTab
            definitions={definitions}
            query={query}
            runsByDefinition={runsByDefinition}
            onRan={(msg) => { setNotice({ text: msg, ok: true }); void loadRuns(); setTab("runs"); }}
          />
        </div>
      )}
    </div>
  );
}

function formatStarted(iso: string) {
  if (!iso) return "—";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}

/**
 * The stage flow as the mock draws it — title, then one short line (how long
 * the stage took and the one fact worth knowing). The stage's full message is
 * kept as the node's tooltip; durations appear only where the server recorded one.
 */
function CompactDag({ stages, run }: { stages: PipelineDetail["stages"]; run: RunRow }) {
  if (!stages.length) return <div className="empty-hint">No stages recorded for this run.</div>;
  return (
    <div className="dag dag-lg">
      {stages.map((s, i) => (
        <div key={s.order ?? i} style={{ display: "contents" }}>
          {i > 0 && <div className="dag-arrow" />}
          <div className={"dag-node " + dagClass(s.state)} title={s.sub}>
            <div className="dn-title">{s.title}</div>
            <div className="dn-sub">{compactSub(s, run)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function compactSub(s: PipelineDetail["stages"][number], run: RunRow): string {
  const sub = s.sub ?? "";
  const dur = sub.match(/\((\d+(?:\.\d+)?)s\)\s*$/);
  const time = dur ? (Number(dur[1]) < 0.1 ? "<0.1s" : `${Math.round(Number(dur[1]) * 10) / 10}s`) : null;
  const body = dur ? sub.slice(0, dur.index).trim() : sub;
  const t = s.title.toLowerCase();
  const join = (...xs: (string | null | undefined)[]) => xs.filter(Boolean).join(" · ") || s.state;
  if (s.state === "pending") return "pending";
  if (s.state === "running") return join("in progress", time);
  if (s.state === "failed") return join("failed", time);
  if (t.includes("valid")) {
    const w = body.match(/(\d+) real warning/);
    return join(time, w ? `${w[1]} warning${w[1] === "1" ? "" : "s"}` : run.kind === "training-run" ? body : "passed");
  }
  if (t.includes("train") || t.includes("tune")) {
    // The algorithm, as in "34s · xgboost": a training run's meta starts with its base model.
    const algo = run.kind === "training-run" ? run.meta.split(" · ")[0] : body.match(/succeeded — ([^:]+):/)?.[1];
    return join(time, algo);
  }
  if (t.includes("eval")) {
    // The headline metric, in the order a reader looks for it.
    for (const k of ["accuracy", "r2", "rmse", "mae", "evalLoss", "eval_loss", "perplexity", "f1"]) {
      const m = body.match(new RegExp(`"?\\b${k}"?\\s*[:\\s]\\s*(-?[\\d.]+)`));
      if (m) return join(time, `${k} ${m[1]}`);
    }
    return join(time, null);
  }
  if (t.includes("regist")) {
    const v = body.match(/' (v\d+)/);
    return join(time, v ? v[1] : "registered");
  }
  return join(time, body.length > 40 ? null : body);
}

/**
 * Quick look at one run, as in the mock: its status, the stage flow it went
 * through, and the two ways onward — the full log, and the run in Experiments.
 * Everything comes from GET /pipelines/:id; nothing here is synthesised.
 */
function RunDrawer({ run, live, onClose }: { run: RunRow; live: unknown; onClose: () => void }) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<PipelineDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.getPipeline(run.id)
      .then((d) => { if (!cancelled) { setDetail(d); setError(null); } })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [run.id, live]);

  const d = detail ?? null;
  const trainingRunId = run.kind === "training-run" ? run.id : d?.trainingRunId ?? null;
  const status = d?.status ?? run.status;

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-md">
        <div className="drawer-head">
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)", fontFamily: "var(--font-mono)", wordBreak: "break-all" }}>{run.title}</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>
              {[`Started ${formatStarted(run.startedAt)}`, run.meta.includes(run.computeProfile) ? null : run.computeProfile, run.meta].filter(Boolean).join(" · ")}
            </div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          <StatusPill status={status} />
          <div className="drawer-section" style={{ marginTop: 16 }}>
            <div className="drawer-section-title">Stages</div>
            {error && <div className="callout callout-amber">Couldn't load this run: {error}</div>}
            {!d && !error && <div className="empty-hint">Loading…</div>}
            {d && <CompactDag stages={d.stages ?? []} run={run} />}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn btn-secondary btn-sm" onClick={() => navigate(`/pipelines/${run.id}`)}>
              <Icon name="file" />View full logs
            </button>
            <button className="btn btn-secondary btn-sm" disabled={!trainingRunId}
              title={trainingRunId ? undefined : "This run hasn't produced a training run yet"}
              onClick={() => trainingRunId && navigate(`/experiments?run=${trainingRunId}`)}>
              <Icon name="trend" />View in Experiments
            </button>
          </div>
          {d && !trainingRunId && (
            <div className="hint" style={{ marginTop: 8 }}>
              {status === "succeeded"
                ? "This pipeline has no training stage, so there's no run to show in Experiments."
                : "View in Experiments becomes available once the training stage has run."}
            </div>
          )}
          {d?.modelName && (
            <div className="hint" style={{ marginTop: 8 }}>Registered model: <code>{d.modelName}</code></div>
          )}
        </div>
      </div>
    </div>
  );
}

function PipelineCard({ run: r, actions }: { run: RunRow; actions: ReactNode }) {
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-pad" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 14, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {r.title}
            <StatusPill status={r.status} />
          </div>
          {/* `meta` for a training run already names its compute profile; a
              stored pipeline's doesn't — don't print it twice. */}
          <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 3 }}>
            {[`Started ${formatStarted(r.startedAt)}`, r.meta.includes(r.computeProfile) ? null : r.computeProfile, r.meta]
              .filter(Boolean).join(" · ")}
          </div>
          {r.currentStage && (
            <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 4 }}>{r.currentStage}</div>
          )}
        </div>
        {actions}
      </div>
    </div>
  );
}

/**
 * The worker fleet. Every worker loop beats into the server each poll cycle, so
 * idle workers are listed too (an idle worker holds no queue row). In flight is
 * claimed / slots; current runs are the model or search trial it's working on.
 * Refreshes alongside the run list.
 */
const FLEET_OPEN_KEY = "mlstudio.pipelines.fleetOpen";
const FLEET_HEIGHT_KEY = "mlstudio.pipelines.fleetTableHeight";
const FLEET_MIN_HEIGHT = 64;
const readStored = (key: string) => { try { return localStorage.getItem(key); } catch { return null; } };
const writeStored = (key: string, v: string) => { try { localStorage.setItem(key, v); } catch { /* per-viewer convenience only */ } };

function WorkerFleetCard({ tick }: { tick: unknown }) {
  const fleet = useApi(api.getWorkers, []);
  // The card is pinned above the run list, so with many workers it could push the
  // runs off screen. It collapses to one summary line, and its worker table has a
  // drag handle — the table scrolls inside whatever height is chosen. Both choices
  // are remembered per browser.
  const [open, setOpen] = useState(() => readStored(FLEET_OPEN_KEY) !== "0");
  const [tableHeight, setTableHeight] = useState(() => Number(readStored(FLEET_HEIGHT_KEY)) || 180);
  const maxHeight = () => Math.max(FLEET_MIN_HEIGHT, Math.round(window.innerHeight * 0.5));
  const toggle = () => setOpen((o) => { writeStored(FLEET_OPEN_KEY, o ? "0" : "1"); return !o; });
  const startResize = (e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY, startH = tableHeight;
    let h = startH;
    const move = (ev: PointerEvent) => {
      h = Math.min(maxHeight(), Math.max(FLEET_MIN_HEIGHT, startH + ev.clientY - startY));
      setTableHeight(h);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
      writeStored(FLEET_HEIGHT_KEY, String(h));
    };
    document.body.style.cursor = "ns-resize";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const nudge = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    const h = Math.min(maxHeight(), Math.max(FLEET_MIN_HEIGHT, tableHeight + (e.key === "ArrowDown" ? 24 : -24)));
    setTableHeight(h);
    writeStored(FLEET_HEIGHT_KEY, String(h));
  };
  useEffect(() => { if (tick) fleet.reload(); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);
  const workers = fleet.data?.fleet;
  const live = (workers ?? []).filter((w) => w.live);
  const inFlight = live.reduce((n, w) => n + w.inFlight, 0);
  const slots = live.reduce((n, w) => n + w.capacity, 0);
  const queued = (fleet.data?.queuedRuns ?? 0) + (fleet.data?.queuedHpoTrials ?? 0);
  const ago = (s: number) => (s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`);

  return (
    <div className="card card-pad" style={{ marginBottom: 16, ...(open ? {} : { paddingTop: 12, paddingBottom: 12 }) }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, marginBottom: open ? 10 : 0 }}>
        <button className="fleet-toggle" onClick={toggle} aria-expanded={open} title={open ? "Collapse the worker fleet" : "Expand the worker fleet"}>
          <Icon name={open ? "chevron-down" : "chevron-right"} />
          <span className="drawer-section-title" style={{ marginBottom: 0 }}>Worker fleet</span>
        </button>
        {!open && (
          <span className="hint" style={{ margin: 0, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {workers ? `${live.length} active worker${live.length === 1 ? "" : "s"} · ${inFlight} / ${slots} in flight · ${queued} queued` : "—"}
          </span>
        )}
        <IconAction icon="refresh" label="Refresh worker fleet" align="end" onClick={() => fleet.reload()} />
      </div>
      {open && <>
      <div className="hint" style={{ marginBottom: 12 }}>
        Separate worker processes — each independently deployable and scalable — all claiming from the
        same Postgres queue. Each worker reports a heartbeat every poll cycle, so idle workers show up too.
      </div>

      <div className="field-row" style={{ gridTemplateColumns: "repeat(3,1fr)", marginBottom: 14 }}>
        <div className="metric-mini" style={{ textAlign: "center" }}>
          <div className="m-label">Active workers</div>
          <div className="m-val" style={{ fontSize: 18 }}>{workers ? live.length : "—"}</div>
        </div>
        <div className="metric-mini" style={{ textAlign: "center" }}>
          <div className="m-label">In flight</div>
          <div className="m-val" style={{ fontSize: 18 }}>{workers ? `${inFlight} / ${slots}` : "—"}</div>
          {workers && <div className="m-sub">claimed / slots</div>}
        </div>
        <div className="metric-mini" style={{ textAlign: "center" }}>
          <div className="m-label">Queued (unclaimed)</div>
          <div className="m-val" style={{ fontSize: 18 }}>{fleet.data ? queued : "—"}</div>
          {fleet.data && <div className="m-sub">{fleet.data.queuedRuns} runs · {fleet.data.queuedHpoTrials} trials</div>}
        </div>
      </div>

      {fleet.error && <div className="callout callout-amber">Couldn't load the worker fleet: {fleet.error}</div>}
      {workers && workers.length > 0 && (<>
        <div className="table-wrap fleet-table" style={{ maxHeight: tableHeight }}>
          <table>
            <thead>
              <tr><th style={{ width: "34%" }}>Worker</th><th style={{ width: "9%" }}>Pool</th><th style={{ width: "10%" }}>In flight</th><th style={{ width: "15%" }}>Heartbeat</th><th>Current run(s)</th></tr>
            </thead>
            <tbody>
              {workers.map((w) => (
                <tr key={`${w.hostname}-${w.pid}`} style={{ cursor: "default" }}>
                  <td className="cell-name" style={{ fontSize: 12.5, wordBreak: "break-all" }} title={`${w.hostname} · pid ${w.pid} · ${w.roles.join(" + ")} loops`}>{w.workerId}</td>
                  <td><span className="tag" title={w.pool === "any" ? "Claims every compute profile (WORKER_COMPUTE_PROFILES unset)" : undefined}>{w.pool}</span></td>
                  <td>{w.inFlight} / {w.capacity}</td>
                  <td style={{ fontSize: 12, whiteSpace: "nowrap", color: w.live ? "var(--teal-700)" : "var(--red)" }}>
                    {w.live ? "● live" : "⚠ not responding"} · {ago(w.secondsSinceHeartbeat)}
                  </td>
                  <td style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {w.current.length
                      ? w.current.map((c) => c.label).join(", ")
                      : w.live ? "idle — waiting for real queued work" : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="fleet-resize" role="separator" aria-orientation="horizontal" aria-label="Resize the worker list"
          tabIndex={0} title="Drag to resize the worker list" onPointerDown={startResize} onKeyDown={nudge} />
      </>)}
      {workers && workers.length === 0 && (
        <div className="empty-hint" style={{ padding: "14px 20px" }}>
          No worker has reported in the last 2 minutes — queued runs will wait until one starts.
        </div>
      )}
      {fleet.data && !workers && (
        <div className="empty-hint" style={{ padding: "14px 20px" }}>{fleet.data.note}</div>
      )}
      </>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Definitions                                                         */
/* ------------------------------------------------------------------ */

type StageType = PipelineDefinitionStage["type"];

/** What each stage type actually does when the workflow engine runs it —
 *  shown as the badge tooltip, as in the mock. */
const STAGE_META: Record<StageType, { label: string; icon: ReactNode; does: string }> = {
  "dataset-validation": { label: "Dataset validation", does: "Real quality gate: leakage, imbalance, outliers, schema",
    icon: <><path d="M9 12l2 2 4-4" /><path d="M12 3l8 4v5c0 5-3.5 9-8 10-4.5-1-8-5-8-10V7l8-4z" /></> },
  preprocessing: { label: "Preprocessing", does: "Fitted ColumnTransformer — applied inside training and saved with the model",
    icon: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" /></> },
  training: { label: "Training", does: "Real training_runs row, real completeTrainingRun()",
    icon: <><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" /></> },
  hpo: { label: "HPO search", does: "Real fit per real trial, best real result kept",
    icon: <><path d="M4 21V14M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" /><path d="M1 14h6M9 8h6M17 12h6" /></> },
  evaluation: { label: "Evaluation", does: "Real measured metrics, surfaced not recomputed",
    icon: <path d="M18 20V10M12 20V4M6 20v-6" /> },
  register: { label: "Register", does: "Real, independent check the model exists in UCS",
    icon: <><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5" /><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3" /></> },
  deploy: { label: "Deploy", does: "Real serving-engine call, real readiness polling",
    icon: <path d="M12 2l3 7h7l-5.5 5 2 8-6.5-4.5L5.5 22l2-8L2 9h7z" /> },
  generic: { label: "Generic", does: "No real work defined for this stage yet",
    icon: <circle cx="12" cy="12" r="3" /> },
};
const STAGE_TYPES = Object.keys(STAGE_META) as StageType[];

function StageBadge({ stage }: { stage: PipelineDefinitionStage }) {
  const meta = STAGE_META[stage.type] ?? STAGE_META.generic;
  return (
    <span className="tag" style={{ display: "inline-flex", alignItems: "center", gap: 4 }} title={meta.does}>
      <svg className="ic" viewBox="0 0 24 24" style={{ width: 11, height: 11, flex: "none" }}>{meta.icon}</svg>
      {stage.title}
    </span>
  );
}

const StageChain = ({ stages }: { stages: PipelineDefinitionStage[] }) => (
  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
    {stages.map((s, i) => (
      <span key={i} style={{ display: "contents" }}>
        {i > 0 && <span style={{ color: "var(--text-faint)" }}>→</span>}
        <StageBadge stage={s} />
      </span>
    ))}
  </div>
);

function DefinitionsTab({
  definitions, query, runsByDefinition, onRan,
}: {
  definitions: ReturnType<typeof useApi<PipelineDefinition[]>>;
  query: string;
  runsByDefinition: Map<string, number>;
  onRan: (msg: string) => void;
}) {
  const { can } = usePermissions();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openVersions, setOpenVersions] = useState<Set<string>>(new Set());
  // null = closed; { base: undefined } = new; { base: def } = new version of def.
  const [editor, setEditor] = useState<{ base?: PipelineDefinition } | null>(null);

  // Newest version first, one group per name.
  const groups = useMemo(() => {
    const m = new Map<string, PipelineDefinition[]>();
    for (const d of definitions.data ?? []) m.set(d.name, [...(m.get(d.name) ?? []), d]);
    return [...m.values()].map((vs) => vs.sort((a, b) => b.version - a.version))
      .filter((vs) => matchesQuery(query, vs[0].name, vs[0].computeProfile, vs[0].createdBy, ...vs[0].stages.map((st) => `${st.title} ${st.type}`)));
  }, [definitions.data, query]);

  async function run(def: PipelineDefinition) {
    setBusy(def.id);
    setError(null);
    try {
      const res = await api.runPipelineDefinition(def.id);
      onRan(`Running ${def.name} v${def.version} — each stage does real work: validation re-profiles the data, training creates a real run.${(res as any).pipelineId ? ` Pipeline ${(res as any).pipelineId}.` : ""}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const toggle = (name: string) =>
    setOpenVersions((s) => { const n = new Set(s); n.has(name) ? n.delete(name) : n.add(name); return n; });

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 14 }}>
        <div className="hint" style={{ margin: 0 }}>
          Immutable, versioned pipeline definitions — what a pipeline actually <em>is</em>, separate from
          any one execution of it. Running a definition creates a new entry under Runs above; editing one
          creates a new version, never changes an existing one.
        </div>
        <button hidden={!can(PERM.train)} className="btn btn-primary btn-sm" style={{ flex: "none" }} onClick={() => setEditor({})}>
          <Icon name="plus" />New definition
        </button>
      </div>

      {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}
      {definitions.loading && <div className="empty-hint">Loading…</div>}
      {!definitions.loading && !groups.length && (
        <div className="card card-pad">
          <div className="empty-hint">
            {query.trim() && definitions.data?.length
              ? <>No pipeline definitions match “{query.trim()}”.</>
              : <>No pipeline definitions yet. Create one — e.g. Dataset validation → Training → Evaluation → Register
                — and run it whenever the data changes.</>}
          </div>
        </div>
      )}

      {groups.map((versions) => {
        const latest = versions[0];
        const older = versions.slice(1);
        const runCount = versions.reduce((n, v) => n + (runsByDefinition.get(v.id) ?? 0), 0);
        const open = openVersions.has(latest.name);
        return (
          <div className="card card-pad" style={{ marginBottom: 14 }} key={latest.name}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 700, fontSize: 14, fontFamily: "var(--font-mono)" }}>
                  {latest.name} <span className="tag">v{latest.version}</span>
                </div>
                <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 4 }}>
                  Created by {latest.createdBy} · {formatStarted(latest.createdAt)} · {latest.computeProfile} · run {runCount} time{runCount === 1 ? "" : "s"}
                </div>
                <div style={{ marginTop: 10 }}><StageChain stages={latest.stages} /></div>
              </div>
              <div style={{ display: "flex", gap: 6, flex: "none" }}>
                <button className="btn btn-ghost btn-sm" onClick={() => toggle(latest.name)}>
                  {older.length} older version{older.length === 1 ? "" : "s"}
                </button>
                <IconAction icon="pencil" label={`Edit definition ${latest.name}`} gated={PERM.train} onClick={() => setEditor({ base: latest })} />
                <button hidden={!can(PERM.train)} className="btn btn-primary btn-sm" disabled={busy === latest.id} onClick={() => run(latest)}>
                  <Icon name="play" />{busy === latest.id ? "Starting…" : `Run v${latest.version}`}
                </button>
              </div>
            </div>
            {open && (
              <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border)" }}>
                {older.length ? older.map((v) => (
                  <div key={v.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, padding: "6px 0" }}>
                    <div style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span className="tag">v{v.version}</span>
                      <span style={{ color: "var(--text-faint)" }}>{formatStarted(v.createdAt)}</span>
                      <StageChain stages={v.stages} />
                    </div>
                    <button hidden={!can(PERM.train)} className="btn btn-ghost btn-sm" disabled={busy === v.id} onClick={() => run(v)}>
                      {busy === v.id ? "Starting…" : `Run v${v.version}`}
                    </button>
                  </div>
                )) : <div className="hint">No earlier versions — v{latest.version} is the first.</div>}
              </div>
            )}
          </div>
        );
      })}

      {editor && (
        <DefinitionEditor
          base={editor.base}
          onClose={() => setEditor(null)}
          onSaved={(d) => { setEditor(null); definitions.reload(); setOpenVersions(new Set()); setError(null); void d; }}
        />
      )}
    </>
  );
}

const PROFILES = ["cpu-small", "cpu-large", "gpu-1x", "gpu-4x", "gpu-8x"];
const DEFAULT_STAGES: { title: string; type: StageType }[] = [
  { title: "Dataset Validation", type: "dataset-validation" },
  { title: "Training", type: "training" },
  { title: "Evaluation", type: "evaluation" },
  { title: "Register", type: "register" },
];

/**
 * Create a definition, or save an edit as its next version (the server never
 * edits an existing one). The dataset / base model / target are asked once and
 * written into the params of the stages that need them — a training stage
 * without a base model and dataset fails the moment it runs.
 */
function DefinitionEditor({
  base, onClose, onSaved,
}: { base?: PipelineDefinition; onClose: () => void; onSaved: (d: PipelineDefinition) => void }) {
  const { can } = usePermissions();
  const datasets = useApi(api.getDatasets, []);
  const baseModels = useApi(api.getBaseModels, []);
  const seed = (key: string) => {
    for (const s of base?.stages ?? []) { const v = (s.params as any)?.[key]; if (v) return String(v); }
    return "";
  };
  const [name, setName] = useState(base?.name ?? "");
  const [computeProfile, setComputeProfile] = useState(base?.computeProfile ?? "cpu-small");
  const [datasetName, setDatasetName] = useState(seed("datasetName"));
  const [baseModelId, setBaseModelId] = useState(seed("baseModelId"));
  const [targetColumn, setTargetColumn] = useState(seed("targetColumn"));
  const [modelName, setModelName] = useState(seed("modelName"));
  const [stages, setStages] = useState<{ title: string; type: StageType }[]>(
    base?.stages.map((s) => ({ title: s.title, type: s.type })) ?? DEFAULT_STAGES
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const has = (t: StageType) => stages.some((s) => s.type === t);
  const needsDataset = has("dataset-validation") || has("training") || has("hpo");
  const needsModel = has("training") || has("hpo");

  function validate(): string | null {
    if (!name.trim()) return "Give the definition a name.";
    if (!/^[a-z0-9][a-z0-9._-]*$/i.test(name.trim())) return "Use letters, numbers, dots, dashes or underscores in the name.";
    if (!stages.length) return "Add at least one stage.";
    if (stages.some((s) => !s.title.trim())) return "Every stage needs a title.";
    if (needsDataset && !datasetName) return "Pick the dataset — the validation and training stages run against it.";
    if (needsModel && !baseModelId) return "Pick the base model the training stage trains.";
    const order = (t: StageType) => stages.findIndex((s) => s.type === t);
    for (const t of ["evaluation", "register", "deploy"] as StageType[]) {
      if (has(t) && !(has("training") || has("hpo"))) return `A ${STAGE_META[t].label.toLowerCase()} stage needs a training stage before it.`;
      if (has(t) && order(t) < Math.max(order("training"), order("hpo"))) return `Move ${STAGE_META[t].label.toLowerCase()} after training — it uses what training produced.`;
    }
    if (has("deploy") && !has("register")) return "Add a register stage before deploy.";
    return null;
  }

  async function save() {
    const v = validate();
    if (v) return setError(v);
    setSaving(true);
    setError(null);
    const tgt = targetColumn.trim() || undefined;
    const built: PipelineDefinitionStage[] = stages.map((s) => {
      const params: Record<string, unknown> =
        s.type === "dataset-validation" ? { datasetName, ...(tgt ? { targetColumn: tgt } : {}) }
        : s.type === "training" ? { baseModelId, datasetName, computeProfile, modelName: modelName.trim() || name.trim(), ...(tgt ? { targetColumn: tgt } : {}) }
        : s.type === "hpo" ? { baseModelId, datasetName, computeProfile, ...(tgt ? { targetColumn: tgt } : {}) }
        : {};
      return { title: s.title.trim(), type: s.type, ...(Object.keys(params).length ? { params } : {}) };
    });
    try {
      const d = await api.createPipelineDefinition({ name: name.trim(), computeProfile, stages: built });
      onSaved(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const move = (i: number, dir: -1 | 1) => setStages((s) => {
    const j = i + dir; if (j < 0 || j >= s.length) return s;
    const n = [...s]; [n[i], n[j]] = [n[j], n[i]]; return n;
  });

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer drawer-md">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>
              {base ? `New version of ${base.name}` : "New pipeline definition"}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3 }}>
              {base ? `Saves as v${base.version + 1} — v${base.version} stays exactly as it is.` : "Saved as v1. Later edits become new versions."}
            </div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

          <div className="drawer-section">
            <div className="field">
              <label>Name</label>
              <input className="input" value={name} disabled={!!base} placeholder="e.g. churn-predictor-weekly"
                onChange={(e) => setName(e.target.value)} style={{ fontFamily: "var(--font-mono)" }} />
            </div>
            <div className="field">
              <label id="pd-profile-label">Compute profile</label>
              <SearchSelect labelledBy="pd-profile-label" noun="profile" value={computeProfile}
                options={PROFILES.map((p) => ({ value: p, label: p }))} onChange={setComputeProfile} />
            </div>
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">What it runs on</div>
            <div className="field">
              <label id="pd-dataset-label">Dataset {needsDataset ? "" : <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(not needed by these stages)</span>}</label>
              <SearchSelect labelledBy="pd-dataset-label" noun="dataset" placeholder="Select…" value={datasetName}
                options={(datasets.data ?? []).filter((d) => d.structure_type !== "unstructured").map((d) => ({ value: d.name, label: d.name, hint: d.rows ? `${d.rows} rows` : undefined }))}
                onChange={setDatasetName} />
            </div>
            <div className="field">
              <label id="pd-model-label">Base model {needsModel ? "" : <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(no training stage)</span>}</label>
              <SearchSelect labelledBy="pd-model-label" noun="model" placeholder="Select…" value={baseModelId}
                options={(baseModels.data ?? []).filter((b) => b.domain !== "llm").map((b) => ({ value: b.id, label: b.id, hint: b.task }))}
                onChange={setBaseModelId} />
              <div className="hint">LLM fine-tunes need text-column settings — launch those from the Train wizard.</div>
            </div>
            <div className="field-row">
              <div className="field">
                <label>Target column <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(optional)</span></label>
                <input className="input" value={targetColumn} placeholder="e.g. churned" onChange={(e) => setTargetColumn(e.target.value)} />
              </div>
              <div className="field">
                <label>Registered model name <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(optional)</span></label>
                <input className="input" value={modelName} placeholder={name.trim() || "defaults to the definition name"} onChange={(e) => setModelName(e.target.value)} />
                <div className="hint">Each run registers the next version of this model.</div>
              </div>
            </div>
          </div>

          <div className="drawer-section">
            <div className="drawer-section-title">Stages</div>
            {stages.map((s, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 8, alignItems: "center", marginBottom: 8 }}>
                <input className="input" value={s.title} aria-label={`Stage ${i + 1} title`}
                  onChange={(e) => setStages((st) => st.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} />
                <SearchSelect ariaLabel={`Stage ${i + 1} type`} noun="type" value={s.type}
                  options={STAGE_TYPES.map((t) => ({ value: t, label: STAGE_META[t].label }))}
                  onChange={(v) => setStages((st) => st.map((x, j) => (j === i ? { type: v as StageType, title: x.title === STAGE_META[x.type].label || !x.title ? STAGE_META[v as StageType].label : x.title } : x)))} />
                <div style={{ display: "flex", gap: 2 }}>
                  <IconAction icon="arrow-up" label={`Move stage ${i + 1} up`} align="end" disabled={i === 0} onClick={() => move(i, -1)} />
                  <IconAction icon="arrow-down" label={`Move stage ${i + 1} down`} align="end" disabled={i === stages.length - 1} onClick={() => move(i, 1)} />
                  <IconAction icon="trash" label={`Remove stage ${i + 1}${s.title ? ` (${s.title})` : ""}`} danger align="end" onClick={() => setStages((st) => st.filter((_, j) => j !== i))} />
                </div>
              </div>
            ))}
            <button className="btn btn-ghost btn-sm" onClick={() => setStages((st) => [...st, { title: "Generic", type: "generic" }])}>
              <Icon name="plus" />Add stage
            </button>
            {stages.length > 0 && <div style={{ marginTop: 12 }}><StageChain stages={stages} /></div>}
            {has("hpo") && <div className="hint" style={{ marginTop: 8 }}>An HPO stage without declared trials is skipped — run full searches from Experiments.</div>}
          </div>

          <button hidden={!can(PERM.train)} className="btn btn-primary btn-block" disabled={saving} onClick={save}>
            <Icon name="check" />{saving ? "Saving…" : base ? `Save as v${base.version + 1}` : "Create definition"}
          </button>
        </div>
      </div>
    </div>
  );
}
