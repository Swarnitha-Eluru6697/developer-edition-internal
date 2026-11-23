import { Pool } from "pg";

type Trace = {
  predictionId?: string;
  dataSource?: string;
  latencyMs?: number;
  missingRatePct?: number;
  numericFeatures?: Record<string, number>;
};

type Component = { score: number | null; weight: number; reason: string; available: boolean };
export type HealthSnapshot = {
  score: number;
  status: "healthy" | "watch" | "degraded" | "insufficient-data";
  confidencePct: number;
  components: Record<string, Component>;
  signals: Record<string, number | null>;
  recomputedAt: string;
};

const clamp = (v: number, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, v));
const finite = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) ? v : null;
const asFraction = (v: number) => v > 1.5 ? v / 100 : v;

export function percentile(values: number[], p: number): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a,b)=>a-b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx];
}

// PSI bins the baseline into deciles, so each side needs enough samples for a
// decile to mean anything. At 10-per-side — the old floor — two random halves
// of the SAME distribution routinely score PSI > 2, i.e. "catastrophic drift"
// invented out of sampling noise. 50 per side gives ~5 expected observations
// per bin, the usual minimum for this statistic to be interpretable.
const MIN_PSI_SAMPLES_PER_SIDE = 50;

function psiForFeature(baseline: number[], current: number[]): number | null {
  if (baseline.length < MIN_PSI_SAMPLES_PER_SIDE || current.length < MIN_PSI_SAMPLES_PER_SIDE) return null;
  const sorted = [...baseline].sort((a,b)=>a-b);
  const quantiles = Array.from({length: 9}, (_,i) => sorted[Math.min(sorted.length-1, Math.floor(((i+1)/10) * (sorted.length-1)))]);
  const edges = [...new Set(quantiles)];
  if (!edges.length) return 0;
  const bucket = (x:number) => {
    let i=0; while (i<edges.length && x>edges[i]) i++; return i;
  };
  const bins = edges.length + 1;
  const bc = new Array(bins).fill(0); const cc = new Array(bins).fill(0);
  baseline.forEach(v=>bc[bucket(v)]++); current.forEach(v=>cc[bucket(v)]++);
  const eps = 1e-6;
  let psi=0;
  for(let i=0;i<bins;i++) {
    const bp=Math.max(eps,bc[i]/baseline.length); const cp=Math.max(eps,cc[i]/current.length);
    psi += (cp-bp)*Math.log(cp/bp);
  }
  return Number.isFinite(psi) ? psi : null;
}

export function computeFeatureDrift(traces: Trace[]): { maxPsi: number | null; features: Array<{feature:string;psi:number;status:string}> } {
  const measured = traces.filter(t=>t.dataSource === "measured" && t.numericFeatures && typeof t.numericFeatures === "object");
  // Below this, drift is reported as unmeasurable rather than as a number —
  // an unmeasurable signal drops out of the health score entirely (its weight
  // is excluded), which is the honest outcome, where a noise-derived PSI would
  // have actively pushed a healthy model to "degraded".
  if (measured.length < MIN_PSI_SAMPLES_PER_SIDE * 2) return { maxPsi:null, features:[] };
  const split = Math.floor(measured.length / 2);
  const baseline = measured.slice(0, split);
  const current = measured.slice(split);
  const names = [...new Set(baseline.flatMap(t=>Object.keys(t.numericFeatures || {})))];
  const features: Array<{feature:string;psi:number;status:string}> = [];
  for (const name of names) {
    const b=baseline.map(t=>t.numericFeatures?.[name]).filter((x):x is number=>typeof x === "number" && Number.isFinite(x));
    const c=current.map(t=>t.numericFeatures?.[name]).filter((x):x is number=>typeof x === "number" && Number.isFinite(x));
    const psi=psiForFeature(b,c); if(psi===null) continue;
    features.push({feature:name,psi,status:psi>=0.25?"critical":psi>=0.1?"watch":"healthy"});
  }
  return {maxPsi:features.length?Math.max(...features.map(f=>f.psi)):null,features};
}

