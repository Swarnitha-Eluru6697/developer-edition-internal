import { useEffect, useState } from 'react';
import { api } from '../api';
import Drawer from '../components/Drawer';
import { SevChip, SecHead, KV } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Alert } from '../types';

export default function AlertDetailDrawer({ alertId }: { alertId: string }) {
  const { closeDrawer, me, toast, notifyDataChanged } = useApp();
  const [alert, setAlert] = useState<Alert | null>(null);

  const load = () => api.getAlert(alertId).then(setAlert).catch(() => {});
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [alertId]);

  if (!alert) return null;
  // Recorded on the alert as who acknowledged/resolved it. The API takes the
  // gateway-verified identity where it has one; this is the display name.
  const who = me?.user || 'unknown';

  const acknowledge = () => api.acknowledgeAlert(alert.id, who).then((a) => { setAlert(a); notifyDataChanged(); toast('Alert acknowledged'); }).catch((e) => toast(e.message));
  const resolve = () => api.resolveAlert(alert.id, who).then(() => { notifyDataChanged(); toast('Alert resolved'); closeDrawer(); }).catch((e) => toast(e.message));
  return (
    <Drawer title={`Alert · ${alert.rule}`} onClose={closeDrawer} footer={
      alert.state === 'open'
        ? <>
            <button className="btn ghost" onClick={acknowledge}>Acknowledge</button>
            <button className="btn" onClick={resolve}>Resolve</button>
          </>
        : <button className="btn ghost" onClick={closeDrawer}>Close</button>
    }>
      <div className="row-split">
        <SevChip sev={alert.sev} /><span className={`chip ${alert.state === 'open' ? 'amber' : 'line'}`}>{alert.state}</span>
        {alert.acked && alert.state === 'open' && <span className="chip blue">acknowledged</span>}
      </div>
      <p style={{ margin: '14px 0 0' }}>{alert.summary}</p>
      <SecHead>Details</SecHead>
      <KV k="Agent" v={alert.agent_name} />
      <KV k="Detected" v={<span className="mono small">{alert.ts}</span>} />
      <SecHead>History</SecHead>
      {alert.history.length ? alert.history.map((h, i) => (
        <div className="row-split" key={i} style={{ padding: '7px 0', borderBottom: '1px solid var(--line-2)' }}>
          <span className="small"><b>{h.action}</b> by {h.by}</span><span className="mono small muted">{h.ts}</span>
        </div>
      )) : <span className="small muted">No actions recorded yet</span>}
    </Drawer>
  );
}
