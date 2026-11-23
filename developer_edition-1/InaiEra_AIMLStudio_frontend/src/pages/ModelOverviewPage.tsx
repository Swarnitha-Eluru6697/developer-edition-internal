import { ArtifactLocation } from "../components/ArtifactLocation";
import { useState, ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import { StatusPill } from "../components/StatusPill";
import { TrendChart } from "../components/TrendChart";
import { domainBadge } from "../components/modelBadges";
import type { ModelOverviewCard } from "../types/domain";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";

/** Local calendar day as YYYY-MM-DD — what a date input reads and writes. */
const isoDay = (d: Date) => d.toLocaleDateString("en-CA");
const daysAgo = (n: number) => new Date(Date.now() - n * 86400000);

// ---------------------------------------------------------------------------
// Production metrics measured from labelled prediction traces (prediction vs.
// submitted ground truth). Rates are returned 0–1; losses/errors raw.
// ---------------------------------------------------------------------------

type PerfRow = { label: string; key: string; rate?: boolean; lowerIsBetter?: boolean; suffix?: string };

const predOf = (t: any) => (typeof t.prediction === "object" && t.prediction ? t.prediction.prediction : t.prediction);

function classMetrics(b: any[]) {
  const pred = b.map((t) => String(predOf(t)));
  const truth = b.map((t) => String(t.groundTruth));
  const labels = [...new Set([...truth, ...pred])].sort();
  const n = b.length;
  const accuracy = pred.filter((p, i) => p === truth[i]).length / n;
  const prf = (l: string) => {
    const tp = pred.filter((p, i) => p === l && truth[i] === l).length;
    const fp = pred.filter((p, i) => p === l && truth[i] !== l).length;
    const fn = pred.filter((p, i) => p !== l && truth[i] === l).length;
    const precision = tp + fp ? tp / (tp + fp) : 0;
    const recall = tp + fn ? tp / (tp + fn) : 0;
    return { precision, recall, f1: precision + recall ? (2 * precision * recall) / (precision + recall) : 0 };
  };
  const binary = labels.length === 2;
  const pos = binary ? (labels.includes("1") ? "1" : labels.includes("true") ? "true" : labels[1]) : null;
  // Support-weighted across classes — the same averaging the trainer used
  // for the baseline, so the two columns compare like with like.
  const avg = (() => {
    const all = labels.map((l) => ({ ...prf(l), w: truth.filter((y) => y === l).length / n }));
    const m = (k: "precision" | "recall" | "f1") => all.reduce((s, x) => s + x[k] * x.w, 0);
    return { precision: m("precision"), recall: m("recall"), f1: m("f1") };
  })();

  // Probability of the positive class, from the confidence the endpoint
  // returned for its predicted class.
  const conf = b.map((t) => (typeof t.confidence === "number" ? t.confidence : null));
  const scored = binary && conf.every((c) => c !== null)
    ? b.map((_, i) => ({ p: pred[i] === pos ? conf[i]! : 1 - conf[i]!, y: truth[i] === pos ? 1 : 0 }))
    : null;
  let rocAuc: number | null = null, prAuc: number | null = null, logLoss: number | null = null, brier: number | null = null;
  if (scored) {
    const P = scored.filter((s) => s.y === 1).length, N = scored.length - P;
    if (P && N) {
      // Mann–Whitney U with average ranks for ties.
      const sorted = scored.map((s, i) => ({ ...s, i })).sort((a, c) => a.p - c.p);
      const ranks = new Array(sorted.length);
      for (let i = 0; i < sorted.length; ) {
        let j = i;
        while (j + 1 < sorted.length && sorted[j + 1].p === sorted[i].p) j++;
        for (let k = i; k <= j; k++) ranks[k] = (i + j) / 2 + 1;
        i = j + 1;
      }
      const rankSum = sorted.reduce((s, x, k) => s + (x.y ? ranks[k] : 0), 0);
      rocAuc = (rankSum - (P * (P + 1)) / 2) / (P * N);
      // Average precision over the score ranking.
      const desc = scored.slice().sort((a, c) => c.p - a.p);
      let tp = 0, ap = 0;
      desc.forEach((s, k) => { if (s.y) { tp++; ap += tp / (k + 1); } });
      prAuc = ap / P;
    }
    const eps = 1e-15;
    logLoss = -scored.reduce((s, x) => s + (x.y ? Math.log(Math.max(eps, x.p)) : Math.log(Math.max(eps, 1 - x.p))), 0) / scored.length;
    brier = scored.reduce((s, x) => s + (x.p - x.y) ** 2, 0) / scored.length;
  }
  // Expected calibration error: top-label confidence vs. correctness, 10 bins.
  const top = b.map((t, i) => ({ c: conf[i], ok: pred[i] === truth[i] })).filter((x) => x.c !== null) as { c: number; ok: boolean }[];
  const ece = top.length
    ? Array.from({ length: 10 }, (_, k) => top.filter((x) => Math.min(9, Math.floor(x.c * 10)) === k))
        .filter((bin) => bin.length)
        .reduce((s, bin) => s + (Math.abs(bin.filter((x) => x.ok).length / bin.length - bin.reduce((c, x) => c + x.c, 0) / bin.length) * bin.length) / top.length, 0)
    : null;
  if (brier === null && top.length) brier = top.reduce((s, x) => s + (x.c - (x.ok ? 1 : 0)) ** 2, 0) / top.length;
  return { accuracy, ...avg, rocAuc, prAuc, logLoss, brier, ece };
}

function regMetrics(b: any[]) {
  const pairs = b.map((t) => [Number(predOf(t)), Number(t.groundTruth)]).filter(([p, y]) => Number.isFinite(p) && Number.isFinite(y));
  if (!pairs.length) return { mae: null, rmse: null, r2: null, mape: null, smape: null, medianAE: null };
  const err = pairs.map(([p, y]) => p - y);
  const abs = err.map(Math.abs);
  const mean = pairs.reduce((s, [, y]) => s + y, 0) / pairs.length;
  const ssTot = pairs.reduce((s, [, y]) => s + (y - mean) ** 2, 0);
  const nz = pairs.filter(([, y]) => y !== 0);
  const sortedAbs = abs.slice().sort((a, c) => a - c);
  const mid = Math.floor(sortedAbs.length / 2);
  return {
    mae: abs.reduce((s, x) => s + x, 0) / abs.length,
    rmse: Math.sqrt(err.reduce((s, x) => s + x * x, 0) / err.length),
    r2: ssTot ? 1 - err.reduce((s, x) => s + x * x, 0) / ssTot : null,
    mape: nz.length ? (nz.reduce((s, [p, y]) => s + Math.abs(p - y) / Math.abs(y), 0) / nz.length) * 100 : null,
    smape: (pairs.reduce((s, [p, y]) => s + (Math.abs(p) + Math.abs(y) ? (2 * Math.abs(p - y)) / (Math.abs(p) + Math.abs(y)) : 0), 0) / pairs.length) * 100,
    medianAE: sortedAbs.length % 2 ? sortedAbs[mid] : (sortedAbs[mid - 1] + sortedAbs[mid]) / 2,
  };
}

function confusion(b: any[]) {
  const labels = [...new Set(b.flatMap((t) => [String(t.groundTruth), String(predOf(t))]))].sort();
  const matrix = labels.map(() => labels.map(() => 0));
  for (const t of b) matrix[labels.indexOf(String(t.groundTruth))][labels.indexOf(String(predOf(t)))]++;
  return { labels, matrix };
}

/** Full-width area trend with min/max on the y axis and first/last on the x axis. */
function AreaTrend({ points, suffix = "", xStart, xEnd, caption }: {
  points: Array<{ t: string; value: number }>; suffix?: string; xStart: string; xEnd: string; caption?: string;
}) {
  const W = 400, H = 90, top = 8, bottom = 82;
  const vals = points.map((p) => p.value);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const xy = vals.map((v, i) => [(i / (vals.length - 1)) * W, top + (1 - (v - min) / span) * (bottom - top)]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const fmt = (v: number) => `${v.toLocaleString(undefined, { maximumFractionDigits: suffix === "%" ? 1 : Math.abs(v) >= 100 ? 0 : 3 })}${suffix}`;
  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "56px 1fr", columnGap: 8 }}>
        <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", fontSize: 11, color: "var(--text-faint)", textAlign: "right", height: H }}>
          <span>{fmt(max)}</span><span>{fmt(min)}</span>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: H, display: "block" }}>
          <line x1="0" x2={W} y1={top} y2={top} stroke="var(--border)" strokeDasharray="3 3" />
          <polygon points={`0,${bottom} ${line} ${W},${bottom}`} fill="var(--brand-strong)" opacity={0.12} />
          <polyline points={line} fill="none" stroke="var(--brand-strong)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        </svg>
        <div />
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
          <span>{xStart}</span><span>{xEnd}</span>
        </div>
      </div>
      {caption && <div style={{ fontSize: 10.5, color: "var(--text-faint)", marginTop: 4, textAlign: "right" }}>{caption}</div>}
    </div>
  );
}

/** One LLM stat tab: the mockup's navy callout over a grid of metric tiles.
 *  A payload that's only a {note} (nothing measured yet) shows the note. */
function LlmStatTab({ callout, data }: { callout: string; data: Record<string, unknown> | null | undefined }) {
  const entries = Object.entries(data ?? {}).filter(([k]) => k !== "note");
  return (
    <div>
      <div className="callout callout-navy" style={{ marginBottom: 18 }}>{callout}</div>
      {entries.length ? (
        <div className="metric-grid">
          {entries.map(([k, v]) => (
            <div className="metric-mini" key={k}>
              <div className="m-label">{/[A-Z ]/.test(k) ? k : humanizeKey(k)}</div>
              <div className="m-val" style={{ fontSize: 16 }}>{v === null || v === undefined ? "—" : scalarText(v)}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="card card-pad"><div className="hint" style={{ marginBottom: 0 }}>{String((data as any)?.note ?? "Not measured for this model yet.")}</div></div>
      )}
    </div>
  );
}

/** Tile-sized area sparkline with the series' peak marked. Only drawn from two
 *  or more real points — one measurement isn't a trend. */
function MiniSpark({ points, suffix = "", caption }: { points: Array<{ t: string; value: number }>; suffix?: string; caption?: string }) {
  const W = 300, H = 44, top = 6;
  const vals = points.map((p) => p.value);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const xy = vals.map((v, i) => [(i / (vals.length - 1)) * W, top + (1 - (v - min) / span) * (H - top - 2)]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const peak = max.toLocaleString(undefined, { maximumFractionDigits: suffix === "%" ? 1 : max >= 100 ? 0 : 3 });
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <span style={{ fontSize: 10.5, color: "var(--text-faint)", flex: "none" }}>{peak}{suffix}</span>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: H, display: "block" }}>
          <line x1="0" x2={W} y1={top} y2={top} stroke="var(--border)" strokeDasharray="3 3" />
          <polygon points={`0,${H} ${line} ${W},${H}`} fill="var(--brand-strong)" opacity={0.12} />
          <polyline points={line} fill="none" stroke="var(--brand-strong)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        </svg>
      </div>
      {caption && <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 2, textAlign: "right" }}>{caption}</div>}
    </div>
  );
}

/** Display names for modelHealth.ts's component keys. */
const HEALTH_LABELS: Record<string, string> = {
  performance: "Performance",
  drift: "Drift (PSI)",
  dataQuality: "Data quality",
  serving: "Serving",
  infrastructure: "Infrastructure",
};

const UCS_ENABLED = (import.meta as { env?: Record<string, string | undefined> }).env?.VITE_UCS_ENABLED !== "false";
const CLASSIC_TABS = ["Overview", "Performance", "Drift", "Predictions", "Explainability", "Infrastructure", "Alerts", "Lineage", "Model Details"] as const;
// A classification/regression model trained AS a deep net (framework
// pytorch) still has accuracy/F1/confusion-matrix like any classical ML
// model — but ALSO has training dynamics (epochs, loss curves, gradient
// norm) that xgboost genuinely has no equivalent of. One extra tab, not a
// wholesale different set like LLM gets.
const DL_TABS = ["Overview", "Performance", "Training Metrics", "Input Quality", "Drift", "Predictions", "Explainability", "Infrastructure", "Alerts", "Lineage", "Model Details"] as const;
// LLM models get a distinct tab set instead of reusing classification's —
// explainability and confusion-matrix-style performance don't map onto
// generated text, and there are real categories a classifier has no
// equivalent of at all (hallucination/groundedness, safety, cost, LLM-
// specific drift, fine-tuning).
const LLM_TABS = ["Overview", "Response Quality", "Hallucination & Groundedness", "Latency & Serving", "Infrastructure", "Cost", "Safety", "Drift", "Fine-tuning", "Predictions", "Alerts", "Lineage", "Model Details"] as const;