function performanceComponent(performance:any): Component {
  const prod=performance?.production || {}; const base={...(performance?.baseline || {})};
  // Trainers record test MSE; production feedback reports RMSE. RMSE is
  // exactly sqrt(MSE), so the baseline is derived rather than left missing —
  // which had dropped performance out of every regression model's score.
  if(finite(base.rmse)===null && finite(base.mse)!==null && base.mse>=0) base.rmse=Math.sqrt(base.mse);
  const candidates:number[]=[]; const reasons:string[]=[];
  for (const key of ["f1","recall","accuracy","precision"]) {
    const c=finite(prod[key]); const b=finite(base[key]); if(c!==null) {
      const cf=asFraction(c); const bf=b!==null?asFraction(b):1;
      candidates.push(clamp((cf/Math.max(0.0001,bf))*100)); reasons.push(`${key}=${c}`);
    }
  }
  for (const key of ["rmse","mae","mape","evalLoss"]) {
    const c=finite(prod[key]); const b=finite(base[key]); if(c!==null && b!==null && c>=0 && b>=0) {
      candidates.push(c===0?100:clamp((b/Math.max(c,1e-9))*100)); reasons.push(`${key}=${c}`);
    }
  }
  if(!candidates.length) return {score:null,weight:.35,reason:"No labeled production performance yet",available:false};
  return {score:Math.min(...candidates),weight:.35,reason:reasons.join(", "),available:true};
}

const TABULAR_LATENCY={budgetMs:100,msPerPoint:10};
const LLM_LATENCY={budgetMs:10000,msPerPoint:100};

export function calculateHealthSnapshot(row:any): HealthSnapshot {
  const traces:Trace[]=Array.isArray(row.predictions?.traces)?row.predictions.traces:[];
  const recent=traces.filter(t=>t.dataSource === "measured").slice(-200);
  const drift=computeFeatureDrift(traces);
  const missingRates=recent.map(t=>finite(t.missingRatePct)).filter((x):x is number=>x!==null);
  const missingPct=missingRates.length?missingRates.reduce((a,b)=>a+b,0)/missingRates.length:null;
  const telemetry=row.infrastructure?.productionTelemetry || {};
  const requestCount=finite(telemetry.requestRate); const errorCount=finite(telemetry.errorCount);
  const errorRatePct=requestCount!==null && requestCount>0 && errorCount!==null ? (errorCount/requestCount)*100 : null;
  const p95=percentile(recent.map(t=>finite(t.latencyMs)).filter((x):x is number=>x!==null),95);
  const restarts=finite(telemetry.restarts); const ooms=finite(telemetry.ooms); const queue=finite(telemetry.queueDepth);

  // A tabular model answers in milliseconds; an LLM generating a few hundred
  // tokens takes seconds. Scoring both against a 100 ms budget gave every
  // working LLM a serving score of 0 and a "degraded" verdict.
  const latency=row.model_type==="generation"?LLM_LATENCY:TABULAR_LATENCY;
  const components:Record<string,Component>={
    performance: performanceComponent(row.performance),
    drift: drift.maxPsi===null?{score:null,weight:.20,reason:`Insufficient production window for PSI — needs ${MIN_PSI_SAMPLES_PER_SIDE*2} measured predictions, has ${traces.filter(t=>t.dataSource==="measured").length}`,available:false}:{score:clamp(100-(drift.maxPsi/0.5)*100),weight:.20,reason:`max PSI=${drift.maxPsi.toFixed(4)}`,available:true},
    dataQuality: missingPct===null?{score:null,weight:.15,reason:"No measured input-quality window",available:false}:{score:clamp(100-missingPct*4),weight:.15,reason:`missing=${missingPct.toFixed(2)}%`,available:true},
    serving: errorRatePct===null && p95===null?{score:null,weight:.20,reason:"No serving signals yet",available:false}:{score:Math.min(errorRatePct===null?100:clamp(100-errorRatePct*10),p95===null?100:clamp(100-Math.max(0,p95-latency.budgetMs)/latency.msPerPoint)),weight:.20,reason:`error=${errorRatePct?.toFixed(2) ?? "n/a"}%, p95=${p95?.toFixed(1) ?? "n/a"}ms (budget ${latency.budgetMs}ms)`,available:true},
    infrastructure: restarts===null && ooms===null && queue===null?{score:null,weight:.10,reason:"No infrastructure signals yet",available:false}:{score:clamp(100-(restarts||0)*5-(ooms||0)*25-Math.min(30,(queue||0)*2)),weight:.10,reason:`restarts=${restarts??"n/a"}, ooms=${ooms??"n/a"}, queue=${queue??"n/a"}`,available:true},
  };
  const available=Object.values(components).filter(c=>c.available && c.score!==null);
  const totalWeight=available.reduce((a,c)=>a+c.weight,0);
  const score=totalWeight?Math.round(available.reduce((a,c)=>a+(c.score as number)*c.weight,0)/totalWeight):0;
  const confidencePct=Math.round(totalWeight*100);
  const status=confidencePct<30?"insufficient-data":score>=85?"healthy":score>=65?"watch":"degraded";
  const groundTruthCount=finite(row.performance?.production?.groundTruthCount) || 0;
  const gtCoveragePct=recent.length?Math.min(100,(groundTruthCount/recent.length)*100):null;
  const prodF1=finite(row.performance?.production?.f1); const baseF1=finite(row.performance?.baseline?.f1);
  const prodRecall=finite(row.performance?.production?.recall); const baseRecall=finite(row.performance?.baseline?.recall);
  const drop=(b:number|null,c:number|null)=>b===null||c===null?null:Math.max(0,(asFraction(b)-asFraction(c))*100);
  return {score,status,confidencePct,components,signals:{
    f1DropPct:drop(baseF1,prodF1), recallDropPct:drop(baseRecall,prodRecall), maxPsi:drift.maxPsi,
    errorRatePct, missingValuesPct:missingPct, groundTruthCoveragePct:gtCoveragePct, p95LatencyMs:p95,
  },recomputedAt:new Date().toISOString()};
}

