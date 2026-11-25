import type { PipelineStage } from "../types/domain";

/**
 * The pipeline DAG, rendered identically wherever it appears — the Pipelines
 * list, a pipeline run page, and a training run's detail page all show the same
 * stages from the same projection, so they cannot drift into disagreeing about
 * what stage a run reached.
 */
export function DagStages({ stages }: { stages: PipelineStage[] }) {
  if (!stages.length) return <div className="empty-hint">No stages recorded for this run.</div>;
  return (
    <div className="dag">
      {stages.map((s, i) => (
        <div key={s.order ?? i} style={{ display: "contents" }}>
          {i > 0 && <div className="dag-arrow" />}
          <div className={"dag-node " + dagClass(s.state)}>
            <div className="dn-title">{s.title}</div>
            <div className="dn-sub">{s.sub}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function dagClass(state: string) {
  if (state === "ok") return "done";
  if (state === "running") return "running";
  if (state === "failed") return "failed";
  return "";
}
