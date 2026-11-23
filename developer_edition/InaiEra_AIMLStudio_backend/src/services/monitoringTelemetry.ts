import { Pool } from "pg";
import { recomputeAllDeployedModelHealth } from "./modelHealth";

type Sample = { name: string; labels: Record<string,string>; value: number };

function parsePrometheus(text: string): Sample[] {
  const out: Sample[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|NaN|Inf|-Inf)$/);
    if (!m) continue;
    const labels: Record<string,string> = {};
    if (m[2]) for (const lm of m[2].matchAll(/([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"])*)"/g)) labels[lm[1]] = lm[2].replace(/\\"/g,'"');
    const value = Number(m[3]); if (Number.isFinite(value)) out.push({ name:m[1], labels, value });
  }
  return out;
}

async function scrape(url?: string): Promise<Sample[]> {
  if (!url) return [];
  const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), Number(process.env.MONITORING_SCRAPE_TIMEOUT_MS || 5000));
  try { const r = await fetch(url, { signal: ctrl.signal }); if (!r.ok) throw new Error(`${r.status} ${r.statusText}`); return parsePrometheus(await r.text()); }
  finally { clearTimeout(timer); }
}
function sum(s: Sample[], names: RegExp) { return s.filter(x=>names.test(x.name)).reduce((a,x)=>a+x.value,0); }
function max(s: Sample[], names: RegExp) { const v=s.filter(x=>names.test(x.name)).map(x=>x.value); return v.length?Math.max(...v):null; }

export async function collectProductionTelemetry(pool: Pool): Promise<void> {
  const sources = [
    ["prometheus", process.env.PROMETHEUS_METRICS_URL],
    ["kserve", process.env.KSERVE_METRICS_URL],
    ["vllm", process.env.VLLM_METRICS_URL],
    ["dcgm", process.env.DCGM_METRICS_URL],
  ] as const;
  const collected: Record<string, Sample[]> = {}; const errors: Record<string,string> = {};
  await Promise.all(sources.map(async ([name,url])=>{ if (!url) return; try { collected[name]=await scrape(url); } catch(e) { errors[name]=e instanceof Error?e.message:String(e); } }));
  const all = Object.values(collected).flat();
  const snapshot = {
    collectedAt: new Date().toISOString(), sources: Object.keys(collected), scrapeErrors: errors,
    requestRate: sum(all, /request.*(total|count)|inference.*request/i),
    errorCount: sum(all, /error.*(total|count)|request.*error/i),
    timeouts: sum(all, /timeout/i),
    queueDepth: max(all, /queue.*(size|depth|requests)|num_requests_waiting/i),
    replicas: max(all, /replicas|ready_replicas/i), restarts: sum(all, /restart/i), ooms: sum(all, /oom/i),
    gpuUtilizationPct: max(all, /DCGM_FI_DEV_GPU_UTIL|gpu.*util/i),
    gpuMemoryUsedBytes: max(all, /DCGM_FI_DEV_FB_USED|gpu.*memory.*used/i),
    ttftSeconds: max(all, /time_to_first_token|ttft/i), tpotSeconds: max(all, /time_per_output_token|tpot/i),
    tokensPerSecond: max(all, /tokens.*per.*second|generation_tokens.*rate/i),
  };
  await pool.query(`INSERT INTO monitoring_telemetry_snapshots(source, payload) VALUES ('production-collectors',$1)`, [JSON.stringify(snapshot)]);
  await pool.query(`UPDATE model_monitoring mm SET infrastructure = COALESCE(mm.infrastructure,'{}'::jsonb) || jsonb_build_object('productionTelemetry',$1::jsonb)
                    WHERE EXISTS (SELECT 1 FROM deployments d WHERE d.model_id=mm.model_id)`, [JSON.stringify(snapshot)]);
  await recomputeAllDeployedModelHealth(pool);
}
