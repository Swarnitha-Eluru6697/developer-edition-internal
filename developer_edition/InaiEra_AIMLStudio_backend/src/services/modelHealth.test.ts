import { describe,it,expect } from "vitest";
import { calculateHealthSnapshot, computeFeatureDrift } from "./modelHealth";

describe("model health",()=>{
  it("does not invent unavailable signals",()=>{
    const h=calculateHealthSnapshot({performance:{baseline:{f1:.9}},predictions:{traces:[]},infrastructure:{}});
    expect(h.signals.f1DropPct).toBeNull(); expect(h.signals.maxPsi).toBeNull(); expect(h.status).toBe("insufficient-data");
  });
  it("computes labeled performance drops and ground-truth coverage",()=>{
    const traces=Array.from({length:10},(_,i)=>({dataSource:"measured",latencyMs:100,missingRatePct:0,predictionId:String(i)}));
    const h=calculateHealthSnapshot({performance:{baseline:{f1:.9,recall:.8},production:{f1:.75,recall:.7,groundTruthCount:5}},predictions:{traces},infrastructure:{}});
    expect(h.signals.f1DropPct).toBeCloseTo(15); expect(h.signals.recallDropPct).toBeCloseTo(10); expect(h.signals.groundTruthCoveragePct).toBe(50);
  });
  // The name always promised "only with enough real samples" but the body only
  // ever exercised the positive case — so when MIN_PSI_SAMPLES_PER_SIDE was
  // raised from 10 to 50 (a 20-vs-20 split was reporting PSI 2.66,
  // "catastrophic drift", on a healthy model) this silently became a test of
  // the floor it no longer met. Both sides are asserted now.
  it("reports no PSI below the minimum sample floor",()=>{
    // 40 traces = 20 per side, under the 50-per-side floor.
    const traces=Array.from({length:40},(_,i)=>({dataSource:"measured",numericFeatures:{x:i<20?i:i+30}}));
    const d=computeFeatureDrift(traces);
    expect(d.maxPsi).toBeNull(); expect(d.features).toEqual([]);
  });
  it("computes production PSI once there are enough real samples",()=>{
    // 120 traces = 60 per side, clearing the floor, with the second half
    // shifted so there is genuine drift to measure.
    const traces=Array.from({length:120},(_,i)=>({dataSource:"measured",numericFeatures:{x:i<60?i:i+200}}));
    const d=computeFeatureDrift(traces);
    expect(d.maxPsi).not.toBeNull(); expect(d.features[0].feature).toBe("x");
  });
});