export async function recomputeModelHealth(pool:Pool, modelId:string):Promise<HealthSnapshot|null> {
  const r=await pool.query("SELECT * FROM model_monitoring WHERE model_id=$1",[modelId]); if(!r.rows.length)return null;
  const row=r.rows[0]; const drift=computeFeatureDrift(Array.isArray(row.predictions?.traces)?row.predictions.traces:[]);
  if(drift.features.length){
    const traces:Trace[]=(Array.isArray(row.predictions?.traces)?row.predictions.traces:[]).filter((t:any)=>t.dataSource==="measured");
    // Prediction drift over the same window, using the same PSI: the
    // distribution of what the model OUTPUTS shifting is a distinct signal
    // from its inputs shifting, and it needs no ground truth to measure.
    const numericPreds=traces.map((t:any)=>finite(typeof t.prediction==="object"&&t.prediction?"prediction" in t.prediction?t.prediction.prediction:null:t.prediction)).filter((x):x is number=>x!==null);
    let predictionDrift:Record<string,unknown>|undefined;
    if(numericPreds.length>=MIN_PSI_SAMPLES_PER_SIDE*2){
      const half=Math.floor(numericPreds.length/2);
      const b=numericPreds.slice(0,half), c=numericPreds.slice(half);
      const psi=psiForFeature(b,c);
      const mean=(a:number[])=>a.reduce((x,y)=>x+y,0)/a.length;
      if(psi!==null) predictionDrift={psi,baselineMean:Number(mean(b).toFixed(4)),currentMean:Number(mean(c).toFixed(4)),
        status:psi>=0.25?"critical":psi>=0.1?"watch":"healthy",
        // KS isn't computed here; the field is present in the seeded shape,
        // so say it's absent rather than leaving a stale seeded number.
        ks:null};
    }
    // Target drift: has the real LABEL distribution moved? Independent of what
    // the model predicts, so it needs ground truth — computed only from
    // feedback rows, and absent (not zero) when there aren't enough.
    let targetDrift:Record<string,unknown>|undefined;
    const fb=await pool.query("SELECT ground_truth, created_at FROM prediction_feedback WHERE model_id=$1 ORDER BY created_at",[modelId]);
    if(fb.rows.length>=MIN_PSI_SAMPLES_PER_SIDE*2){
      const half=Math.floor(fb.rows.length/2);
      const asNum=(r:any)=>{const v=typeof r.ground_truth==="string"?JSON.parse(r.ground_truth):r.ground_truth;return finite(Number(v));};
      const b=fb.rows.slice(0,half).map(asNum).filter((x):x is number=>x!==null);
      const c=fb.rows.slice(half).map(asNum).filter((x):x is number=>x!==null);
      if(b.length>=MIN_PSI_SAMPLES_PER_SIDE&&c.length>=MIN_PSI_SAMPLES_PER_SIDE){
        const psi=psiForFeature(b,c);
        const mean=(a:number[])=>a.reduce((x,y)=>x+y,0)/a.length;
        if(psi!==null) targetDrift={psi,ks:null,baselineMean:Number(mean(b).toFixed(4)),currentMean:Number(mean(c).toFixed(4)),
          status:psi>=0.25?"critical":psi>=0.1?"watch":"healthy",groundTruthCount:fb.rows.length};
      }
    }

    // Training vs production class balance — the mock's distribution table.
    // Training comes from the dataset snapshot's own profile of the target
    // column; production from the model's actual predictions.
    let distribution:Record<string,unknown>|undefined;
    const ver=await pool.query(
      `SELECT r.dataset_name, r.target_column, v.dataset_snapshot_version
         FROM deployments d JOIN model_versions v ON v.model_id=d.model_id AND v.v=d.current_version
         LEFT JOIN training_runs r ON r.id=v.training_run_id WHERE d.model_id=$1`,[modelId]);
    const dv=ver.rows[0];
    if(dv?.dataset_name&&dv?.target_column){
      const snap=await pool.query(
        `SELECT profile FROM dataset_snapshots WHERE dataset_name=$1 ${dv.dataset_snapshot_version?"AND version=$2":""} ORDER BY version DESC LIMIT 1`,
        dv.dataset_snapshot_version?[dv.dataset_name,dv.dataset_snapshot_version]:[dv.dataset_name]);
      const col=(snap.rows[0]?.profile?.columns||[]).find((c:any)=>c.name===dv.target_column);
      let top:Array<{value:unknown;count:number}>=Array.isArray(col?.topValues)?col.topValues:[];
      // A binary numeric target (0/1) is profiled as numeric, so it carries a
      // mean and no topValues — but its class balance is exactly the mean.
      // Without this the distribution table was simply missing for every
      // 0/1-labelled classifier, which is most of them.
      if(!top.length && col?.kind==="numeric" && col.min===0 && col.max===1 && typeof col.mean==="number"){
        const rows=snap.rows[0]?.profile?.rowCount ?? 1000;
        top=[{value:1,count:Math.round(col.mean*rows)},{value:0,count:Math.round((1-col.mean)*rows)}];
      }
      if(top.length){
        const trainTotal=top.reduce((a:number,t:any)=>a+t.count,0)||1;
        const prodCounts=new Map<string,number>();
        for(const t of traces as any[]){const raw=t.prediction;const k=String(raw&&typeof raw==="object"?raw.prediction:raw);prodCounts.set(k,(prodCounts.get(k)||0)+1);}
        const prodTotal=traces.length||1;
        distribution={
          classes: top.map((t:any)=>({
            label:String(t.value),
            trainingPct:Number((100*t.count/trainTotal).toFixed(1)),
            productionPct:prodCounts.has(String(t.value))?Number((100*(prodCounts.get(String(t.value)) as number)/prodTotal).toFixed(1)):0,
          })),
          note:"Training share is the target column's real distribution in the pinned dataset snapshot; production share is the model's own predictions over its retained traces.",
        };
      }
    }

    row.drift={...(row.drift||{}),
      dataSource:"measured-production",
      featureDrift:drift.features,
      maxPsi:drift.maxPsi,
      ...(predictionDrift?{predictionDrift}:{}),
      ...(targetDrift?{targetDrift}:{}),
      ...(distribution?{distribution}:{}),
      // Concept drift needs BOTH a stable input distribution and a moving
      // performance signal; stating the two real numbers is honest, inventing
      // a verdict from one recompute is not.
      conceptDrift:{
        note: targetDrift
          ? `Inputs moved by max PSI ${drift.maxPsi?.toFixed(4) ?? "n/a"} while the label distribution moved by PSI ${(targetDrift.psi as number).toFixed(4)}. A relationship change shows up as performance falling while inputs stay put — that needs several recomputes to separate from noise.`
          : "Not computable yet — separating a changed input/outcome relationship from ordinary input drift needs a labelled production window, and not enough ground truth has been submitted.",
      },
      // The registration-time note says drift is "not yet computable"; it
      // plainly is now, and leaving it would contradict the table beside it.
      note:`Computed from ${traces.length} measured predictions.`,
      recomputedAt:new Date().toISOString()};
  }
  const snapshot=calculateHealthSnapshot(row);

  // The overview tiles (predictions1h / p95Latency / errorRate) are written at
  // registration as the literal strings "n/a — not yet deployed" and were
  // never updated afterwards, so a model serving real traffic still displayed
  // "not yet deployed" next to a health score computed from that very
  // traffic. Recompute them from the same measured traces the score uses.
  const measured:Trace[]=(Array.isArray(row.predictions?.traces)?row.predictions.traces:[]).filter((t:Trace)=>t.dataSource==="measured");
  const tiles:Record<string,unknown>={};
  if(measured.length){
    const hourAgo=Date.now()-3600_000;
    const withTime=measured.filter((t:any)=>typeof t.timestamp==="string");
    // Only claim a 1h count when traces actually carry timestamps to count by.
    tiles.predictions1h=withTime.length?withTime.filter((t:any)=>Date.parse(t.timestamp)>=hourAgo).length:`${measured.length} total — no per-trace timestamps to window by`;
    const p95m=percentile(measured.map(t=>finite(t.latencyMs)).filter((x):x is number=>x!==null),95);
    if(p95m!==null) tiles.p95Latency=`${Math.round(p95m)} ms`;
    // A trace is only recorded for a request that produced a prediction, so
    // these traces genuinely cannot measure the failure rate. Say that,
    // rather than leaving the registration-time "not yet deployed" text on a
    // model that plainly IS deployed, and rather than writing "0%", which
    // would assert something this data does not show.
    // Short enough to read as a tile value; the reason goes alongside rather
    // than inside it, so the number slot stays a number slot.
    tiles.errorRate="not measured";
    tiles.errorRateNote="Only successful predictions are traced, so these traces cannot measure a failure rate. Writing 0% would assert something the data does not show.";
    // The mock's Overview shows an average-confidence tile. Traces carry the
    // model's own predicted probability, so this is measured, not assumed —
    // absent for a regressor, which has no confidence to report.
    const confidences=measured.map(t=>finite((t as any).confidence)).filter((x):x is number=>x!==null);
    if(confidences.length) tiles.avgConfidence=`${(confidences.reduce((a,b)=>a+b,0)/confidences.length*100).toFixed(1)}%`;
  }
  // Drifted-feature count, from the same PSI table the Drift tab renders, so
  // the tile and the tab can't disagree.
  const featureDrift:Array<{status?:string}>=Array.isArray(row.drift?.featureDrift)?row.drift.featureDrift:[];
  tiles.driftedFeatures=featureDrift.length?featureDrift.filter(f=>f.status==="watch"||f.status==="critical").length:"not measured yet";

  const overview={...(row.overview||{}),...tiles,health:snapshot};
  const infrastructure=await buildInfrastructure(pool,modelId,measured);

  // Append a point to the time axis. model_monitoring only ever holds the
  // CURRENT state, so every "over time" chart in the UI had nothing behind it;
  // this is what gives the drift trend and the health history real data as it
  // accumulates, instead of a drawn squiggle.
  await pool.query(
    `INSERT INTO model_metric_history
       (model_id, health_score, health_status, max_psi, p95_latency_ms, missing_pct, gt_coverage_pct, components)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)`,
    [modelId, snapshot.score, snapshot.status, snapshot.signals.maxPsi, snapshot.signals.p95LatencyMs,
     snapshot.signals.missingValuesPct, snapshot.signals.groundTruthCoveragePct, JSON.stringify(snapshot.components)]
  ).catch((err)=>{
    // A history row is supplementary: losing one must not fail the recompute
    // that produced the live snapshot.
    // eslint-disable-next-line no-console
    console.error(`metric history append failed for '${modelId}':`, err instanceof Error?err.message:err);
  });
  await pool.query(
    "UPDATE model_monitoring SET health_score=$1, overview=$2::jsonb, drift=$3::jsonb, infrastructure=$4::jsonb WHERE model_id=$5",
    [snapshot.score,JSON.stringify(overview),JSON.stringify(row.drift||{}),JSON.stringify(infrastructure),modelId]
  );
  return snapshot;
}

