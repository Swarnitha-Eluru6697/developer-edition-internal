import { Router, Request, Response } from "express";
import { z } from "zod";
import { pool } from "../db/connection";
import { collectProductionTelemetry } from "../services/monitoringTelemetry";
import { recomputeModelHealth } from "../services/modelHealth";
import { computeExplainability } from "../services/modelExplain";
import { getModelInputSchema } from "../services/modelInputSchema";
import { getModelTimeseries } from "../services/modelTimeseries";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";

export const monitoringRouter = Router();

// Computed on demand rather than on every monitoring read: it shells out to
// Python and loads the artifact, which is far too heavy for the page's main
// fetch. `?refresh=1` forces a recompute after a promotion.
monitoringRouter.get("/models/:id/timeseries", requirePermission(PERM.modelOverview), async (req: Request, res: Response) => {
  const days = Math.min(365, Math.max(1, Number(req.query.days) || 30));
  res.json(await getModelTimeseries(pool, req.params.id, days));
});

monitoringRouter.get("/models/:id/input-schema", requireAnyPermission(PERM.modelOverview, PERM.train), async (req: Request, res: Response) => {
  res.json(await getModelInputSchema(pool, req.params.id));
});

monitoringRouter.get("/models/:id/explainability", requirePermission(PERM.modelOverview), async (req: Request, res: Response) => {
  res.json(await computeExplainability(pool, req.params.id, req.query.refresh === "1"));
});

monitoringRouter.get("/models/:id/monitoring", requirePermission(PERM.modelOverview), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM model_monitoring WHERE model_id = $1", [req.params.id]);
  if (!result.rows.length) return res.status(404).json({ error: `no monitoring data for '${req.params.id}' yet` });
  const r=result.rows[0];
  res.json({ modelId:r.model_id, modelType:r.model_type, healthScore:r.health_score, overview:r.overview, performance:r.performance, drift:r.drift, predictions:r.predictions, explainability:r.explainability, infrastructure:r.infrastructure, hallucination:r.hallucination, safety:r.safety, cost:r.cost, fineTuning:r.fine_tuning, trainingMetrics:r.training_metrics, dlInputQuality:r.dl_input_quality, classicalTrainingResult:r.classical_training_result });
});

const feedbackSchema=z.object({ predictionId:z.string().uuid(), groundTruth:z.any(), metadata:z.record(z.unknown()).optional() });
monitoringRouter.post("/models/:id/feedback", requirePermission(PERM.monitoringEdit), async (req:Request,res:Response)=>{
  const body=feedbackSchema.parse(req.body); const c=await pool.connect();
  try { await c.query("BEGIN");
    const mon=await c.query("SELECT model_type,predictions FROM model_monitoring WHERE model_id=$1 FOR UPDATE",[req.params.id]);
    if(!mon.rows.length){await c.query("ROLLBACK");return res.status(404).json({error:"model monitoring row not found"});}
    const traces=Array.isArray(mon.rows[0].predictions?.traces)?mon.rows[0].predictions.traces:[];
    const idx=traces.findIndex((t:any)=>t.predictionId===body.predictionId); if(idx<0){await c.query("ROLLBACK");return res.status(404).json({error:"predictionId not found in retained trace history"});}
    const pred=traces[idx].prediction; const type=mon.rows[0].model_type; let correct:null|boolean=null; let error:null|number=null;
    if(type==="classification") correct=String(pred)===String(body.groundTruth);
    if(type==="regression" && Number.isFinite(Number(pred)) && Number.isFinite(Number(body.groundTruth))) error=Number(pred)-Number(body.groundTruth);
    traces[idx]={...traces[idx],groundTruth:body.groundTruth,correct,error,feedbackAt:new Date().toISOString()};
    await c.query(`INSERT INTO prediction_feedback(model_id,prediction_id,ground_truth,prediction,correct,error,metadata) VALUES($1,$2,$3,$4,$5,$6,$7)
      ON CONFLICT(model_id,prediction_id) DO UPDATE SET ground_truth=EXCLUDED.ground_truth,correct=EXCLUDED.correct,error=EXCLUDED.error,metadata=EXCLUDED.metadata,updated_at=NOW()`,[req.params.id,body.predictionId,JSON.stringify(body.groundTruth),JSON.stringify(pred),correct,error,JSON.stringify(body.metadata||{})]);
    const fb=await c.query("SELECT prediction,ground_truth,correct,error FROM prediction_feedback WHERE model_id=$1",[req.params.id]);
    let current:any={groundTruthCount:fb.rowCount};
    if(type==="classification") {
      const known=fb.rows.filter((r:any)=>r.correct!==null);
      const labels=[...new Set(known.flatMap((r:any)=>[String(r.prediction),String(r.ground_truth)]))];
      const perClass=labels.map(label=>{
        const tp=known.filter((r:any)=>String(r.prediction)===label&&String(r.ground_truth)===label).length;
        const fp=known.filter((r:any)=>String(r.prediction)===label&&String(r.ground_truth)!==label).length;
        const fn=known.filter((r:any)=>String(r.prediction)!==label&&String(r.ground_truth)===label).length;
        const precision=tp+fp?tp/(tp+fp):0; const recall=tp+fn?tp/(tp+fn):0; const f1=precision+recall?2*precision*recall/(precision+recall):0;
        return {label,precision,recall,f1,tp,fp,fn};
      });
      const avg=(k:"precision"|"recall"|"f1")=>perClass.length?perClass.reduce((a,x)=>a+x[k],0)/perClass.length:null;
      current={...current,accuracy:known.length?known.filter((r:any)=>r.correct).length/known.length:null,precision:avg("precision"),recall:avg("recall"),f1:avg("f1"),perClass};
    }
    if(type==="regression") { const errs=fb.rows.map((r:any)=>Number(r.error)).filter(Number.isFinite); current={...current,mae:errs.length?errs.reduce((a:number,x:number)=>a+Math.abs(x),0)/errs.length:null,rmse:errs.length?Math.sqrt(errs.reduce((a:number,x:number)=>a+x*x,0)/errs.length):null}; }
    await c.query(`UPDATE model_monitoring SET predictions=jsonb_set(COALESCE(predictions,'{}'::jsonb),'{traces}',$1::jsonb), performance=jsonb_set(COALESCE(performance,'{}'::jsonb),'{production}', $2::jsonb) WHERE model_id=$3`,[JSON.stringify(traces),JSON.stringify(current),req.params.id]);
    await c.query("COMMIT");
    const health=await recomputeModelHealth(pool,req.params.id);
    res.json({ok:true,predictionId:body.predictionId,productionPerformance:current,health});
  } catch(e){await c.query("ROLLBACK");throw e;} finally{c.release();}
});

monitoringRouter.post("/monitoring/telemetry/collect", requirePermission(PERM.monitoringEdit), async (_req,res)=>{ await collectProductionTelemetry(pool); res.status(202).json({ok:true}); });
monitoringRouter.get("/monitoring/telemetry/latest", requirePermission(PERM.modelOverview), async (_req,res)=>{ const r=await pool.query("SELECT * FROM monitoring_telemetry_snapshots ORDER BY collected_at DESC LIMIT 1"); res.json(r.rows[0]||null); });