// Latency/Serving and Infrastructure are two distinct categories in the
// monitoring taxonomy this is built from, but share one JSONB column in
// the DB (both describe the same running deployment) — split by key here
// rather than duplicating the whole object under both tabs.
const LATENCY_KEYS = new Set(["p50Latency", "p95Latency", "p99Latency", "p50Ttft", "p95Ttft", "outputThroughput", "queueTime", "tokensPerSec"]);

/** Turns `p95Latency` into `P95 latency` — these keys come straight from the
 *  monitoring JSON, whose shape differs per model type, so they're formatted
 *  generically rather than mapped by a fixed label table. */
function humanizeKey(k: string) {
  const spaced = k
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    // A trailing time window reads as a window, not as part of the word:
    // `predictions1h` -> `predictions (1h)`, matching how the mock labels
    // every other windowed metric.
    .replace(/([a-z])(\d+[hdm])$/i, "$1 ($2)")
    .replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A scalar cell value. Objects are NOT stringified into "[object Object]" —
 *  a nested object is rendered as its own block below instead. */
function scalarText(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function KvGrid({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  const scalars = entries.filter(([, v]) => v === null || typeof v !== "object");
  const objects = entries.filter(([, v]) => v !== null && typeof v === "object");
  return (
    <>
      {scalars.length > 0 && (
        <div className="metric-grid">
          {scalars.map(([k, v]) => (
            <div className="metric-mini" key={k}>
              <div className="m-label">{humanizeKey(k)}</div>
              <div className="m-val">{scalarText(v)}</div>
            </div>
          ))}
        </div>
      )}
      {objects.map(([k, v]) => (
        <div className="card card-pad" style={{ marginBottom: 16 }} key={k}>
          <div className="drawer-section-title">{humanizeKey(k)}</div>
          {Array.isArray(v) ? (
            v.length === 0 ? (
              <div className="empty-hint">Nothing recorded.</div>
            ) : (
              <div className="step-log" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(v, null, 2)}</div>
            )
          ) : (
            Object.entries(v as Record<string, unknown>).map(([ik, iv]) =>
              // A nested object here is a real sub-structure — the health
              // components are {score, reason, weight, available} each — so it
              // gets its own rows rather than being flattened into raw JSON.
              iv !== null && typeof iv === "object" && !Array.isArray(iv) ? (
                <div key={ik} style={{ margin: "10px 0 4px" }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: "var(--navy)", marginBottom: 4 }}>{humanizeKey(ik)}</div>
                  {Object.entries(iv as Record<string, unknown>).map(([jk, jv]) => (
                    <div className="kv-row" key={jk}>
                      <span className="k" style={{ paddingLeft: 12 }}>{humanizeKey(jk)}</span>
                      <span className="v" style={{ maxWidth: "70%", textAlign: "right" }}>
                        {jv !== null && typeof jv === "object" ? JSON.stringify(jv) : scalarText(jv)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="kv-row" key={ik}>
                  <span className="k">{humanizeKey(ik)}</span>
                  <span className="v" style={{ maxWidth: "70%", textAlign: "right" }}>
                    {Array.isArray(iv) ? (iv.length ? iv.join(", ") : "none") : scalarText(iv)}
                  </span>
                </div>
              )
            )
          )}
        </div>
      ))}
    </>
  );
}

// Same lightweight inline-SVG-polyline approach as the mockup's mmMiniLine
// — no charting library, just enough to show a real trend shape from a
// real array of values.
function LineChart({ values, color = "var(--teal)" }: { values: number[]; color?: string }) {
  if (!values || values.length < 2) return null;
  const w = 420, h = 92, pad = 8;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const points = values.map((v, i) => `${pad + (i * (w - pad * 2)) / (values.length - 1)},${h - pad - ((v - min) * (h - pad * 2)) / range}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: 92, display: "block", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8 }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function MetricMini({ label, value, sub }: { label: string; value: ReactNode; sub?: string }) {
  return (
    <div className="metric-mini">
      <div className="m-label">{label}</div>
      <div className="m-val">{value}</div>
      {sub && <div className="m-sub">{sub}</div>}
    </div>
  );
}


/** The landing grid. Mirrors the mock's card: title, type badge,
 *  `v4 · Champion · KServe` subtitle, big health score, status pill.
 *
 *  The one deliberate divergence from the mock is the score itself. The mock
 *  has a hardcoded number for every model; here a model that has not served
 *  measured traffic reports `insufficient-data`, and the card shows "N/A"
 *  rather than inventing a figure or printing a 0 that reads as "scored
 *  zero". Colour follows the mock's thresholds (>=85 pass, >=70 watch).
 */
function ModelCard({ card, onOpen }: { card: ModelOverviewCard; onOpen: () => void }) {
  const scored = card.healthScore !== null && card.status !== "insufficient-data";
  const scoreColor = !scored
    ? "var(--text-faint)"
    : card.healthScore! >= 85
    ? "var(--log-pass)"
    : card.healthScore! >= 70
    ? "var(--amber-700)"
    : "var(--log-fail)";
  const label = card.category.charAt(0).toUpperCase() + card.category.slice(1);
  return (
    <div className="opt-card" onClick={onOpen} role="button" tabIndex={0}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <div className="opt-card-head">
        <span className="opt-card-title" style={{ fontFamily: "var(--font-mono)", wordBreak: "break-word" }}>{card.modelId}</span>
        <span className={"badge " + (card.domain === "llm" ? "badge-llm" : "badge-ml")} style={{ flexShrink: 0 }}>{label}</span>
      </div>
      <div className="opt-card-desc">
        {[
          card.version != null ? `v${card.version}` : null,
          // "champion" is stored lowercase; the card presents it as a label.
          card.alias ? card.alias.charAt(0).toUpperCase() + card.alias.slice(1) : null,
          // A model with no deployment row has no runtime to name — say so
          // rather than leaving the line silently short.
          card.runtime || (card.deployed ? null : "not deployed"),
        ].filter(Boolean).join(" · ")}
      </div>
      <div className="dataset-card-stats" style={{ marginTop: 12 }}>
        <div className="dataset-card-stat">
          <div className="n" style={{ color: scoreColor, fontSize: 20 }}>{scored ? card.healthScore : "N/A"}</div>
          <div className="l">Health score</div>
        </div>
        <div className="dataset-card-stat" style={{ textAlign: "right" }}>
          <div className="n">
            <StatusPill status={scored ? card.status : "queued"} label={card.status.replace(/-/g, " ")} compact />
          </div>
          <div className="l">Status</div>
        </div>
      </div>
    </div>
  );
}

export default function ModelOverviewPage() {
  const navigate = useNavigate();
  const cards = useApi(api.getModelsOverview, []);
  const [query] = usePageSearch("Search models by name, task, status…");
  const shownCards = (cards.data ?? []).filter((c) =>
    matchesQuery(query, c.modelId, c.task, c.category, c.domain, c.framework, c.alias, c.deployed ? "deployed" : "not deployed", c.status.replace(/-/g, " ")));
  // `?model=` opens straight onto one model, so "Open model" from a training
  // run, a pipeline or the catalog lands on that model rather than on the grid
  // with the user having to find it again.
  const [searchParams, setSearchParams] = useSearchParams();
  const [modelId, setModelId] = useState<string | null>(searchParams.get("model"));
  const [tab, setTab] = useState<string>("Overview");
  const monitoring = useApi(() => (modelId ? api.getModelMonitoring(modelId) : Promise.resolve(null)), [modelId]);
  const activeAlerts = useApi(api.getActiveAlerts, []);
  const model = useApi(() => (modelId ? api.getModel(modelId) : Promise.resolve(null)), [modelId]);
  const governance = useApi(() => (modelId && tab === "Lineage" ? api.getModelGovernance(modelId) : Promise.resolve(null)), [modelId, tab]);
  const explainability = useApi(() => (modelId && tab === "Explainability" ? api.getModelExplainability(modelId) : Promise.resolve(null)), [modelId, tab]);
  // The Alerts tab reads the same policy/channel/history state the full Alerts
  // page does, so the two can never disagree about this model.
  const alertPolicies = useApi(() => (tab === "Alerts" ? api.getAlertPolicies() : Promise.resolve(null)), [tab]);
  const alertChannels = useApi(() => (tab === "Alerts" ? api.getNotificationChannels() : Promise.resolve(null)), [tab]);
  const alertHistory = useApi(() => (tab === "Alerts" ? api.getAlertHistory() : Promise.resolve(null)), [tab]);
  // The trend window. 7/14/30 count back from today; "custom" uses the From/To
  // inputs. It drives the Performance and Drift trend charts and the
  // Predictions table — the live snapshots (Overview, Infrastructure) ignore it.
  const [range, setRange] = useState<7 | 14 | 30 | "custom">(30);
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const today = isoDay(new Date());
  let fromDate = range === "custom" ? customFrom || isoDay(daysAgo(29)) : isoDay(daysAgo(range - 1));
  let toDate = range === "custom" ? customTo || today : today;
  // A backwards range would silently show nothing — swap it instead.
  if (fromDate > toDate) [fromDate, toDate] = [toDate, fromDate];
  const fetchDays = Math.min(365, Math.max(30, Math.ceil((Date.now() - new Date(fromDate + "T00:00:00").getTime()) / 86400000) + 1));
  const inRange = (t: string | undefined | null) => !!t && t.slice(0, 10) >= fromDate && t.slice(0, 10) <= toDate;
  // Real dated series, for the over-time charts and the range summary line.
  const series = useApi(
    () => (modelId ? api.getModelTimeseries(modelId, fetchDays) : Promise.resolve(null)),
    [modelId, fetchDays]
  );
  const baseModel = useApi(
    () => (tab === "Model Details" && model.data?.base_ref ? api.getBaseModel(model.data.base_ref).catch(() => null) : Promise.resolve(null)),
    [tab, model.data]
  );
  // Validation results on Model Details come from the training run that
  // produced the live version.
  const runDetail = useApi(() => {
    const champion: any = model.data?.versions.find((v) => v.alias === "champion") ?? model.data?.versions[0];
    const runId = champion?.reproducibility?.trainingRunId;
    return tab === "Model Details" && runId ? api.getTrainingRunDetail(String(runId)) : Promise.resolve(null);
  }, [tab, model.data]);

  const isLlm = monitoring.data?.modelType === "generation";
  // Deep learning can be backed by any supported neural framework, not just pytorch.
  const isDeepLearning = !isLlm && /(pytorch|torch|tensorflow|keras|jax)/i.test(model.data?.framework || "");
  // Lineage is read from the Unified Catalog; a build without one (VITE_UCS_ENABLED=false) doesn't offer it.
  const tabs = (isLlm ? LLM_TABS : isDeepLearning ? DL_TABS : CLASSIC_TABS)
    .filter((t) => UCS_ENABLED || t !== "Lineage") as readonly (typeof CLASSIC_TABS[number] | typeof DL_TABS[number] | typeof LLM_TABS[number])[];

  function selectModel(id: string) {
    setModelId(id);
    setTab("Overview");
    setRange(30);
    setSearchParams({ model: id }, { replace: true });
  }

  function clearModel() {
    setModelId(null);
    setSearchParams({}, { replace: true });
  }

  // Landing view: the card grid. The detail is only mounted once a card is
  // picked, so the per-model monitoring fetch doesn't fire for a model the
  // user hasn't asked for.
  if (modelId === null) {
    return (
      <div>
        <div className="page-fixed">
          <div className="page-head">
            <div>
              <h1>Model overview</h1>
              <p>
                Full observability per model — performance, drift, predictions, explainability, and the
                infrastructure serving it.
              </p>
            </div>
          </div>
        </div>
        {cards.loading && <div className="empty-hint">Loading models…</div>}
        {cards.error && <div className="callout callout-amber">Could not load models: {cards.error}</div>}
        {cards.data && cards.data.length === 0 && (
          <div className="empty-hint">No models in the catalog yet. Train one from the Train tab.</div>
        )}
        {cards.data && cards.data.length > 0 && shownCards.length === 0 && (
          <div className="empty-hint">No models match “{query.trim()}”.</div>
        )}
        {shownCards.length > 0 && (
          <div className="opt-grid">
            {shownCards.map((c) => (
              <ModelCard key={c.modelId} card={c} onOpen={() => selectModel(c.modelId)} />
            ))}
          </div>
        )}
      </div>
    );
  }

  const activeCard = cards.data?.find((c) => c.modelId === modelId) || null;

  // "Showing N days of data" — counted from the days that actually have
  // traffic, so a quiet week says so instead of implying a full series.
  const allDays = [...new Set((series.data?.serving.volume ?? []).map((p) => p.t.slice(0, 10)))].sort();
  const shownDays = allDays.filter((d) => d >= fromDate && d <= toDate);
  const fmtDay = (d: string) => new Date(d + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const rangeHint = series.loading
    ? "Loading measured history…"
    : !allDays.length
    ? "No dated traffic for this model yet — trends appear once its endpoint serves real predictions."
    : !shownDays.length
    ? `No data in that range — try widening it. Data is available from ${fmtDay(allDays[0])} to ${fmtDay(allDays[allDays.length - 1])}.`
    : `Showing ${shownDays.length} day${shownDays.length === 1 ? "" : "s"} of data · ${fmtDay(shownDays[0])} – ${fmtDay(shownDays[shownDays.length - 1])}`;
  const byRange = <T extends { t: string }>(pts: T[] | undefined) => (pts ?? []).filter((p) => inRange(p.t));

  return (
    <div>
      <div className="page-fixed">
        <button className="btn btn-ghost btn-sm" style={{ marginBottom: 14 }} onClick={clearModel}>
          <Icon name="arrow-left" /> Back to Model Overview
        </button>

        <div className="card card-pad" style={{ marginBottom: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 2 }}>Model</div>
              <div style={{ fontWeight: 700, fontSize: 20, fontFamily: "var(--font-mono)" }}>{modelId}</div>
            </div>
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 2 }}>Version · Environment</div>
              <div style={{ fontWeight: 700, fontFamily: "var(--font-mono)" }}>
                {activeCard
                  ? [activeCard.version != null ? `v${activeCard.version}` : "—", activeCard.runtime || "not deployed"].join(" · ")
                  : "—"}
              </div>
            </div>
          </div>
        </div>

        <div className="card card-pad" style={{ marginBottom: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 12 }}>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>From</label>
                <input className="input" type="date" value={fromDate} max={today} style={{ padding: "6px 8px" }}
                  onChange={(e) => { setCustomFrom(e.target.value); setCustomTo(toDate); setRange("custom"); }} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>To</label>
                <input className="input" type="date" value={toDate} max={today} style={{ padding: "6px 8px" }}
                  onChange={(e) => { setCustomTo(e.target.value); setCustomFrom(fromDate); setRange("custom"); }} />
              </div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              {([7, 14, 30] as const).map((d) => (
                <button key={d} className={"btn btn-sm " + (range === d ? "btn-primary" : "btn-ghost")} onClick={() => setRange(d)}>{d}D</button>
              ))}
            </div>
          </div>
          <p className="hint" style={{ margin: "8px 0 0" }}>{rangeHint}</p>
        </div>

        <div className="tabs">
          {tabs.map((t) => (
            <button key={t} className={"tab" + (tab === t ? " active" : "")} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
      {monitoring.loading && <div className="empty-hint">Loading…</div>}
      {monitoring.error && (
        <div className="callout callout-amber">
          No monitoring record for this model: {monitoring.error}. A model accumulates one once it is
          promoted and starts serving real predictions.
        </div>
      )}

      {monitoring.data && tab === "Overview" && (() => {
        const ov = monitoring.data!.overview as any;
        const health = ov?.health;
        const insufficient = !health || health.status === "insufficient-data";
        const score = health?.score ?? monitoring.data!.healthScore;
        const scoreColor = insufficient ? "var(--text-faint)" : score >= 85 ? "var(--log-pass)" : score >= 70 ? "var(--amber-700)" : "var(--log-fail)";
        const featureDrift: any[] = Array.isArray((monitoring.data!.drift as any)?.featureDrift) ? (monitoring.data!.drift as any).featureDrift : [];
        const drifted = featureDrift.filter((f: any) => f.status === "watch" || f.status === "critical");
        const worstDrift = featureDrift.slice().sort((a, b) => (b.psi ?? 0) - (a.psi ?? 0))[0];
        // Fixed reading order (the mock's), whatever order the backend sent.
        const ORDER = ["performance", "drift", "dataQuality", "serving", "infrastructure"];
        const components: Record<string, any> = Object.fromEntries(
          Object.entries(health?.components ?? {}).sort(([a], [b]) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99))
        );
        // Long floats from the scorer ("f1=0.9048855103365161") read as noise;
        // three decimals carries the same information. The drift reason also
        // names the feature behind the max PSI.
        const fmtReason = (key: string, reason: unknown) => {
          let r = String(reason ?? "").replace(/-?\d+\.\d{4,}/g, (m) => String(+Number(m).toFixed(3)));
          if (key === "drift" && worstDrift?.feature && !r.includes(worstDrift.feature)) r += ` (${worstDrift.feature})`;
          return r;
        };

        // The one-line explanation under the bar. Names the component actually
        // dragging the score down rather than a generic phrase, so it says
        // something a real incident review could act on.
        const scored = Object.entries(components).filter(([, c]) => c?.available && typeof c.score === "number");
        const worst = scored.sort((a, b) => a[1].score - b[1].score)[0];
        const note = insufficient
          ? "Not enough measured production signal to score this model yet. It starts accumulating once real prediction traffic reaches its endpoint."
          : drifted.length
          ? `Pulled down by feature drift on ${drifted.map((f: any) => f.feature).join(", ")}.`
          : worst && worst[1].score < 85
          ? `Lowest component is ${HEALTH_LABELS[worst[0]] ?? worst[0]} at ${Math.round(worst[1].score)} — ${worst[1].reason}.`
          : "No component is currently pulling this score down.";

        // Accuracy and F1/R² tiles: the measured production value (from
        // ground-truth feedback) against the training baseline, plus its daily
        // trend. Falls back to the training value, labelled as such, when no
        // production label exists for that metric.
        const perf = monitoring.data!.performance as any;
        type Tile = {
          label: string; value: unknown; note?: string; delta?: number | null; deltaUnit?: string; lowerIsBetter?: boolean;
          spark?: Array<{ t: string; value: number }>; sparkNote?: string; sparkSuffix?: string;
        };
        const toPct = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? (v > 1.5 ? v : v * 100) : null);
        // Trend for a tile: the daily series once there are two or more days;
        // until then, the same metric over consecutive batches of labelled
        // production predictions in time order — real measurements, just at a
        // finer grain than a day.
        const labelledTraces = (((monitoring.data!.predictions as any)?.traces ?? []) as any[])
          .filter((t) => t.groundTruth !== null && t.groundTruth !== undefined)
          .sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
        const predOf = (t: any) => (typeof t.prediction === "object" && t.prediction ? t.prediction.prediction : t.prediction);
        const batchSize = Math.max(10, Math.ceil(labelledTraces.length / 10));
        const batches: any[][] = [];
        for (let i = 0; i + batchSize <= labelledTraces.length; i += batchSize) batches.push(labelledTraces.slice(i, i + batchSize));
        const METRIC: Record<string, (b: any[]) => number> = {
          accuracy: (b) => (b.filter((t) => String(predOf(t)) === String(t.groundTruth)).length / b.length) * 100,
          f1: (b) => classMetrics(b).f1 * 100,
          rmse: (b) => Math.sqrt(b.reduce((s, t) => s + (Number(predOf(t)) - Number(t.groundTruth)) ** 2, 0) / b.length),
          r2: (b) => {
            const mean = b.reduce((s, t) => s + Number(t.groundTruth), 0) / b.length;
            const ssTot = b.reduce((s, t) => s + (Number(t.groundTruth) - mean) ** 2, 0);
            const ssRes = b.reduce((s, t) => s + (Number(predOf(t)) - Number(t.groundTruth)) ** 2, 0);
            return ssTot ? (1 - ssRes / ssTot) * 100 : NaN;
          },
        };
        const trendFor = (key: string, daily: Array<{ t: string; value: number }> | undefined) => {
          if (daily && daily.length >= 2) return { spark: daily, sparkNote: `daily · ${daily.length} days` };
          if (!METRIC[key] || batches.length < 2) return {};
          const pts = batches.map((b) => ({ t: b[b.length - 1].timestamp, value: METRIC[key](b) })).filter((p) => Number.isFinite(p.value));
          return pts.length >= 2 ? { spark: pts, sparkNote: `per ${batchSize} labelled predictions · ${pts.length} batches` } : {};
        };
        const qualityTile = (label: string, key: string, daily: Array<{ t: string; value: number }> | undefined): Tile => {
          // Regression R² isn't returned by the backend's production block, so
          // it's measured here from the same labelled traces.
          // Same calculation as the Performance tab, so the two never disagree.
          const measured = labelledTraces.length < 10 ? null
            : key === "r2" ? METRIC.r2(labelledTraces)
            : toPct((classMetrics(labelledTraces) as any)[key]);
          const prodVal = measured !== null && Number.isFinite(measured) ? measured : toPct(perf?.production?.[key]);
          const base = toPct(perf?.baseline?.[key]);
          const value = prodVal ?? base;
          return {
            label,
            value: value === null ? null : `${value.toFixed(1)}%`,
            delta: prodVal !== null && base !== null ? prodVal - base : null,
            note: value !== null && prodVal === null ? "from the training run — no production labels for this metric yet" : undefined,
            ...trendFor(key, daily),
            sparkSuffix: "%",
          };
        };
        const f1Key = perf?.production?.f1 != null || perf?.baseline?.f1 != null ? "f1" : "r2";
        // Regression has no accuracy: its headline error is RMSE, production
        // (from ground truth) against the training baseline (√MSE).
        const rmseTile = (): Tile => {
          const p = typeof perf?.production?.rmse === "number" ? perf.production.rmse : null;
          const b = typeof perf?.baseline?.rmse === "number" ? perf.baseline.rmse : typeof perf?.baseline?.mse === "number" ? Math.sqrt(perf.baseline.mse) : null;
          const v = p ?? b;
          const fmt = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: n >= 100 ? 0 : 3 });
          return {
            label: "RMSE",
            value: v === null ? null : fmt(v),
            delta: p !== null && b ? ((p - b) / b) * 100 : null,
            deltaUnit: "%",
            lowerIsBetter: true,
            note: v !== null && p === null ? "from the training run — no production labels yet" : b !== null && p !== null ? `baseline ${fmt(b)}` : undefined,
            ...trendFor("rmse", undefined),
          };
        };
        const tiles: Tile[] = [
          { label: isLlm ? "Requests (1h)" : "Predictions (1h)", value: ov?.predictions1h },
          isLlm ? { label: "Answer relevance", value: ov?.accuracy }
            : f1Key === "r2" ? rmseTile()
            : qualityTile("Accuracy", "accuracy", series.data?.performance.accuracy),
          isLlm ? { label: "Task success", value: ov?.f1 } : qualityTile("F1 / R²", f1Key, f1Key === "f1" ? series.data?.performance.f1 : undefined),
          { label: "Avg confidence", value: ov?.avgConfidence },
          { label: "P95 latency", value: ov?.p95Latency },
          { label: "Error rate", value: ov?.errorRate, note: ov?.errorRateNote },
          { label: "Drifted features", value: ov?.driftedFeatures },
        ];

        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 18 }}>
              Reflects the model's current live state — the date range above applies to the historical
              trend charts on Performance and Drift, not to this snapshot.
            </div>

            {/* Score + bar + reason, matching the mock's header card. */}
            <div className="card card-pad" style={{ marginBottom: 18 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
                <div style={{ textAlign: "center", flex: "none" }}>
                  <div style={{ fontSize: 44, fontWeight: 800, fontFamily: "var(--font-mono)", color: scoreColor }}>
                    {insufficient ? "N/A" : score}
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: ".04em" }}>
                    Health score
                  </div>
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                    <div className="progress-track" style={{ height: 10, flex: 1, marginBottom: 0 }}>
                      <div className="progress-fill" style={{ width: `${insufficient ? 0 : score}%`, background: scoreColor }} />
                    </div>
                    <StatusPill status={insufficient ? "queued" : health.status} label={String(health?.status ?? "unknown").replace(/-/g, " ").toUpperCase()} />
                  </div>
                  <p className="hint" style={{ marginBottom: 0 }}>{note}</p>
                </div>
              </div>
            </div>

            {/* Weighted components — the honest version: an unavailable one is
                shown as "—" with its reason, not folded into the average. */}
            {Object.keys(components).length > 0 && (
              <div className="card card-pad" style={{ marginBottom: 18 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                  <div className="drawer-section-title" style={{ marginBottom: 0 }}>Health components</div>
                  <span className="hint" style={{ margin: 0, color: health.confidencePct >= 80 ? "var(--log-pass)" : health.confidencePct >= 40 ? "var(--amber-700)" : "var(--log-fail)" }}>
                    {health.confidencePct}% of score backed by real signal
                  </span>
                </div>
                <div className="hint" style={{ marginBottom: 12 }}>
                  Real, weighted, continuously recomputed — components without enough real data to
                  score are excluded from the average, not defaulted to a passing grade.
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 10 }}>
                  {Object.entries(components).map(([key, c]: [string, any]) => {
                    const label = HEALTH_LABELS[key] ?? humanizeKey(key);
                    if (!c?.available) {
                      return (
                        <div className="card card-pad" style={{ textAlign: "center", background: "var(--surface-sunken)", opacity: 0.7 }} key={key}>
                          <div className="s-label">{label}</div>
                          <div style={{ fontWeight: 700, fontSize: 20, margin: "6px 0 2px", color: "var(--text-faint)" }}>—</div>
                          <div className="hint" style={{ margin: 0, fontSize: 10.5 }}>{c?.reason}</div>
                        </div>
                      );
                    }
                    const color = c.score >= 85 ? "var(--log-pass)" : c.score >= 65 ? "var(--amber-700)" : "var(--log-fail)";
                    return (
                      <div className="card card-pad" style={{ textAlign: "center" }} key={key}>
                        <div className="s-label">{label}</div>
                        <div style={{ fontWeight: 700, fontSize: 20, margin: "6px 0 2px", color }}>{Math.round(c.score)}</div>
                        <div className="hint" style={{ margin: "0 0 4px", fontSize: 10.5 }}>weight {Math.round(c.weight * 100)}%</div>
                        <div className="hint" style={{ margin: 0, fontSize: 10.5, color: "var(--text-faint)" }} title={c.reason}>{fmtReason(key, c.reason)}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="metric-grid">
              {tiles.map(({ label, value, note, delta, deltaUnit = " pts", lowerIsBetter, spark, sparkNote, sparkSuffix }) => (
                <div className="card card-pad" key={label}>
                  <div className="hint" style={{ marginBottom: 6 }}>{label}</div>
                  <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "var(--font-mono)",
                                color: label === "Drifted features" && typeof value === "number" && value > 0 ? "var(--amber-700)" : undefined }}>
                    {/* An absent tile says so instead of rendering "undefined" —
                        several of these are genuinely unmeasurable until the
                        model serves traffic. */}
                    {value === null || value === undefined ? "not measured yet" : scalarText(value)}
                  </div>
                  {typeof delta === "number" && (
                    <div style={{ marginTop: 4, fontSize: 13, fontWeight: 600, color: (delta >= 0) !== !!lowerIsBetter ? "var(--log-pass)" : "var(--log-fail)" }}>
                      {delta >= 0 ? "↑" : "↓"} {Math.abs(delta).toFixed(1)}{deltaUnit} vs baseline
                    </div>
                  )}
                  {spark && spark.length >= 2 && <MiniSpark points={spark} suffix={sparkSuffix} caption={sparkNote} />}
                  {note && <div className="hint" style={{ marginTop: 6, marginBottom: 0, fontSize: 10.5 }}>{note}</div>}
                </div>
              ))}
            </div>
          </div>
        );
      })()}

      {/* ---- Classification/regression-only tabs ---- */}
      {monitoring.data && !isLlm && tab === "Performance" && (
        <div>
          {(() => {
            const perf = monitoring.data!.performance as any;
            const isClassifier = monitoring.data!.modelType !== "regression" && !(perf?.baseline && "r2" in perf.baseline && !("accuracy" in perf.baseline));
            const traces: any[] = (monitoring.data!.predictions as any)?.traces ?? [];
            // Every "current" number below is measured from served predictions
            // that have a real ground-truth label — the same traces the
            // Predictions tab lists.
            const labelled = traces
              .filter((t) => t.groundTruth !== null && t.groundTruth !== undefined)
              .sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
            const windowed = labelled.filter((t) => inRange(t.timestamp));
            const measured: any = labelled.length ? (isClassifier ? classMetrics(labelled) : regMetrics(labelled)) : null;
            const pct = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? (v > 1.5 ? v : v * 100) : null);
            const baseVal = (key: string) => {
              const b = perf?.baseline ?? {};
              if (key === "rmse" && typeof b.rmse !== "number" && typeof b.mse === "number") return Math.sqrt(b.mse);
              return typeof b[key] === "number" ? b[key] : null;
            };

            const rows: PerfRow[] = isClassifier
              ? [
                  { label: "Accuracy", key: "accuracy", rate: true },
                  { label: "Precision", key: "precision", rate: true },
                  { label: "Recall", key: "recall", rate: true },
                  { label: "F1", key: "f1", rate: true },
                  { label: "ROC-AUC", key: "rocAuc", rate: true },
                  { label: "PR-AUC", key: "prAuc", rate: true },
                  { label: "Log loss", key: "logLoss", lowerIsBetter: true },
                ]
              : [
                  { label: "MAE", key: "mae", lowerIsBetter: true },
                  { label: "RMSE", key: "rmse", lowerIsBetter: true },
                  { label: "R²", key: "r2", rate: true },
                  { label: "MAPE", key: "mape", lowerIsBetter: true, suffix: "%" },
                  { label: "sMAPE", key: "smape", lowerIsBetter: true, suffix: "%" },
                  { label: "Median absolute error", key: "medianAE", lowerIsBetter: true },
                ];
            const table = rows.map((r) => {
              const rawBase = baseVal(r.key);
              const rawCur = (measured as any)?.[r.key] ?? null;
              const base = r.rate ? pct(rawBase) : rawBase;
              const cur = r.rate ? pct(rawCur) : rawCur;
              // Rates compare in points; errors and losses as a relative change.
              const change = base === null || cur === null || !Number.isFinite(cur) ? null : r.rate ? cur - base : base ? ((cur - base) / Math.abs(base)) * 100 : null;
              const relRegression = base === null || cur === null || !base ? null : r.lowerIsBetter ? ((cur - base) / Math.abs(base)) * 100 : ((base - cur) / Math.abs(base)) * 100;
              return { ...r, base, cur, change, relRegression };
            });
            const fmtVal = (r: PerfRow, v: number | null) =>
              v === null || !Number.isFinite(v) ? "—"
                : r.rate ? `${v.toFixed(1)}%`
                : r.suffix ? `${v.toFixed(1)}${r.suffix}`
                : v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 3 });

            // Gate: ≥10% relative regression vs the training baseline fails, 5–10% warns.
            const compared = table.filter((r) => r.relRegression !== null);
            const failing = compared.filter((r) => r.relRegression! >= 10);
            const warning = compared.filter((r) => r.relRegression! >= 5 && r.relRegression! < 10);
            const reasonText = (list: typeof table) =>
              list.map((r) => `${r.label} ${r.change! >= 0 ? "+" : ""}${r.change!.toFixed(1)}${r.rate ? " pts" : "%"}`).join(", ");

            // Trend charts: daily once the range holds two or more days, else
            // the metric over consecutive batches of labelled predictions.
            const ts = series.data;
            const dailyKeys: Record<string, any[] | undefined> = ts
              ? { accuracy: ts.performance.accuracy, f1: ts.performance.f1, precision: ts.performance.precision, recall: ts.performance.recall }
              : {};
            const batchSize = Math.max(10, Math.ceil(windowed.length / 12));
            const batches: any[][] = [];
            for (let i = 0; i + batchSize <= windowed.length; i += batchSize) batches.push(windowed.slice(i, i + batchSize));
            const chartKeys: Array<[string, string, boolean]> = isClassifier
              ? [["Accuracy over time", "accuracy", true], ["F1 over time", "f1", true], ["Precision over time", "precision", true], ["Recall over time", "recall", true]]
              : [["MAE over time", "mae", false], ["RMSE over time", "rmse", false], ["R² over time", "r2", true], ["MAPE over time", "mape", false]];
            const fmtAxis = (iso: string, daily: boolean) =>
              daily ? new Date(iso.slice(0, 10) + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" })
                : new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
            const charts = chartKeys.map(([title, key, rate]) => {
              const daily = byRange(dailyKeys[key]);
              if (daily.length >= 2) return { title, rate, key, points: daily, caption: `daily · ${daily.length} days`, x: [fmtAxis(daily[0].t, true), fmtAxis(daily[daily.length - 1].t, true)] };
              const pts = batches
                .map((b) => {
                  const m: any = isClassifier ? classMetrics(b) : regMetrics(b);
                  const v = rate ? pct(m[key]) : m[key];
                  return { t: b[b.length - 1].timestamp as string, value: v as number };
                })
                .filter((p) => typeof p.value === "number" && Number.isFinite(p.value));
              return { title, rate, key, points: pts, caption: `per ${batchSize} labelled predictions · ${pts.length} batches`,
                       x: pts.length ? [fmtAxis(pts[0].t, false), fmtAxis(pts[pts.length - 1].t, false)] : ["", ""] };
            });

            const coveragePct = traces.length ? (labelled.length / traces.length) * 100 : null;
            const cm = isClassifier && labelled.length ? confusion(labelled) : null;

            return (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 16 }}>
                  {charts.map((c) => (
                    <div className="card card-pad" key={c.title}>
                      <div className="drawer-section-title">{c.title}</div>
                      {c.points.length >= 2 ? (
                        <AreaTrend points={c.points} suffix={c.rate || c.key === "mape" ? "%" : ""} xStart={c.x[0]} xEnd={c.x[1]} caption={c.caption} />
                      ) : (
                        <div className="empty-hint" style={{ padding: "22px 0" }}>
                          {windowed.length ? "Not enough labelled predictions in this range for a trend." : "No labelled predictions in this date range."}
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                <div className="card card-pad" style={{ marginBottom: 16 }}>
                  <div className="drawer-section-title">{isClassifier ? "Model performance" : "Regression performance"}</div>
                  <div style={{ marginBottom: 14 }}>
                    Predictions: {traces.length.toLocaleString()} · Ground truth: {labelled.length.toLocaleString()}
                    {coveragePct !== null ? ` (${coveragePct.toFixed(1)}%)` : ""}
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Change</th></tr></thead>
                      <tbody>
                        {table.map((r) => {
                          const bad = r.relRegression !== null && r.relRegression >= 5;
                          return (
                            <tr key={r.key}>
                              <td style={{ fontWeight: 600 }}>{r.label}</td>
                              <td style={{ color: "var(--text-muted)" }}>{fmtVal(r, r.base)}</td>
                              <td style={{ fontWeight: 700 }}>{fmtVal(r, r.cur)}</td>
                              <td style={{ fontWeight: 600, color: bad ? "var(--log-fail)" : undefined }}>
                                {r.change === null ? "—" : `${r.change >= 0 ? "+" : ""}${r.change.toFixed(1)}${r.rate ? " pts" : "%"}`}
                                {r.relRegression !== null && r.relRegression >= 10 ? " ⚠" : ""}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  <div className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
                    Baseline is the training run's held-out evaluation ({perf?.source ?? "unknown source"}); "—" where the trainer
                    didn't record that metric. Current is measured from {labelled.length.toLocaleString()} served predictions with
                    real ground-truth labels.
                  </div>
                </div>

                <div className="card card-pad" style={{ marginBottom: 16 }}>
                  <div className="drawer-section-title">Performance gate</div>
                  {!compared.length ? (
                    <div className="hint" style={{ marginBottom: 0 }}>
                      Not evaluated — the gate compares measured production metrics against the training baseline, and
                      this model has no ground-truth feedback yet.
                    </div>
                  ) : failing.length ? (
                    <div className="callout callout-red" style={{ marginBottom: 0 }}>
                      <strong>FAIL.</strong> {reasonText(failing)} — at least one metric regressed ≥10% vs baseline. Recommend
                      holding this version and investigating before it stays champion.
                    </div>
                  ) : warning.length ? (
                    <div className="callout callout-amber" style={{ marginBottom: 0 }}>
                      <strong>WARNING.</strong> {reasonText(warning)} — a moderate regression (5–10%) vs baseline. Not
                      gate-failing, worth watching.
                    </div>
                  ) : (
                    <div className="callout callout-teal" style={{ marginBottom: 0 }}>
                      <strong>PASS.</strong> No metric has regressed more than 5% vs its training baseline ({compared.length} compared).
                    </div>
                  )}
                </div>

                {cm && (
                  <div className="card card-pad" style={{ marginBottom: 16 }}>
                    <div className="drawer-section-title">Confusion matrix</div>
                    <div style={{ display: "grid", gridTemplateColumns: `110px repeat(${cm.labels.length}, minmax(120px, 190px))`, gap: 4, fontSize: 13 }}>
                      <div />
                      {cm.labels.map((l) => (
                        <div key={l} style={{ textAlign: "center", fontWeight: 700, color: "var(--text-faint)", padding: 6 }}>Predicted {l}</div>
                      ))}
                      {cm.labels.map((actual, i) => [
                        <div key={`a-${actual}`} style={{ fontWeight: 700, color: "var(--text-faint)", display: "flex", alignItems: "center" }}>Actual {actual}</div>,
                        ...cm.matrix[i].map((n, j) => (
                          <div key={`${i}-${j}`} style={{ textAlign: "center", padding: 16, borderRadius: 8, fontWeight: 700, fontSize: 15,
                                                         background: i === j ? "var(--green-100)" : "var(--red-100)" }}>
                            {n.toLocaleString()}
                          </div>
                        )),
                      ])}
                    </div>
                  </div>
                )}

                <div style={{ display: "grid", gridTemplateColumns: "minmax(240px, 1fr) 3fr", gap: 16, marginBottom: 16 }}>
                  <div className="card card-pad">
                    <div className="drawer-section-title">Calibration</div>
                    <div className="kv-row"><span className="k">Brier score</span>
                      <span className="v">{!isClassifier ? "n/a — regression" : measured?.brier == null ? "—" : measured.brier.toFixed(3)}</span></div>
                    <div className="kv-row"><span className="k">Expected calibration error</span>
                      <span className="v">{!isClassifier ? "n/a — regression" : measured?.ece == null ? "—" : measured.ece.toFixed(3)}</span></div>
                  </div>
                  <div className="card card-pad">
                    <div className="drawer-section-title">Ground-truth coverage</div>
                    <div style={{ fontSize: 28, fontWeight: 800, fontFamily: "var(--font-mono)" }}>
                      {coveragePct === null ? "—" : `${coveragePct.toFixed(1)}%`}
                    </div>
                    <p className="hint" style={{ marginTop: 6, marginBottom: 0 }}>
                      {!traces.length
                        ? "No predictions served yet."
                        : `${labelled.length.toLocaleString()} of ${traces.length.toLocaleString()} retained predictions have a real label submitted through the feedback API` +
                          (labelled.length < traces.length
                            ? ` — the remaining ${(100 - (coveragePct ?? 0)).toFixed(1)}% are still unlabelled and excluded from these numbers, not counted as wrong.`
                            : " — every served prediction is covered.")}
                    </p>
                  </div>
                </div>
              </>
            );
          })()}
        </div>
      )}

      {monitoring.data && isDeepLearning && tab === "Training Metrics" && (
        <div>
          {monitoring.data.trainingMetrics ? (() => {
            const t = monitoring.data!.trainingMetrics!;
            const { progress: p, loss: l, generalization: g, optimization: o, parameters: params, throughput: th, compute: c, earlyStopping: e, checkpoints, health: h } = t;
            const healthColor = h.overall >= 90 ? "var(--teal-700)" : h.overall >= 75 ? "var(--amber-700)" : "var(--red)";
            const genColor = g.status === "HEALTHY" ? "var(--teal-700)" : "var(--amber-700)";
            return (
              <>
                <p className="sub">Neural-network training dynamics for the run that produced this model version: progress, loss/generalization, optimizer &amp; gradients, early stopping, checkpoints, parameters, throughput, and training compute.</p>

                {t.dataSource === "measured" ? (
                  <div className="callout callout-teal">
                    <strong>✓ Real measured telemetry.</strong> {t.measuredBy}. {t.dataNote}
                  </div>
                ) : t.dataSource === "example" ? (
                  <div className="card card-pad" style={{ background: "var(--surface-sunken)" }}>
                    <strong>ℹ Illustrative example data.</strong> This is hand-authored demo data showing what a fully-populated training dashboard looks like — not from a real training run.
                  </div>
                ) : (
                  <div className="callout callout-amber">
                    <strong>⚠ Derived estimate, not measured.</strong> {t.derivedReason || "Real training telemetry wasn't available for this run — these values are estimated from the final reported metric, not from an actual training loop."}
                  </div>
                )}

                <div className="card card-pad">
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 18, flexWrap: "wrap" }}>
                    <div><div className="sub" style={{ marginBottom: 4 }}>Architecture</div><div style={{ fontWeight: 700 }}>{t.architecture}</div></div>
                    <div style={{ textAlign: "right" }}>
                      <div className="sub" style={{ marginBottom: 4 }}>Training health</div>
                      <div style={{ fontSize: 26, fontWeight: 800, color: healthColor }}>{h.overall}/100</div>
                      <span className={`badge ${h.overall >= 90 ? "badge-ml" : "badge-amber"}`}>{h.status}</span>
                    </div>
                  </div>
                  <div style={{ height: 8, background: "var(--border)", borderRadius: 4, marginTop: 16, overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${p.progressPct}%`, background: "var(--navy)" }} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 11.5, color: "var(--text-faint)" }}>
                    <span>Epoch {p.epoch}/{p.totalEpochs} · Step {p.step.toLocaleString()}/{p.totalSteps.toLocaleString()}</span>
                    <span>{p.progressPct}%</span>
                  </div>
                </div>

                <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
                  <MetricMini label="Training loss" value={l.training} sub={`Loss: ${l.type}`} />
                  <MetricMini label="Validation loss" value={l.validation ?? "n/a — see validation score"} sub={`Best validation score ${l.bestValidation} @ epoch ${l.bestEpoch}`} />
                  <MetricMini label="Train / validation F1" value={`${g.trainF1}% / ${g.validationF1}%`} sub={`Gap ${g.gap}%`} />
                  <MetricMini label="Generalization" value={<span style={{ color: genColor }}>{g.status}</span>} sub={`Overfitting: ${g.overfittingStarted}`} />
                  <MetricMini label="Learning rate" value={o.learningRate} sub={`${o.scheduler} · initial ${o.initialLearningRate}`} />
                  <MetricMini label="Gradient norm" value={o.gradientNorm} sub={`NaN ${o.nanGradients} · Inf ${o.infGradients}`} />
                  <MetricMini label="Samples / sec" value={th.samplesPerSec} sub={`${th.batchesPerSec} batches/sec`} />
                  <MetricMini label="Elapsed / ETA" value={`${p.elapsed} / ${p.eta}`} sub={`${p.samplesProcessed} / ${p.totalSamples} samples`} />
                </div>

                <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Training &amp; validation loss</div>
                    <LineChart values={l.trainCurve} color="var(--teal)" />
                    <div style={{ height: 8 }} />
                    <LineChart values={l.valCurve} color="var(--amber)" />
                    <p className="sub" style={{ marginTop: 8, marginBottom: 0 }}>Training loss (teal) and validation loss (amber), epoch 1 → {p.epoch}.</p>
                  </div>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Optimization health</div>
                    <div className="kv"><span className="k">Optimizer</span><span>{o.optimizer}</span></div>
                    <div className="kv"><span className="k">Mean / max gradient</span><span>{o.meanGradient} / {o.maxGradient}</span></div>
                    <div className="kv"><span className="k">Exploding-gradient events</span><span>{o.explodingGradientEvents}</span></div>
                    <div className="kv"><span className="k">Vanishing-gradient warning</span><span>{o.vanishingGradientWarning}</span></div>
                    <div className="sub" style={{ marginTop: 10, marginBottom: 8 }}>Gradient norm trend</div>
                    <LineChart values={o.gradCurve} color="#7c5cbf" />
                  </div>
                </div>

                <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Early stopping</div>
                    <div className="kv"><span className="k">Enabled</span><span>{e.enabled}</span></div>
                    <div className="kv"><span className="k">Monitor</span><span>{e.monitor}</span></div>
                    <div className="kv"><span className="k">Patience / min delta</span><span>{e.patience} / {e.minDelta}</span></div>
                    <div className="kv"><span className="k">Best score / epoch</span><span>{e.bestScore} / {e.bestEpoch}</span></div>
                    <div className="kv"><span className="k">Without improvement</span><span>{e.epochsSinceImprovement}</span></div>
                  </div>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Model parameters</div>
                    <div className="kv"><span className="k">Total parameters</span><span>{params.total}</span></div>
                    <div className="kv"><span className="k">Trainable / frozen</span><span>{params.trainable} / {params.frozen}</span></div>
                    <div className="kv"><span className="k">Weight norm</span><span>{params.weightNorm}</span></div>
                    <div className="kv"><span className="k">Weight mean / std dev</span><span>{params.weightMean} / {params.weightStdDev}</span></div>
                  </div>
                </div>

                <div className="card card-pad">
                  <div className="sub" style={{ marginBottom: 8 }}>Checkpoints</div>
                  <table>
                    <thead><tr><th>Epoch</th><th>Validation loss</th><th>F1</th><th>Status</th></tr></thead>
                    <tbody>
                      {checkpoints.map((cp) => (
                        <tr key={cp.epoch}>
                          <td>{cp.epoch}</td>
                          <td>{cp.valLoss}</td>
                          <td>{cp.f1}%</td>
                          <td>{cp.status === "best" ? <span className="badge badge-ml">★ BEST</span> : <span className="badge badge-amber">{cp.status}</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Training compute</div>
                    <div className="kv"><span className="k">GPU utilization</span><span>{c.gpuUtilization}</span></div>
                    <div className="kv"><span className="k">GPU memory</span><span>{c.gpuMemory}</span></div>
                    <div className="kv"><span className="k">GPU temperature</span><span>{c.gpuTemperature}</span></div>
                    <div className="kv"><span className="k">CPU / memory</span><span>{c.cpu} / {c.memory}</span></div>
                    <div className="kv"><span className="k">Batch size</span><span>{th.batchSize}</span></div>
                  </div>
                  <div className="card card-pad">
                    <div className="sub" style={{ marginBottom: 8 }}>Training health breakdown</div>
                    {Object.entries({ "Loss convergence": h.lossConvergence, "Generalization": h.generalization, "Gradient health": h.gradientHealth, "Learning rate": h.learningRate, "Compute health": h.computeHealth, "Data quality": h.dataQuality }).map(([k, v]) => (
                      <div key={k} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                        <span style={{ flex: "0 0 130px", fontSize: 12 }}>{k}</span>
                        <div style={{ flex: 1, height: 6, background: "var(--border)", borderRadius: 3, overflow: "hidden" }}>
                          <div style={{ height: "100%", width: `${v}%`, background: v >= 90 ? "var(--teal)" : v >= 75 ? "var(--amber)" : "var(--red)" }} />
                        </div>
                        <span style={{ flex: "0 0 30px", fontSize: 12, textAlign: "right" }}>{v}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </>
            );
          })() : (
            <div className="card card-pad">No training metrics recorded for this model.</div>
          )}
        </div>
      )}

      {monitoring.data && isDeepLearning && tab === "Input Quality" && (
        <div>
          {monitoring.data.dlInputQuality ? (
            <>
              <p className="sub">Production input validation for this deployed model: shape/tensor violations, NaN/Inf values, missing/invalid values, and range violations, monitored before they become model-quality incidents.</p>
              <KvGrid data={monitoring.data.dlInputQuality} />
            </>
          ) : (
            <div className="card card-pad">No input-quality metrics recorded for this model.</div>
          )}
        </div>
      )}

      {monitoring.data && !isLlm && tab === "Drift" && (() => {
        const d = monitoring.data!.drift as any;
        const features: any[] = Array.isArray(d.featureDrift) ? d.featureDrift : [];
        const worst = features.slice().sort((a, b) => (b.psi ?? 0) - (a.psi ?? 0))[0];
        const psiSeries = byRange(series.data?.health.maxPsi);
        const fmtDay = (t: string) => new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });
        const statusColor = (s?: string) => (s === "critical" ? "var(--log-fail)" : s === "watch" ? "var(--amber-700)" : "var(--log-pass)");
        const num = (v: unknown, dp = 2) => (typeof v === "number" ? v.toFixed(dp) : "—");
        const kv = (k: string, v: ReactNode, color?: string) => (
          <div className="kv-row" key={k}><span className="k">{k}</span><span className="v" style={color ? { color } : undefined}>{v}</span></div>
        );
        const critical = features.filter((f) => f.status === "critical");
        const watch = features.filter((f) => f.status === "watch");
        const reasons = [
          critical.length ? `feature drift on ${critical.map((f) => `${f.feature} (PSI ${num(f.psi)})`).join(", ")}` : null,
          d.predictionDrift?.psi >= 0.1 ? `prediction drift (PSI ${num(d.predictionDrift.psi)})` : null,
          d.targetDrift && d.targetDrift.status !== "healthy" ? `target drift (${d.targetDrift.status})` : null,
        ].filter(Boolean);
        const firing = (activeAlerts.data ?? []).filter((a) => a.modelId === modelId && a.status === "FIRED");
        const dist = d.distribution;

        if (!features.length && !d.predictionDrift && !d.targetDrift) {
          return <div className="card card-pad">{String(d.note ?? "No drift data available for this version yet.")}</div>;
        }
        return (
          <div>
            <div className="card card-pad" style={{ marginBottom: 18 }}>
              <div className="drawer-section-title">Feature drift trend — max PSI{worst ? ` (currently ${worst.feature})` : ""}</div>
              <p className="hint" style={{ marginBottom: 10 }}>
                {psiSeries.length >= 2
                  ? `The highest PSI across tracked features at each health recompute, over the date range selected above · ${psiSeries.length} points.`
                  : "No drift history in the selected date range — try widening it."}
              </p>
              {psiSeries.length >= 2 && (
                <AreaTrend points={psiSeries} xStart={fmtDay(psiSeries[0].t)} xEnd={fmtDay(psiSeries[psiSeries.length - 1].t)} />
              )}
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 18 }}>
              <div className="card card-pad">
                <div className="drawer-section-title">Feature drift</div>
                {features.length ? features.map((f) => kv(
                  f.feature,
                  `PSI ${num(f.psi)}${typeof f.ks === "number" ? ` · KS ${num(f.ks)}` : ""} — ${String(f.status ?? "healthy").toUpperCase()}`,
                  statusColor(f.status),
                )) : <div className="hint">{d.note ?? "Not computed yet."}</div>}
              </div>
              <div className="card card-pad">
                <div className="drawer-section-title">Prediction drift</div>
                <p className="hint" style={{ marginBottom: 10 }}>Drift on the model's own output.</p>
                {d.predictionDrift ? (
                  <>
                    {kv("PSI", num(d.predictionDrift.psi))}
                    {kv("KS", typeof d.predictionDrift.ks === "number" ? num(d.predictionDrift.ks) : "not computed")}
                    {kv("Baseline → current mean", `${d.predictionDrift.baselineMean} → ${d.predictionDrift.currentMean}`)}
                  </>
                ) : <div className="hint">n/a — no calibrated output distribution to track for this model.</div>}
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 18 }}>
              <div className="card card-pad">
                <div className="drawer-section-title">Target drift</div>
                <p className="hint" style={{ marginBottom: 10 }}>Drift on the real ground-truth label — independent of what the model predicts.</p>
                {d.targetDrift ? (
                  <>
                    {kv("PSI", num(d.targetDrift.psi))}
                    {kv("KS", typeof d.targetDrift.ks === "number" ? num(d.targetDrift.ks) : "not computed")}
                    {d.targetDrift.baselineMean !== undefined && kv("Baseline → current mean", `${d.targetDrift.baselineMean} → ${d.targetDrift.currentMean}`)}
                    {kv("Status", String(d.targetDrift.status ?? "—").toUpperCase(), statusColor(d.targetDrift.status))}
                  </>
                ) : <div className="hint">n/a — needs ground-truth feedback for a labelled production window.</div>}
              </div>
              <div className="card card-pad">
                <div className="drawer-section-title">Concept drift</div>
                <p className="hint" style={{ marginBottom: 10 }}>Has the relationship between inputs and outcome changed, not just the inputs themselves?</p>
                <div className="hint" style={{ marginBottom: 0 }}>{d.conceptDrift?.note ?? "n/a — measured via performance over time, which needs ground truth."}</div>
              </div>
            </div>

            <div className="card card-pad" style={{ marginBottom: 18 }}>
              <div className="drawer-section-title">Training vs. production distribution</div>
              {dist?.classes?.length ? (
                <>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 100px 100px", gap: "6px 10px", fontSize: 11.5, color: "var(--text-faint)", marginBottom: 6 }}>
                    <span /><span style={{ textAlign: "right" }}>Training</span><span style={{ textAlign: "right" }}>Production</span>
                  </div>
                  {dist.classes.map((c: any) => (
                    <div key={c.label} style={{ display: "grid", gridTemplateColumns: "1fr 100px 100px", gap: "6px 10px", fontSize: 12.5, marginBottom: 4 }}>
                      <span>{c.label}</span>
                      <span style={{ textAlign: "right", color: "var(--text-faint)" }}>{c.trainingPct}%</span>
                      <span style={{ textAlign: "right", fontWeight: 700 }}>{c.productionPct}%</span>
                    </div>
                  ))}
                </>
              ) : <div className="hint">n/a — no discrete output distribution for this model type.</div>}
            </div>

            <div className="card card-pad">
              <div className="drawer-section-title">Retraining recommendation</div>
              {reasons.length ? (
                <>
                  <div className="callout callout-red" style={{ marginBottom: 12 }}>
                    <strong>Recommend retraining.</strong> {reasons.join("; ")}
                    {firing.length ? ` — ${firing.length} alert${firing.length === 1 ? " has" : "s have"} fired for this model.` : "."}
                  </div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn btn-primary btn-sm" onClick={() => navigate("/alerts")}>View in Alerts</button>
                    <button className="btn btn-ghost btn-sm" onClick={() => navigate("/train")}>Start a retraining run</button>
                  </div>
                </>
              ) : watch.length ? (
                <div className="callout callout-amber" style={{ marginBottom: 0 }}>
                  <strong>Monitor, not yet urgent.</strong> {watch.map((f) => f.feature).join(", ")} showing moderate drift (below the
                  alert threshold). Worth checking again in a few days — no action needed yet.
                </div>
              ) : (
                <div className="callout callout-teal" style={{ marginBottom: 0 }}>
                  <strong>No retraining signal.</strong> Feature, prediction, target, and concept drift are all within normal range for this model.
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {monitoring.data && !isLlm && tab === "Explainability" && (() => {
        // Read from its own endpoint rather than the monitoring blob: computing
        // it loads the artifact and shells out to Python, far too heavy to run
        // on every monitoring fetch.
        if (explainability.loading) return <div className="empty-hint">Reading feature importances from the fitted model…</div>;
        if (explainability.error) return <div className="callout callout-amber">Could not compute explainability: {explainability.error}</div>;
        const ex = explainability.data;
        if (!ex?.available) {
          return <div className="card card-pad">{ex?.reason ?? "Explainability isn't available for this model."}</div>;
        }
        const top = ex.features ?? [];
        const max = top.length ? Math.max(...top.map((f) => Math.abs(f.importance))) : 1;
        const latest: any = ((monitoring.data!.predictions as any)?.traces ?? []).slice(-1)[0];
        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 16 }}>
              Reflects the current model's live behavior — not affected by the date range above.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 16 }}>
            <div className="card card-pad">
              <div className="drawer-section-title">Global feature importance</div>
              <div className="hint" style={{ marginBottom: 12 }}>
                {ex.method} · {ex.modelClass} · {ex.grouping}
              </div>
              {top.map((f) => (
                <div key={f.feature} style={{ marginBottom: 10 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
                    <span style={{ fontFamily: "var(--font-mono)" }}>{f.feature}</span>
                    <span style={{ color: "var(--text-muted)" }}>{f.sharePct}%</span>
                  </div>
                  <div className="progress-track" style={{ height: 8, marginBottom: 0 }}>
                    <div className="progress-fill" style={{ width: `${(Math.abs(f.importance) / max) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
            <div className="card card-pad">
              <div className="drawer-section-title">Feature attribution drift</div>
              <p className="hint" style={{ marginBottom: 10 }}>Has the RANKING of what matters shifted since training?</p>
              {top.map((f, i) => (
                <div className="kv-row" key={f.feature}>
                  <span className="k" style={{ fontFamily: "var(--font-mono)" }}>{f.feature}</span>
                  <span className="v">rank {i + 1} → <span style={{ color: "var(--text-faint)" }}>not measured</span></span>
                </div>
              ))}
              <div className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
                Training ranks are real. Production ranks need per-prediction attributions, which the serving
                path doesn't compute yet — so no shift is claimed.
              </div>
            </div>
            </div>
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Individual prediction explanation</div>
              <p className="hint" style={{ marginBottom: 10 }}>
                {latest
                  ? `Latest trace ${String(latest.predictionId ?? "").slice(0, 8)} · predicted ${scalarText(typeof latest.prediction === "object" && latest.prediction ? latest.prediction.prediction : latest.prediction)}${typeof latest.confidence === "number" ? ` at ${(latest.confidence * 100).toFixed(0)}% confidence` : ""}`
                  : "No served predictions to explain yet."}
              </p>
              <div className="empty-hint" style={{ padding: "8px 0" }}>
                Per-feature contributions (SHAP) aren't recorded for served predictions, so none are shown for this one.
              </div>
            </div>
            <div className="hint">
              {ex.note} Computed for v{ex.version}
              {ex.computedAt ? ` at ${new Date(ex.computedAt).toLocaleString()}` : ""}.
            </div>
          </div>
        );
      })()}

      {monitoring.data && tab === "Infrastructure" && (() => {
        const inf = monitoring.data!.infrastructure as any;
        if (!inf || typeof inf !== "object") return <div className="empty-hint">No infrastructure data for this model.</div>;
        // Grouped, rather than one flat KvGrid: rendering a long
        // "telemetryNote" sentence as a metric tile stretched the grid so far
        // that the actual numbers became unreadable.
        const lat = inf.measuredLatencyMs;
        // Last 60 minutes of real served requests, from the retained traces, in
        // 5-minute buckets. An empty bucket is a measured zero: no trace exists.
        const traces: any[] = (monitoring.data!.predictions as any)?.traces ?? [];
        const now = Date.now();
        const recent = traces.filter((t) => t.timestamp && now - new Date(t.timestamp).getTime() <= 3600000);
        const buckets = Array.from({ length: 12 }, (_, i) => {
          const end = now - (11 - i) * 300000;
          const n = recent.filter((t) => { const ts = new Date(t.timestamp).getTime(); return ts > end - 300000 && ts <= end; }).length;
          return { t: new Date(end).toISOString(), value: n / 5 };
        });
        const tel = inf.productionTelemetry ?? {};
        const pctOrDash = (v: unknown) => (typeof v === "number" ? `${Math.round(v)}%` : "not collected");
        const tiles: Array<[string, string]> = [
          ["Throughput", `${(recent.length / 60).toFixed(1)}/min`],
          ["P50 / P95 / P99", lat ? `${lat.p50} / ${lat.p95} / ${lat.p99} ms` : "—"],
          ["CPU", pctOrDash(tel.cpuUtilizationPct)],
          ["Memory", typeof tel.memoryUsedBytes === "number" ? `${(tel.memoryUsedBytes / 1024 ** 3).toFixed(1)} GB` : "not collected"],
          ["GPU", pctOrDash(tel.gpuUtilizationPct)],
          ["Errors", typeof tel.errorCount === "number" ? String(tel.errorCount) : "—"],
          ["Timeouts", typeof tel.timeouts === "number" ? String(tel.timeouts) : "—"],
          ["Replicas", inf.replicas ? String(inf.replicas) : "—"],
        ];
        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 16 }}>
              Shows live infrastructure state (last 60 minutes) — not affected by the date range above, which applies
              to daily trend history instead.
            </div>
            <div className="metric-grid" style={{ marginBottom: 16 }}>
              {tiles.map(([k, v]) => (
                <div className="metric-mini" key={k}>
                  <div className="m-label">{k}</div>
                  <div className="m-val" style={{ fontSize: v.length > 12 ? 14 : 18 }}>{v}</div>
                </div>
              ))}
            </div>
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Throughput (last 60 minutes)</div>
              <AreaTrend points={buckets} suffix="/min" xStart="60m ago" xEnd="now"
                caption={recent.length ? `${recent.length} requests in 5-minute buckets` : `No requests in the last 60 minutes${traces.length ? ` — latest was ${new Date(traces[traces.length - 1].timestamp).toLocaleString()}` : ""}`} />
            </div>
          </div>
        );
      })()}

      {/* ---- LLM-only tabs ---- */}
      {monitoring.data && isLlm && tab === "Response Quality" && (() => {
        const perf = monitoring.data!.performance as any;
        const prod = perf?.production as Record<string, any> | undefined;
        const keys = [...new Set([...Object.keys(perf?.baseline ?? {}), ...Object.keys(prod ?? perf?.current ?? {})])]
          .filter((k) => typeof (prod?.[k] ?? perf?.current?.[k] ?? perf?.baseline?.[k]) === "number");
        const lowerIsBetter = (k: string) => /(loss|perplexity|error|latency)/i.test(k);
        const rows = keys.map((k) => {
          const base = typeof perf?.baseline?.[k] === "number" ? perf.baseline[k] : null;
          const cur = typeof prod?.[k] === "number" ? prod[k] : null;
          const change = base !== null && cur !== null && base !== 0 ? ((cur - base) / Math.abs(base)) * 100 : null;
          const regression = change === null ? null : lowerIsBetter(k) ? change : -change;
          return { k, base, cur, change, regression };
        });
        const traces: any[] = (monitoring.data!.predictions as any)?.traces ?? [];
        const ov = monitoring.data!.overview as any;
        const compared = rows.filter((r) => r.regression !== null);
        const failing = compared.filter((r) => r.regression! >= 10);
        const warning = compared.filter((r) => r.regression! >= 5 && r.regression! < 10);
        const fmt = (v: number | null) => (v === null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 3 }));
        return (
          <div>
            <div className="card card-pad" style={{ marginBottom: 18 }}>
              <div className="drawer-section-title">Response quality</div>
              <div style={{ marginBottom: 14 }}>Requests (1h): {ov?.predictions1h ?? 0} · Traced: {traces.length.toLocaleString()}</div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Change</th></tr></thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.k}>
                        <td style={{ fontWeight: 600 }}>{humanizeKey(r.k)}</td>
                        <td style={{ color: "var(--text-muted)" }}>{fmt(r.base)}</td>
                        <td style={{ fontWeight: 700 }}>{r.cur === null ? "not measured" : fmt(r.cur)}</td>
                        <td style={{ fontWeight: 600, color: r.regression !== null && r.regression >= 5 ? "var(--log-fail)" : undefined }}>
                          {r.change === null ? "—" : `${r.change >= 0 ? "+" : ""}${r.change.toFixed(1)}%${r.regression! >= 10 ? " ⚠" : ""}`}
                        </td>
                      </tr>
                    ))}
                    {!rows.length && <tr><td colSpan={4} className="empty-hint">No quality metrics recorded for this model.</td></tr>}
                  </tbody>
                </table>
              </div>
              <div className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
                Baseline is the fine-tuning run's evaluation ({perf?.source ?? "unknown source"}).
                {prod ? "" : " Current needs production responses scored for quality (relevance, correctness, task success) — no scorer is wired to this endpoint yet, so it isn't measured."}
              </div>
            </div>
            <div className="card card-pad">
              <div className="drawer-section-title">Performance gate</div>
              {!compared.length ? (
                <div className="hint" style={{ marginBottom: 0 }}>Not evaluated — there's no measured production quality to compare against the baseline yet.</div>
              ) : failing.length ? (
                <div className="callout callout-red" style={{ marginBottom: 0 }}><strong>FAIL.</strong> {failing.map((r) => humanizeKey(r.k)).join(", ")} regressed ≥10% vs baseline.</div>
              ) : warning.length ? (
                <div className="callout callout-amber" style={{ marginBottom: 0 }}><strong>WARNING.</strong> {warning.map((r) => humanizeKey(r.k)).join(", ")} regressed 5–10% vs baseline.</div>
              ) : (
                <div className="callout callout-teal" style={{ marginBottom: 0 }}><strong>PASS.</strong> No metric has regressed more than 5% vs its baseline.</div>
              )}
            </div>
          </div>
        );
      })()}

      {monitoring.data && isLlm && tab === "Hallucination & Groundedness" && (
        <LlmStatTab
          callout="For a RAG-backed model, groundedness is one of the primary production KPIs — not a secondary quality signal."
          data={monitoring.data.hallucination as any}
        />
      )}

      {monitoring.data && isLlm && tab === "Latency & Serving" && (() => {
        const inf = monitoring.data!.infrastructure as any;
        const lat = inf?.measuredLatencyMs;
        const tel = inf?.productionTelemetry ?? {};
        const sec = (ms: unknown) => (typeof ms === "number" ? (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`) : "not measured");
        const opt = (v: unknown, f: (n: number) => string) => (typeof v === "number" ? f(v) : "not collected");
        return (
          <LlmStatTab
            callout="Time to first token (TTFT) matters as much as total latency for streaming applications — a 5-second response feels fast if the first token appears in 300ms, and slow if nothing appears for 4 seconds."
            data={{
              "P50 latency": sec(lat?.p50),
              "P95 latency": sec(lat?.p95),
              "P99 latency": sec(lat?.p99),
              "TTFT": opt(tel.ttftSeconds, (n) => `${Math.round(n * 1000)}ms`),
              "Time per output token": opt(tel.tpotSeconds, (n) => `${Math.round(n * 1000)}ms`),
              "Output throughput": opt(tel.tokensPerSecond, (n) => `${Math.round(n)} tokens/sec`),
              "Queue depth": opt(tel.queueDepth, (n) => String(n)),
              "Measured requests": lat?.samples != null ? String(lat.samples) : "0",
            }}
          />
        );
      })()}

      {monitoring.data && isLlm && tab === "Cost" && (
        <LlmStatTab
          callout="Token usage and spend — cost is driven directly by tokens in/out, so both are shown together."
          data={monitoring.data.cost as any}
        />
      )}

      {monitoring.data && isLlm && tab === "Safety" && (
        <LlmStatTab
          callout="Unsafe-response rate and false-refusal rate are tracked together deliberately — a model that refuses everything isn't “safe,” it's broken."
          data={monitoring.data.safety as any}
        />
      )}

      {monitoring.data && isLlm && tab === "Drift" && (
        <LlmStatTab
          callout="Signals that production traffic is diverging from what the model was evaluated on — a new topic cluster or embedding shift can degrade quality even with stable infrastructure."
          data={monitoring.data.drift as any}
        />
      )}

      {monitoring.data && isLlm && tab === "Fine-tuning" && (
        monitoring.data.fineTuning ? (
          <div>
            <p className="hint" style={{ margin: "0 0 12px" }}>Training metrics from the run that produced the current live version.</p>
            <KvGrid data={monitoring.data.fineTuning} />
          </div>
        ) : (
          <div className="callout callout-navy">This model wasn't fine-tuned — no training run associated with it.</div>
        )
      )}

      {monitoring.data && tab === "Predictions" && (() => {
        const traces: any[] = (monitoring.data!.predictions as any)?.traces ?? [];
        // Newest first: a live tail, not affected by the date range.
        const rows = [...traces].reverse();
        const fmtTime = (t: any) => (t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : "—");
        // Raw JSON is unreadable at table width; show the real field/value
        // pairs and keep the full body in the title.
        const fmtInput = (raw: any) => {
          try {
            const o = typeof raw === "string" ? JSON.parse(raw) : raw;
            return Object.entries(o).map(([k, v]) => `${k}=${v}`).join(", ");
          } catch { return String(raw); }
        };
        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 16 }}>
              This is a live tail of the most recent requests — not affected by the date range above, since "the last
              few minutes from two weeks ago" isn't a meaningful thing to ask for.
            </div>
            <div className="card card-pad">
              <div className="drawer-section-title">Prediction traces</div>
              <div className="hint" style={{ marginBottom: 12 }}>
                Input features, prediction, confidence, ground truth, correctness, latency and model version — per
                request.
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Time</th><th>Input features</th><th>{isLlm ? "Response" : "Prediction"}</th>
                    <th>Confidence</th><th>Ground truth</th><th>Correct?</th><th>Latency</th><th>Version</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((t: any, i: number) => (
                    <tr key={t.predictionId ?? i}>
                      <td className="mono" style={{ fontSize: 11, whiteSpace: "nowrap" }}>{fmtTime(t)}</td>
                      <td style={{ fontSize: 11, maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }} title={String(t.input)}>
                        {fmtInput(t.input)}
                      </td>
                      <td style={{ fontWeight: 600 }}>
                        {typeof t.prediction === "object" && t.prediction ? scalarText((t.prediction as any).prediction) : scalarText(t.prediction)}
                      </td>
                      <td>{typeof t.confidence === "number" ? `${(t.confidence * 100).toFixed(0)}%` : "—"}</td>
                      <td style={{ color: "var(--text-muted)" }}>{t.groundTruth === null || t.groundTruth === undefined ? "—" : scalarText(t.groundTruth)}</td>
                      <td>{t.correct === null || t.correct === undefined ? "—" : t.correct ? "✓ correct" : "✗ wrong"}</td>
                      <td>{t.latencyMs}ms</td>
                      <td>v{t.version}</td>
                    </tr>
                  ))}
                  {!rows.length && (
                    <tr><td colSpan={8} className="empty-hint">
                      No prediction traces retained for this model yet.
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        );
      })()}

      {tab === "Alerts" && (() => {
        const mine = (activeAlerts.data ?? []).filter((a) => a.modelId === modelId);
        const fired = mine.filter((a) => a.status === "FIRED");
        const building = mine.filter((a) => a.status !== "FIRED");
        const critical = fired.filter((a) => a.severity === "high");
        const resolved = (alertHistory.data ?? []).filter((r) => r.modelId === modelId);
        const channels = alertChannels.data ?? [];
        // A policy belongs on this model's list when it applies to this model's
        // task, which is what the evaluator itself keys on.
        const modelType = monitoring.data?.modelType;
        const policies = (alertPolicies.data ?? []).filter(
          (p) => !modelType || !p.appliesTo?.length || p.appliesTo.includes(modelType)
        );
        const thresholdFor = (p: any) =>
          p.overrides?.find((o: any) => o.modelId === modelId)?.threshold ?? p.defaultThreshold;
        const windowsOf = (a: any) => `${Math.min(a.windowsExceeded, a.windowsToFire)}/${a.windowsToFire}`;
        // Severity is the POLICY's own severity, not "is it firing".
        // Deriving the badge from fired-vs-building instead made the table
        // label a medium-severity latency alert "Critical" while the KPI above
        // it counted zero critical — the same row described two ways on one
        // screen. Firing state is what the Status column is for.
        const SEVERITY_LABEL: Record<string, string> = { high: "Critical", medium: "Warning", low: "Info" };
        const SEVERITY_PILL: Record<string, string> = { high: "failed", medium: "watch", low: "queued" };

        const kpis: Array<[string, number, string, string | undefined]> = [
          ["Open alerts", mine.length, `${critical.length} critical, ${mine.length - critical.length} lower severity`, undefined],
          ["Critical", critical.length, critical.map((a) => a.policyName).join(", ") || "none", critical.length ? "var(--log-fail)" : undefined],
          ["Warnings", mine.length - critical.length, mine.filter((a) => a.severity !== "high").map((a) => a.policyName).join(", ") || "none", mine.length - critical.length ? "var(--amber-700)" : undefined],
          ["Resolved", resolved.length, resolved.length ? `most recently by ${resolved[0].resolvedBy}` : "none yet", undefined],
        ];

        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 18 }}>
              Shows current alert state, live — not affected by any date range. Manage thresholds and delivery for
              every model on the full Alerts page.
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 18 }}>
              {kpis.map(([label, value, sub, color]) => (
                <div className="card card-pad" key={label}>
                  <div className="hint" style={{ marginBottom: 6 }}>{label}</div>
                  <div style={{ fontSize: 26, fontWeight: 800, fontFamily: "var(--font-mono)", color }}>{value}</div>
                  <div className="hint" style={{ marginTop: 4, marginBottom: 0 }}>{sub}</div>
                </div>
              ))}
            </div>

            <div style={{ display: "flex", gap: 16, alignItems: "stretch", marginBottom: 18, flexWrap: "wrap" }}>
              <div className="card card-pad" style={{ flex: "1.6 1 420px" }}>
                <div className="drawer-section-title">Active alerts</div>
                <table>
                  <thead><tr><th>Severity</th><th>Alert</th><th>Condition</th><th>Current</th><th>Since</th><th>Status</th></tr></thead>
                  <tbody>
                    {[...fired, ...building].map((a) => {
                      const isFired = a.status === "FIRED";
                      return (
                        <tr key={a.policyId}>
                          <td>
                            <StatusPill
                              status={SEVERITY_PILL[a.severity] ?? "queued"}
                              label={SEVERITY_LABEL[a.severity] ?? a.severity}
                              compact
                            />
                          </td>
                          <td style={{ fontFamily: "var(--font-mono)" }}>{a.policyName}</td>
                          <td>{a.condition}</td>
                          <td style={{ fontSize: 12 }}>{a.currentValue}</td>
                          <td>{windowsOf(a)} windows</td>
                          <td style={{ color: isFired ? "var(--log-fail)" : "var(--amber-700)", fontWeight: 700, fontSize: 12 }}>
                            {isFired ? "OPEN" : "BUILDING"}
                          </td>
                        </tr>
                      );
                    })}
                    {!mine.length && (
                      <tr><td colSpan={6} className="empty-hint">No active alerts for this model right now.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="card card-pad" style={{ flex: "1 1 260px" }}>
                <div className="drawer-section-title">Notification channels</div>
                {channels.length ? channels.map((ch) => (
                  <div className="kv-row" key={ch.id}>
                    <span className="k">
                      {ch.name} <span className="hint">({ch.type === "slack" ? "Slack" : "Teams"})</span>
                    </span>
                    <span className="v" style={{ color: ch.enabled ? "var(--log-pass)" : "var(--text-faint)" }}>
                      {ch.enabled ? "Enabled" : "Disabled"}
                    </span>
                  </div>
                )) : <div className="empty-hint">No channels configured yet.</div>}
              </div>
            </div>

            <div className="card card-pad">
              <div className="drawer-section-title">Alert policies</div>
              <table>
                <thead>
                  <tr><th>Policy</th><th>Metric</th><th>Condition</th><th>Window</th><th>Consecutive windows</th><th>Action</th><th>Enabled</th></tr>
                </thead>
                <tbody>
                  {policies.map((p) => {
                    const active = mine.find((a) => a.policyId === p.id);
                    return (
                      <tr key={p.id}>
                        <td>{p.label}</td>
                        <td style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{p.name}</td>
                        <td>{p.operator} {thresholdFor(p)}{p.unit}</td>
                        <td>{p.window}</td>
                        <td>{active ? windowsOf(active) : `0/${3}`}</td>
                        <td style={{ fontSize: 12 }}>{p.action}</td>
                        <td style={{ color: p.enabled ? "var(--log-pass)" : "var(--text-faint)", fontWeight: 600 }}>
                          {p.enabled ? "Yes" : "No"}
                        </td>
                      </tr>
                    );
                  })}
                  {!policies.length && (
                    <tr><td colSpan={7} className="empty-hint">No rules configured for this model.</td></tr>
                  )}
                </tbody>
              </table>
              {/* A policy the evaluator cannot score is named rather than shown
                  as a quiet 0/3, which would read as "checked and fine". */}
              {policies.some((p) => p.evaluable === false) && (
                <div className="hint" style={{ marginTop: 10 }}>
                  Not currently evaluable for this model:{" "}
                  {policies.filter((p) => p.evaluable === false).map((p) => p.label).join(", ")} — the signal behind
                  each needs measured production data that hasn't accumulated yet.
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {tab === "Lineage" && (() => {
        const g = governance.data;
        const champion: any = model.data?.versions.find((v) => v.alias === "champion") ?? model.data?.versions[0];
        const r: any = champion?.reproducibility ?? {};
        const kv = (k: string, v: ReactNode, mono = false) => (
          <div className="kv-row" key={k}>
            <span className="k">{k}</span>
            <span className="v" style={{ maxWidth: "65%", textAlign: "right", wordBreak: "break-all", ...(mono ? { fontFamily: "var(--font-mono)", fontSize: 11 } : {}) }}>
              {v === null || v === undefined || v === "" ? "null — not set" : v}
            </span>
          </div>
        );
        const unavailable = (reason: string) => <div className="callout callout-amber" style={{ marginBottom: 0 }}>UCS unreachable: {reason}</div>;
        return (
          <div>
            <div className="callout callout-navy" style={{ marginBottom: 18 }}>
              Real data from the Unified Catalog Service for this model's FQN — including an honest "not yet available"
              where UCS genuinely has nothing to report.
            </div>
            {governance.loading && <div className="card card-pad">Loading from UCS…</div>}
            {g && (
              <>
                <div className="card card-pad" style={{ marginBottom: 16 }}>
                  <div className="drawer-section-title">Upstream / downstream</div>
                  {g.lineage.status === "unavailable" ? unavailable(g.lineage.reason) : g.lineage.data.status === "not_found" ? (
                    <div className="hint" style={{ marginBottom: 0 }}>Not registered in UCS's lineage graph yet — no training run for this model has emitted a lineage event.</div>
                  ) : (
                    <>
                      {kv("Upstream (inputs)", g.lineage.data.upstream.length ? g.lineage.data.upstream.map((n) => `${n.fqn} (via ${n.job})`).join(", ") : "none recorded")}
                      {kv("Downstream (consumers)", g.lineage.data.downstream.length ? g.lineage.data.downstream.map((n) => `${n.fqn} (via ${n.job})`).join(", ") : "none recorded")}
                    </>
                  )}
                </div>

                <div className="card card-pad" style={{ marginBottom: 16 }}>
                  <div className="drawer-section-title">Training runs</div>
                  {g.runs.status === "unavailable" ? unavailable(g.runs.reason) : g.runs.data.runs.length === 0 ? (
                    <div className="hint" style={{ marginBottom: 0 }}>No runs recorded in UCS yet.</div>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>Run</th><th>Job</th><th>State</th><th>When</th></tr></thead>
                        <tbody>
                          {g.runs.data.runs.map((run) => (
                            <tr key={run.run}>
                              <td style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{run.run}</td>
                              <td>{run.job}</td>
                              <td><StatusPill status={run.state === "COMPLETE" ? "healthy" : run.state === "FAIL" ? "failed" : "watch"} label={run.state} compact /></td>
                              <td>{run.ts ?? "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                <div className="card card-pad" style={{ marginBottom: 16 }}>
                  <div className="drawer-section-title">Impact — what depends on this model</div>
                  {g.impact.status === "unavailable" ? unavailable(g.impact.reason) : (
                    <>
                      {kv("Downstream objects", String(g.impact.data.downstream_count))}
                      {kv("Financially flagged downstream", String(g.impact.data.financial_downstream))}
                    </>
                  )}
                </div>
              </>
            )}

            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Reproducibility</div>
              <div className="hint" style={{ marginBottom: 10 }}>
                Exactly what produced this version — real dataset pin, real seed, real dependency hashes. Honest
                "null" for anything genuinely not set, never fabricated.
              </div>
              {kv("Dataset", r.datasetName ? `${r.datasetName}${champion?.datasetSnapshotVersion ? ` · v${champion.datasetSnapshotVersion}` : ""}` : null, true)}
              {kv("Dataset checksum", champion?.datasetChecksum ?? r.datasetChecksum, true)}
              {kv("Seed", r.seed != null ? String(r.seed) : null, true)}
              {kv("Actual framework / architecture", [champion?.actualFramework, champion?.actualArchitecture].filter(Boolean).join(" · ") || null)}
              {kv("Git commit / repo", r.gitCommit || r.gitRepository ? `${r.gitCommit ?? "null"} · ${r.gitRepository ?? "null"}` : null, true)}
              {kv("Container image", r.containerImage ? `${r.containerImage}${r.containerImageDigest ? `@${r.containerImageDigest}` : ""}` : null, true)}
              {kv("Node version / platform", r.nodeVersion ? `${r.nodeVersion} · ${r.platform ?? "?"}/${r.arch ?? "?"}` : null)}
              {kv("package-lock.json SHA-256", r.packageLockSha256, true)}
              {kv("requirements-training.txt SHA-256", r.trainingRequirementsSha256, true)}
              {kv("Captured", r.capturedAt ? new Date(r.capturedAt).toLocaleString() : null)}
            </div>

            {g && (
              <div className="card card-pad">
                <div className="drawer-section-title">Governance profile</div>
                {g.profile.status === "unavailable" ? unavailable(g.profile.reason) : (
                  <>
                    {kv("FQN", g.fqn, true)}
                    {kv("Engine type", String(g.profile.data.engine_type ?? "—"))}
                    {kv("Tags", (g.profile.data.tags as string[] | undefined)?.length ? (g.profile.data.tags as string[]).join(", ") : "none")}
                    {kv("My privileges", JSON.stringify(g.profile.data.my_privileges ?? {}), true)}
                  </>
                )}
              </div>
            )}
          </div>
        );
      })()}

      {tab === "Model Details" && model.data && (() => {
        const m = model.data!;
        const current: any = m.versions.find((v) => v.alias === "champion") ?? m.versions[0];
        const repro: any = current?.reproducibility ?? {};
        const inf: any = monitoring.data?.infrastructure ?? {};
        const run: any = runDetail.data?.run;
        const hp = repro.hyperparams && Object.keys(repro.hyperparams).length ? repro.hyperparams : null;
        const row = (k: string, v: unknown, mono = false) => (
          <div className="kv-row" key={k}>
            <span className="k">{k}</span>
            <span className="v" style={{ maxWidth: "62%", textAlign: "right", wordBreak: "break-all", ...(mono ? { fontFamily: "var(--font-mono)", fontSize: 11.5 } : {}) }}>
              {v === null || v === undefined || v === "" ? "not recorded" : scalarText(v)}
            </span>
          </div>
        );
        const gate = runDetail.data?.validationGate;
        return (
          <div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 18 }}>
              <div className="card card-pad">
                <div className="drawer-section-title">Model identity</div>
                {row("Task", m.task)}
                {row("Owner", null)}
                {row("Team", null)}
                {row("Registered", repro.capturedAt ? new Date(repro.capturedAt).toLocaleString() : m.updated)}
                {row("Framework", current?.actualFramework ?? m.framework)}
              </div>
              <div className="card card-pad">
                <div className="drawer-section-title">Training lineage</div>
                {row("Base model", m.base_ref ?? repro.baseModelId)}
                {row("Dataset", repro.datasetName)}
                {row("Training run", repro.trainingRunId ? String(repro.trainingRunId).slice(0, 8) : null, true)}
                {row("Pipeline", runDetail.data?.pipelineId ? `train-${runDetail.data.pipelineId.slice(0, 6)}` : null, true)}
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 18 }}>
              <div className="card card-pad">
                <div className="drawer-section-title">Deployment</div>
                {row("Runtime", inf.runtime)}
                {row("Endpoint", inf.endpointUrl, true)}
                {row("Compute", run?.computeProfile)}
                {row("Replicas", inf.replicas)}
                {row("Deployed", inf.version != null ? `v${inf.version} live` : null)}
              </div>
              <div className="card card-pad">
                <div className="drawer-section-title">Hyperparameters</div>
                {hp
                  ? Object.entries(hp).map(([k, v]) => row(k, v, true))
                  : <div className="hint" style={{ marginBottom: 0 }}>This run used the base model's defaults — no explicit hyperparameters were recorded.</div>}
              </div>
            </div>

            <div className="card card-pad" style={{ marginBottom: 18 }}>
              <div className="drawer-section-title">Validation results</div>
              {runDetail.loading ? <div className="empty-hint">Loading the training run's validation gate…</div>
                : !repro.trainingRunId ? <div className="hint" style={{ marginBottom: 0 }}>No training run is linked to this version, so there is no validation gate to show.</div>
                : !gate ? <div className="hint" style={{ marginBottom: 0 }}>{runDetail.error ?? "No validation record for this run."}</div>
                : !gate.applicable ? (
                  <>
                    {row("Gate", "N/A")}
                    <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>{gate.reason}</p>
                  </>
                ) : (
                  <>
                    <div className="kv-row">
                      <span className="k">Gate</span>
                      <span className="v" style={{ fontWeight: 700, color: gate.verdict.status === "passed" ? "var(--log-pass)" : gate.verdict.status === "failed" ? "var(--log-fail)" : "var(--text-faint)" }}>
                        {gate.verdict.status === "passed" ? "✓ PASSED" : gate.verdict.status === "failed" ? "✗ FAILED" : "INCOMPLETE"}
                      </span>
                    </div>
                    {gate.rows.map((gr) =>
                      row(`${gr.label} (${gr.direction === "min" ? "≥" : "≤"} ${gr.threshold}${gr.scale === "percent" ? "%" : ""})`,
                        gr.actual === null ? "not recorded" : `${gr.actual}${gr.scale === "percent" ? "%" : ""} · ${gr.result}`)
                    )}
                    <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>{gate.verdict.message}</p>
                  </>
                )}
            </div>

            <div className="card card-pad">
              <div className="drawer-section-title">Artifact &amp; governance</div>
              <div style={{ marginBottom: 10 }}><ArtifactLocation uri={repro.weightsUri} /></div>
              {row("Artifact URI", repro.weightsUri, true)}
              {row("Checksum", current?.datasetChecksum ? `dataset sha256:${String(current.datasetChecksum).slice(0, 16)}…` : null, true)}
              {row("Catalog entry", `ml_models.default.${m.id.replace(/\./g, "_")}`, true)}
              {row("License", baseModel.data?.license)}
            </div>
          </div>
        );
      })()}
      </div>
    </div>
  );
}
