import { Pool } from "pg";

export interface Point { t: string; value: number }
export interface ModelTimeseries {
  modelId: string;
  buckets: string;
  /** Real per-bucket accuracy/precision/recall/F1, computed from ground-truth
   *  feedback. Empty until feedback exists — never interpolated. */
  performance: { accuracy: Point[]; precision: Point[]; recall: Point[]; f1: Point[]; groundTruthCount: number };
  /** From prediction traces: volume, latency and model confidence per bucket. */
  serving: { volume: Point[]; p95Latency: Point[]; avgConfidence: Point[] };
  /** From model_metric_history — health and drift as they were recomputed. */
  health: { score: Point[]; maxPsi: Point[] };
  note: string;
}

/** Day key in UTC, so buckets are stable regardless of who is looking. */
const dayOf = (iso: string) => new Date(iso).toISOString().slice(0, 10);

function macroF1(rows: Array<{ prediction: unknown; ground_truth: unknown }>) {
  const labels = [...new Set(rows.flatMap((r) => [String(r.prediction), String(r.ground_truth)]))];
  if (!labels.length) return null;
  let p = 0, r = 0, f = 0;
  for (const label of labels) {
    const tp = rows.filter((x) => String(x.prediction) === label && String(x.ground_truth) === label).length;
    const fp = rows.filter((x) => String(x.prediction) === label && String(x.ground_truth) !== label).length;
    const fn = rows.filter((x) => String(x.prediction) !== label && String(x.ground_truth) === label).length;
    const prec = tp + fp ? tp / (tp + fp) : 0;
    const rec = tp + fn ? tp / (tp + fn) : 0;
    p += prec; r += rec; f += prec + rec ? (2 * prec * rec) / (prec + rec) : 0;
  }
  return { precision: (p / labels.length) * 100, recall: (r / labels.length) * 100, f1: (f / labels.length) * 100 };
}

function percentile(values: number[], q: number): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.ceil((q / 100) * s.length) - 1))];
}

/**
 * Real per-day series for a model.
 *
 * Every point is computed from stored events — prediction traces and
 * ground-truth feedback rows — rather than from a stored chart. A day with no
 * traffic produces no point at all instead of a zero, because "nothing was
 * served" and "everything failed" are different claims and a chart that draws
 * them the same way is worse than an empty chart.
 */
export async function getModelTimeseries(pool: Pool, modelId: string, days = 30): Promise<ModelTimeseries> {
  const since = new Date(Date.now() - days * 86_400_000);

  const mon = await pool.query("SELECT predictions FROM model_monitoring WHERE model_id = $1", [modelId]);
  const traces: any[] = Array.isArray(mon.rows[0]?.predictions?.traces) ? mon.rows[0].predictions.traces : [];
  const measured = traces.filter((t) => t.dataSource === "measured" && typeof t.timestamp === "string" && new Date(t.timestamp) >= since);

  const byDay = new Map<string, any[]>();
  for (const t of measured) {
    const d = dayOf(t.timestamp);
    (byDay.get(d) ?? byDay.set(d, []).get(d)!).push(t);
  }
  const days_ = [...byDay.keys()].sort();

  const volume: Point[] = days_.map((d) => ({ t: d, value: byDay.get(d)!.length }));
  const p95Latency: Point[] = [];
  const avgConfidence: Point[] = [];
  for (const d of days_) {
    const rows = byDay.get(d)!;
    const lat = rows.map((r) => r.latencyMs).filter((x) => typeof x === "number");
    const p = percentile(lat, 95);
    if (p !== null) p95Latency.push({ t: d, value: Math.round(p) });
    const conf = rows.map((r) => r.confidence).filter((x) => typeof x === "number");
    if (conf.length) avgConfidence.push({ t: d, value: Number(((conf.reduce((a, b) => a + b, 0) / conf.length) * 100).toFixed(2)) });
  }

  // Performance per day, from real labels.
  const fb = await pool.query(
    "SELECT prediction, ground_truth, correct, created_at FROM prediction_feedback WHERE model_id = $1 AND created_at >= $2 ORDER BY created_at",
    [modelId, since]
  );
  const fbByDay = new Map<string, any[]>();
  for (const r of fb.rows) {
    const d = dayOf(new Date(r.created_at).toISOString());
    (fbByDay.get(d) ?? fbByDay.set(d, []).get(d)!).push(r);
  }
  const accuracy: Point[] = [];
  const precision: Point[] = [];
  const recall: Point[] = [];
  const f1: Point[] = [];
  for (const d of [...fbByDay.keys()].sort()) {
    const rows = fbByDay.get(d)!;
    const known = rows.filter((r) => r.correct !== null);
    if (!known.length) continue;
    accuracy.push({ t: d, value: Number(((known.filter((r) => r.correct).length / known.length) * 100).toFixed(2)) });
    const m = macroF1(known);
    if (m) {
      precision.push({ t: d, value: Number(m.precision.toFixed(2)) });
      recall.push({ t: d, value: Number(m.recall.toFixed(2)) });
      f1.push({ t: d, value: Number(m.f1.toFixed(2)) });
    }
  }

  const hist = await pool.query(
    `SELECT recorded_at, health_score, max_psi FROM model_metric_history
      WHERE model_id = $1 AND recorded_at >= $2 ORDER BY recorded_at`,
    [modelId, since]
  );
  const score: Point[] = [];
  const maxPsi: Point[] = [];
  for (const r of hist.rows) {
    const t = new Date(r.recorded_at).toISOString();
    if (r.health_score !== null) score.push({ t, value: r.health_score });
    if (r.max_psi !== null) maxPsi.push({ t, value: Number(Number(r.max_psi).toFixed(4)) });
  }

  return {
    modelId,
    buckets: "one point per UTC day for traffic and performance; one per recompute for health and drift",
    performance: { accuracy, precision, recall, f1, groundTruthCount: fb.rowCount ?? 0 },
    serving: { volume, p95Latency, avgConfidence },
    health: { score, maxPsi },
    note: "Computed from stored prediction traces, ground-truth feedback and recorded health snapshots. Days with no traffic produce no point rather than a zero.",
  };
}
