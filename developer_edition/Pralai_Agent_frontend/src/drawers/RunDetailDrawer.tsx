import { useEffect, useState } from 'react';
import { api } from '../api';
import Drawer from '../components/Drawer';
import Icon from '../components/Icon';
import TraceWaterfall from '../components/TraceWaterfall';
import { RunStatusChip, FailClassChip, SecHead, KV } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Run, TraceSpan } from '../types';

export default function RunDetailDrawer({ runId }: { runId: string }) {
  const { closeDrawer } = useApp();
  const [run, setRun] = useState<Run | null>(null);
  const [spans, setSpans] = useState<TraceSpan[]>([]);
  const [failClass, setFailClass] = useState<string | null>(null);

  useEffect(() => {
    api.getRun(runId).then((r) => {
      setRun(r);
      // reuse the trace-span synthesizer keyed off a matching trace when available
      api.listTraces().then((traces) => {
        const match = traces.find((t) => t.agent_id === r.agent_id);
        if (match) api.getTraceSpans(match.id).then((res) => { setSpans(res.spans); setFailClass(res.fail_class); });
      });
    }).catch(() => {});
  }, [runId]);

  if (!run) return null;

  return (
    <Drawer title={`Run · ${run.id}`} onClose={closeDrawer} footer={<button className="btn ghost" onClick={closeDrawer}>Close</button>}>
      <div className="row-split"><RunStatusChip status={run.status} /><FailClassChip fail={run.fail_class} /><span className="fqn">{run.agent_name}</span></div>
      <SecHead style={{ marginTop: 18 }}>Trace</SecHead>
      {spans.length > 0 && <TraceWaterfall spans={spans} />}
      <SecHead>Principal chain</SecHead>
      <div className="chain">
        <div className="hop user"><b>user:sam</b></div>
        <div className="gate"><div className={`g${failClass === 'ucs' ? ' deny' : ''}`}><Icon name={failClass === 'ucs' ? 'i-x' : 'i-check'} /></div><small>hop 1</small></div>
        <div className="hop agent"><b>agent:{run.agent_name.split(' ')[0].toLowerCase()}</b></div>
        {run.hops > 1 && (
          <>
            <div className="gate"><div className={`g${failClass === 'ucs' ? ' deny' : ''}`}><Icon name={failClass === 'ucs' ? 'i-x' : 'i-check'} /></div><small>hop 2</small></div>
            <div className="hop agent"><b>sub-agent</b></div>
          </>
        )}
        <div className="gate"><div className={`g${failClass === 'ucs' ? ' deny' : ''}`}><Icon name={failClass === 'ucs' ? 'i-x' : 'i-check'} /></div><small>hop {run.hops}</small></div>
        <div className="hop data"><b>target object</b></div>
      </div>
      <SecHead>Cost</SecHead>
      <KV k="Total" v={<span className="mono">{run.cost}</span>} />
      <KV k="Rolled up to" v={`workspace: ${run.agent_name.split(' ')[0].toLowerCase()}-team`} />
    </Drawer>
  );
}