/**
 * Real serving/infrastructure facts for a model.
 *
 * Replaces a string written once at registration — "Not yet deployed —
 * infrastructure metrics appear once this version is promoted and serving
 * traffic" — that nothing ever replaced, so it stayed on screen for models
 * that had been promoted and were serving. Everything here is read from the
 * deployment row, the endpoint row, and the measured traces; scraped
 * Prometheus telemetry is merged in when it exists and its absence is stated
 * rather than filled with zeros.
 */
async function buildInfrastructure(pool:Pool, modelId:string, measured:Trace[]):Promise<Record<string,unknown>> {
  const dep=await pool.query(
    `SELECT d.current_version, d.runtime, d.replicas, d.health_status, e.url, e.rpm, e.burst, e.concurrent, e.status AS endpoint_status
       FROM deployments d LEFT JOIN endpoints e ON e.model_id = d.model_id
      WHERE d.model_id=$1`,[modelId]);
  if(!dep.rows.length){
    return { status:"Not deployed — no serving infrastructure exists for this model yet.", dataSource:"deployment state" };
  }
  const d=dep.rows[0];
  const lat=measured.map(t=>finite(t.latencyMs)).filter((x):x is number=>x!==null);
  const mon=await pool.query("SELECT infrastructure FROM model_monitoring WHERE model_id=$1",[modelId]);
  const telemetry=mon.rows[0]?.infrastructure?.productionTelemetry ?? null;
  return {
    status:`Deployed — v${d.current_version} on ${d.runtime||"local"}`,
    version:d.current_version,
    runtime:d.runtime,
    replicas:d.replicas,
    deploymentHealth:d.health_status,
    endpointUrl:d.url,
    endpointStatus:d.endpoint_status,
    rateLimits: d.rpm!=null?{ rpm:d.rpm, burst:d.burst, concurrent:d.concurrent }:null,
    measuredLatencyMs: lat.length?{
      p50:Math.round(percentile(lat,50) as number),
      p95:Math.round(percentile(lat,95) as number),
      p99:Math.round(percentile(lat,99) as number),
      samples:lat.length,
    }:null,
    tracedPredictions: measured.length,
    // Named explicitly: these come from a Prometheus/DCGM scrape that is not
    // configured locally, and a null here means "not collected", not "zero".
    productionTelemetry: telemetry,
    telemetryNote: telemetry?null:"CPU/memory/GPU/restart metrics need a configured Prometheus or DCGM endpoint (PROMETHEUS_METRICS_URL / DCGM_METRICS_URL); none is set, so they are not collected rather than zero.",
    dataSource:"deployment + endpoint rows and measured prediction traces",
    recomputedAt:new Date().toISOString(),
  };
}

export async function recomputeAllDeployedModelHealth(pool:Pool):Promise<void>{
  const r=await pool.query("SELECT model_id FROM deployments");
  for(const row of r.rows) await recomputeModelHealth(pool,row.model_id);
}
