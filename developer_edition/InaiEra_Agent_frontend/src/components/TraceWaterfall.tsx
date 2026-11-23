import type { TraceSpan } from '../types';

export default function TraceWaterfall({ spans }: { spans: TraceSpan[] }) {
  if (spans.length === 0) {
    return <p className="small muted" style={{ padding: '12px 0' }}>No span detail was recorded for this trace — it predates real span persistence.</p>;
  }
  const maxDur = Math.max(...spans.map((s) => s.duration_ms), 1);
  return (
    <div className="trace">
      {spans.map((s, i) => (
        <div className={`span ${s.kind}`} key={i}>
          <div className="row-split">
            <span className="lbl"><i />{s.name}</span>
            <div className="bar"><span style={{ width: `${Math.max(4, (s.duration_ms / maxDur) * 100)}%` }} /></div>
            <span className="dur">{s.note ? `${(s.duration_ms / 1000).toFixed(1)}s — ${s.note}` : `${(s.duration_ms / 1000).toFixed(1)}s`}</span>
          </div>
          {s.input_preview && (
            <div className="small mono span-preview"><span className="muted">in </span>{s.input_preview}</div>
          )}
          {s.output_preview && (
            <div className="small mono span-preview"><span className="muted">out </span>{s.output_preview}</div>
          )}
        </div>
      ))}
    </div>
  );
}
