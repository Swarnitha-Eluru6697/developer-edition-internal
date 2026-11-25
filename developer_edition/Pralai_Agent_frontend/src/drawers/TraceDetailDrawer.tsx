import { useEffect, useState } from 'react';
import { api } from '../api';
import Drawer from '../components/Drawer';
import TraceWaterfall from '../components/TraceWaterfall';
import { FailClassChip, PlaneChip, SecHead, KV } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Trace, TraceSpan } from '../types';

export default function TraceDetailDrawer({ traceId }: { traceId: string }) {
  const { closeDrawer } = useApp();
  const [trace, setTrace] = useState<Trace | null>(null);
  const [spans, setSpans] = useState<TraceSpan[]>([]);
  const [failClass, setFailClass] = useState<string | null>(null);

  useEffect(() => {
    api.getTrace(traceId).then(setTrace).catch(() => {});
    api.getTraceSpans(traceId).then((r) => { setSpans(r.spans); setFailClass(r.fail_class); }).catch(() => {});
  }, [traceId]);

  if (!trace) return null;

  return (
    <Drawer title={`Trace · ${trace.id}`} onClose={closeDrawer} footer={<button className="btn ghost" onClick={closeDrawer}>Close</button>}>
      <div className="row-split"><FailClassChip fail={failClass} /><PlaneChip plane={trace.plane} /></div>
      <KV k="Agent" v={<>{trace.agent_name} <span className="mono small muted">{trace.version}</span></>} />
      <KV k="Principal chain" v={<span className="small mono" style={{ textAlign: 'left', maxWidth: 280 }}>{trace.principal}</span>} />
      <KV k="Started" v={<span className="mono small">{trace.ts}</span>} />
      <KV k="Total duration" v={<span className="mono">{trace.duration}</span>} />
      <KV k="Span count" v={trace.spans} />

      {(trace.user_input || trace.final_output) && (
        <>
          <SecHead>What actually happened</SecHead>
          {trace.user_input && (
            <div className="card pad" style={{ marginBottom: 8 }}>
              <div className="small muted" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5, marginBottom: 4 }}>User provided</div>
              <div style={{ whiteSpace: 'pre-wrap' }}>{trace.user_input}</div>
            </div>
          )}
          {trace.final_output && (
            <div className="card pad" style={{ marginBottom: 14 }}>
              <div className="small muted" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5, marginBottom: 4 }}>Agent replied</div>
              <div style={{ whiteSpace: 'pre-wrap' }}>{trace.final_output}</div>
            </div>
          )}
        </>
      )}

      <SecHead>Span waterfall</SecHead>
      <TraceWaterfall spans={spans} />
      <p className="small muted" style={{ marginTop: 14 }}>
        Real spans from the actual run — real node order, real per-node timing, and (where that node type produces
        one) a real content preview. This is the same execution.py result every run produces, not a synthesized
        generic waterfall.
      </p>
    </Drawer>
  );
}
